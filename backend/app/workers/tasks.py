import json
from time import monotonic

from sqlalchemy import or_, select, text, update

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Job,
    JobStatus,
    Playlist,
    PlaylistSource,
    ServiceEnum,
    utcnow,
)
from app.services.musicbrainz import MusicBrainzClient, enrich_albums
from app.services.matcher import run_matching
from app.services.scanner import ScanAlreadyRunning, scan_library
from app.services.spotify import import_spotify_playlists, refresh_spotify_playlist
from app.services.yandex import import_yandex_playlists, refresh_yandex_playlist
from app.workers.celery_app import celery


class JobLeaseLost(RuntimeError):
    """The task was superseded and must stop without changing its job."""


_IMPORT_EXECUTION_LOCK_NAMESPACE = 20260809


def _acquire_import_source_lock(db, source_id: int):
    """Serialize provider writes for one source across PostgreSQL workers.

    A dedicated connection owns the session-level advisory lock because provider
    importers intentionally commit per playlist. Transaction-level locks on the
    ORM session would be released by those commits.
    """

    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        return None

    connection = bind.connect()
    try:
        connection.execute(
            text("SELECT pg_advisory_lock(:namespace, :source_id)"),
            {
                "namespace": _IMPORT_EXECUTION_LOCK_NAMESPACE,
                "source_id": source_id,
            },
        )
        # Session-level advisory locks survive COMMIT; end the SELECT's
        # transaction so external provider calls do not leave it idle/open.
        connection.commit()
    except Exception:
        connection.close()
        raise
    return connection


def _release_import_source_lock(connection, source_id: int) -> None:
    if connection is None:
        return
    try:
        connection.execute(
            text("SELECT pg_advisory_unlock(:namespace, :source_id)"),
            {
                "namespace": _IMPORT_EXECUTION_LOCK_NAMESPACE,
                "source_id": source_id,
            },
        )
        connection.commit()
    except Exception:
        # Never return a PostgreSQL session that might still own the lock to the
        # pool. Invalidating closes the backend connection and releases its lock.
        connection.invalidate()
    finally:
        connection.close()


class PlaylistImportAllFailed(RuntimeError):
    """Every remote playlist failed, so Celery should retry the provider call."""


def _claim_job_lease(db, job_id: int, task_id: str) -> None:
    claimed_id = db.scalar(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.running,
            or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
        )
        .values(lock_owner=task_id, heartbeat_at=utcnow())
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    if claimed_id is None:
        db.rollback()
        raise JobLeaseLost(f"Scan job {job_id} lease is no longer available")

    # Owning the process-level scanner lock proves that older owner markers are stale.
    db.execute(
        update(Job)
        .where(
            Job.id != job_id,
            Job.type == "scan_library",
            Job.lock_owner.is_not(None),
        )
        .values(lock_owner=None)
        .execution_options(synchronize_session=False)
    )
    db.commit()
    db.expire_all()


def _renew_job_lease(db, job_id: int, task_id: str, **values) -> None:
    values["heartbeat_at"] = utcnow()
    renewed_id = db.scalar(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.running,
            Job.lock_owner == task_id,
        )
        .values(**values)
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    if renewed_id is None:
        db.rollback()
        raise JobLeaseLost(f"Scan job {job_id} lease was revoked")
    db.commit()
    db.expire_all()


