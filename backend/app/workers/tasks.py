import json
from time import monotonic

from sqlalchemy import or_, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import Job, JobStatus, utcnow
from app.services.musicbrainz import MusicBrainzClient, enrich_albums
from app.services.scanner import ScanAlreadyRunning, scan_library
from app.workers.celery_app import celery


class JobLeaseLost(RuntimeError):
    """The scan task was superseded and must stop without changing its job."""


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


@celery.task(name="import_playlists")
def import_playlists_task(source_id: int):
    """TODO (Этап 3): вызвать services.spotify / services.yandex."""
    return {"status": "not_implemented", "source_id": source_id}


@celery.task(name="run_matching")
def run_matching_task(playlist_id: int | None = None):
    """TODO (Этап 4): вызвать services.matcher.run()."""
    return {"status": "not_implemented", "playlist_id": playlist_id}