def _finish_job(db, job_id: int, task_id: str, payload: str) -> None:
    finished_id = db.scalar(
        update(Job)
        .where(
            Job.id == job_id,
            Job.status == JobStatus.running,
            Job.lock_owner == task_id,
        )
        .values(
            status=JobStatus.done,
            payload=payload,
            error=None,
            finished_at=utcnow(),
            heartbeat_at=utcnow(),
            lock_owner=None,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    )
    if finished_id is None:
        db.rollback()
        raise JobLeaseLost(f"Scan job {job_id} lease was revoked before completion")
    db.commit()
    db.expire_all()


@celery.task(
    bind=True,
    name="scan_library",
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def scan_library_task(self, job_id: int):
    db = SessionLocal()
    task_id = str(self.request.id or f"direct-{job_id}")[:64]
    try:
        job = db.get(Job, job_id)
        if job is None:
            raise ValueError(f"Scan job {job_id} does not exist")
        if job.status in (JobStatus.done, JobStatus.failed):
            return json.loads(job.payload or "{}")
        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.running,
                error=None,
                finished_at=None,
                heartbeat_at=utcnow(),
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if started_id is None:
            db.rollback()
            return {"status": "already_running", "job_id": job_id}
        db.commit()
        db.expire_all()

        last_scan_progress_update = 0.0

        def mark_lock_acquired():
            _claim_job_lease(db, job_id, task_id)

        def update_progress(summary):
            nonlocal last_scan_progress_update
            processed = (
                summary.added
                + summary.updated
                + summary.unchanged
                + summary.moved
                + summary.duplicate_content
                + summary.failed
            )
            now = monotonic()
            if (
                processed != summary.discovered
                and processed % 25 != 0
                and now - last_scan_progress_update < 1.0
            ):
                return
            last_scan_progress_update = now
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "path": settings.music_library_path,
                        "phase": "scanning",
                        "progress": {
                            "processed": processed,
                            "total": summary.discovered,
                        },
                        "scan": summary.to_dict(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        scan_summary = scan_library(
            db,
            settings.music_library_path,
            progress_callback=update_progress,
            lock_acquired_callback=mark_lock_acquired,
        )
        _renew_job_lease(db, job_id, task_id)
        musicbrainz_summary: dict = {"status": "disabled"}
        if settings.musicbrainz_enabled:
            if settings.musicbrainz_user_agent:
                try:
                    with MusicBrainzClient() as client:

                        def check_enrichment_lease():
                            _renew_job_lease(db, job_id, task_id)

                        def update_enrichment_progress(enrichment_summary):
                            processed = enrichment_summary["processed"]
                            if (
                                processed not in (1, enrichment_summary["total"])
                                and processed % 10 != 0
                            ):
                                return
                            _renew_job_lease(
                                db,
                                job_id,
                                task_id,
                                payload=json.dumps(
                                    {
                                        "path": settings.music_library_path,
                                        "phase": "enriching",
                                        "progress": {
                                            "processed": enrichment_summary[
                                                "processed"
                                            ],
                                            "total": enrichment_summary["total"],
                                        },
                                        "scan": scan_summary.to_dict(),
                                        "musicbrainz": enrichment_summary,
                                    },
                                    ensure_ascii=False,
                                    separators=(",", ":"),
                                ),
                            )

                        enrichment = enrich_albums(
                            db,
                            scan_summary.album_ids,
                            client,
                            before_album_callback=check_enrichment_lease,
                            progress_callback=update_enrichment_progress,
                        )
                        successful = enrichment["enriched"] + enrichment["not_found"]
                        enrichment_status = "completed"
                        if enrichment["failed"]:
                            enrichment_status = "partial" if successful else "failed"
                        musicbrainz_summary = {
                            "status": enrichment_status,
                            **enrichment,
                        }
                except JobLeaseLost:
                    raise
                except Exception as exc:
                    db.rollback()
                    musicbrainz_summary = {
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
            else:
                musicbrainz_summary = {
                    "status": "skipped",
                    "reason": "MUSICBRAINZ_USER_AGENT is not configured",
                }

        final_payload = json.dumps(
            {
                "path": settings.music_library_path,
                "phase": "completed",
                "progress": {
                    "processed": scan_summary.discovered,
                    "total": scan_summary.discovered,
                },
                "scan": scan_summary.to_dict(),
                "musicbrainz": musicbrainz_summary,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _finish_job(db, job_id, task_id, final_payload)
        return json.loads(final_payload)
    except JobLeaseLost:
        db.rollback()
        return {"status": "lease_lost", "job_id": job_id}
    except ScanAlreadyRunning:
        db.rollback()
        holder = db.scalar(
            select(Job)
            .where(Job.type == "scan_library", Job.lock_owner.is_not(None))
            .order_by(Job.heartbeat_at.desc())
        )
        if holder is not None and holder.id == job_id and holder.lock_owner == task_id:
            return {"status": "already_running", "job_id": job_id}

        retrying = self.request.retries < self.max_retries
        retry_values = {
            "status": JobStatus.pending if retrying else JobStatus.failed,
            "error": (
                "Waiting for the active library scan to finish"
                if retrying
                else "Another library scan did not finish before retry limit"
            ),
            "heartbeat_at": utcnow(),
            "finished_at": None if retrying else utcnow(),
            "lock_owner": None,
        }
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(**retry_values)
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if updated_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        if self.request.retries < self.max_retries:
            raise self.retry(
                exc=ScanAlreadyRunning("A library scan is already running"),
                countdown=min(300, 30 * (2**self.request.retries)),
            )
        return {"status": "lock_timeout", "job_id": job_id}
    except Exception as exc:
        db.rollback()
        retrying = self.request.retries < self.max_retries
        error = (
            f"Scan attempt {self.request.retries + 1} failed "
            f"({type(exc).__name__}); retry scheduled"
            if retrying
            else f"Scan failed ({type(exc).__name__})"
        )
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.pending if retrying else JobStatus.failed,
                error=error,
                finished_at=None if retrying else utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if updated_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        if retrying:
            raise self.retry(
                exc=exc,
                countdown=min(60, 2**self.request.retries),
            )
        raise
    finally:
        db.close()


def _import_summary_dict(summary) -> dict:
    if hasattr(summary, "to_dict"):
        return summary.to_dict()
    if hasattr(summary, "as_dict"):
        return summary.as_dict()
    if isinstance(summary, dict):
        return summary
    raise TypeError("Playlist importer returned an unsupported summary")


@celery.task(
    bind=True,
    name="import_playlists",
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def import_playlists_task(
    self,
    job_id: int,
    source_id: int,
    playlist_id: int | None = None,
):
    db = SessionLocal()
    task_id = str(self.request.id or f"direct-import-{job_id}")[:64]
    source_lock = None
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job.status in (JobStatus.done, JobStatus.failed):
            return json.loads(job.payload or "{}")

        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "import_playlists",
                Job.source_id == source_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.running,
                error=None,
                finished_at=None,
                heartbeat_at=utcnow(),
                lock_owner=task_id,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if started_id is None:
            db.rollback()
            return {"status": "already_running", "job_id": job_id}
        db.commit()
        db.expire_all()

        source_lock = _acquire_import_source_lock(db, source_id)
        # A replacement API request may revoke this job while it waits for an
        # older worker's source lock. Revalidate before any provider writes.
        _renew_job_lease(db, job_id, task_id)

        source = db.get(PlaylistSource, source_id)
        if source is None:
            raise ValueError(f"Playlist source {source_id} does not exist")
        playlist = None
        if playlist_id is not None:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None or playlist.source_id != source.id:
                raise ValueError("Playlist does not belong to the import source")

        if source.service == ServiceEnum.spotify:
            summary = (
                refresh_spotify_playlist(db, playlist)
                if playlist is not None
                else import_spotify_playlists(db, source)
            )
        elif source.service == ServiceEnum.yandex:
            summary = (
                refresh_yandex_playlist(db, playlist)
                if playlist is not None
                else import_yandex_playlists(db, source)
            )
        else:
            raise ValueError(f"Unsupported playlist source: {source.service}")

        summary_data = _import_summary_dict(summary)
        result_status = "completed"
        if summary_data.get("failed"):
            successful = sum(
                int(summary_data.get(key, 0) or 0)
                for key in ("imported", "created", "updated", "skipped", "unchanged")
            )
            result_status = "partial" if successful else "failed"
        if result_status == "failed":
            raise PlaylistImportAllFailed("All provider playlists failed")
        final_payload = json.dumps(
            {
                "phase": "completed",
                "source_id": source.id,
                "playlist_id": playlist_id,
                "service": source.service.value,
                "result_status": result_status,
                "import": summary_data,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        finished_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.source_id == source_id,
                Job.status == JobStatus.running,
                Job.lock_owner == task_id,
            )
            .values(
                status=JobStatus.done,
                payload=final_payload,
                error=None,
                finished_at=utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if finished_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        return json.loads(final_payload)
    except Exception as exc:
        db.rollback()
        retrying = self.request.retries < self.max_retries
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "import_playlists",
                Job.source_id == source_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.pending if retrying else JobStatus.failed,
                error=(
                    f"Import attempt {self.request.retries + 1} failed "
                    f"({type(exc).__name__}); retry scheduled"
                    if retrying
                    else f"Playlist import failed ({type(exc).__name__})"
                ),
                finished_at=None if retrying else utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if updated_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        if retrying:
            raise self.retry(
                exc=exc,
                countdown=min(120, 2**self.request.retries),
            )
        raise
    finally:
        _release_import_source_lock(source_lock, source_id)
        db.close()


@celery.task(
    bind=True,
    name="run_matching",
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def run_matching_task(
    self,
    job_id: int,
    playlist_id: int | None = None,
):
    db = SessionLocal()
    task_id = str(self.request.id or f"direct-matching-{job_id}")[:64]
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job.status in (JobStatus.done, JobStatus.failed):
            return json.loads(job.payload or "{}")

        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "run_matching",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.running,
                error=None,
                finished_at=None,
                heartbeat_at=utcnow(),
                lock_owner=task_id,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if started_id is None:
            db.rollback()
            return {"status": "already_running", "job_id": job_id}
        db.commit()
        db.expire_all()

        summary = run_matching(db, playlist_id=playlist_id)
        final_payload = json.dumps(
            {
                "phase": "completed",
                "playlist_id": playlist_id,
                "matching": summary.to_dict(),
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        finished_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status == JobStatus.running,
                Job.lock_owner == task_id,
            )
            .values(
                status=JobStatus.done,
                payload=final_payload,
                error=None,
                finished_at=utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if finished_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        return json.loads(final_payload)
    except Exception as exc:
        db.rollback()
        retrying = self.request.retries < self.max_retries
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "run_matching",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.pending if retrying else JobStatus.failed,
                error=(
                    f"Matching attempt {self.request.retries + 1} failed "
                    f"({type(exc).__name__}); retry scheduled"
                    if retrying
                    else f"Matching failed ({type(exc).__name__})"
                ),
                finished_at=None if retrying else utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if updated_id is None:
            db.rollback()
            return {"status": "lease_lost", "job_id": job_id}
        db.commit()
        if retrying:
            raise self.retry(
                exc=exc,
                countdown=min(60, 2**self.request.retries),
            )
        raise
    finally:
        db.close()
