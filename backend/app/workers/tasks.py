import json
from time import monotonic

from sqlalchemy import or_, select, text, update

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Job,
    JobStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    StorageAccount,
    utcnow,
)
from app.services.musicbrainz import MusicBrainzClient, enrich_albums
from app.services.matcher import run_matching
from app.services.provider_health import run_provider_health_check
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    create_qobuz_client,
    download_url_to_staging,
    fetch_missing_tracks,
    import_files_to_library,
    mark_downloads_stored,
    record_qobuz_download_attempts,
)
from app.services.scanner import ScanAlreadyRunning, scan_library
from app.services.spotify import (
    SpotifyAccessDeniedError,
    import_spotify_playlist_url,
    import_spotify_playlists,
    refresh_spotify_playlist,
)
from app.services.storage import (
    cleanup_expired_storage_cache,
    health_check_account,
    migrate_local_library,
    replicate_imported_files,
)
from app.services.yandex import import_yandex_playlists, refresh_yandex_playlist
from app.services.yandex_acquisition import (
    YandexAcquisitionAuthError,
    YandexAcquisitionConfigurationError,
    create_yandex_acquisition_client,
    fetch_missing_yandex_tracks,
    record_yandex_download_attempts,
)
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
    url: str | None = None,
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

        try:
            requested_payload = json.loads(job.payload or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Playlist import job payload is invalid") from exc
        if not isinstance(requested_payload, dict) or requested_payload.get("url") != url:
            raise ValueError("Playlist import job URL does not match its payload")

        source_lock = _acquire_import_source_lock(db, source_id)
        # A replacement API request may revoke this job while it waits for an
        # older worker's source lock. Revalidate before any provider writes.
        _renew_job_lease(db, job_id, task_id)

        source = db.get(PlaylistSource, source_id)
        if source is None:
            raise ValueError(f"Playlist source {source_id} does not exist")
        if job.user_id is None or source.user_id != job.user_id:
            raise ValueError("Playlist import ownership does not match")
        playlist = None
        if playlist_id is not None:
            playlist = db.get(Playlist, playlist_id)
            if (
                playlist is None
                or playlist.source_id != source.id
                or playlist.user_id != job.user_id
            ):
                raise ValueError("Playlist does not belong to the import source")

        matching_summary = None
        if source.service == ServiceEnum.spotify:
            if url is not None:
                summary, playlist = import_spotify_playlist_url(db, url, source)
                playlist_id = playlist.id
                matching_summary = run_matching(
                    db, source.user_id, playlist_id=playlist_id
                )
            else:
                summary = (
                    refresh_spotify_playlist(db, playlist)
                    if playlist is not None
                    else import_spotify_playlists(db, source)
                )
                if summary.created or summary.updated:
                    matching_summary = run_matching(
                        db, source.user_id, playlist_id=playlist_id
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
            if (
                source.service == ServiceEnum.spotify
                and int(summary_data.get("restricted", 0) or 0)
                == int(summary_data.get("failed", 0) or 0)
            ):
                raise SpotifyAccessDeniedError(
                    "Spotify allows importing only owned or collaborative playlists"
                )
            raise PlaylistImportAllFailed("All provider playlists failed")
        final_payload = json.dumps(
            {
                "phase": "completed",
                "source_id": source.id,
                "playlist_id": playlist_id,
                "url": url,
                "service": source.service.value,
                "result_status": result_status,
                "import": summary_data,
                "matching": (
                    matching_summary.to_dict()
                    if matching_summary is not None
                    else None
                ),
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
        retrying = (
            not isinstance(exc, SpotifyAccessDeniedError)
            and self.request.retries < self.max_retries
        )
        if isinstance(exc, SpotifyAccessDeniedError):
            failure_message = (
                "Spotify allows importing only owned or collaborative playlists"
            )
        elif retrying:
            failure_message = (
                f"Import attempt {self.request.retries + 1} failed "
                f"({type(exc).__name__}); retry scheduled"
            )
        else:
            failure_message = f"Playlist import failed ({type(exc).__name__})"
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
                error=failure_message,
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
    user_id: int,
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
        if job.user_id != user_id:
            raise ValueError("Matching job ownership does not match")

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

        if playlist_id is not None:
            playlist = db.get(Playlist, playlist_id)
            if playlist is None or playlist.user_id != user_id:
                raise ValueError("Matching playlist ownership does not match")
        summary = run_matching(db, user_id, playlist_id=playlist_id)
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


@celery.task(
    bind=True,
    name="qobuz_download",
    # max_retries=2: временные сбои сети/Qobuz повторяем, но не вечно —
    # аккаунт дороже (rate-limit/бан, assessment section 7).
    # acks_late + reject_on_worker_lost: подтверждение после выполнения и
    # перевыход задачи при гибели worker'а — как у остальных тасок проекта.
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
)
def qobuz_download_task(
    self,
    job_id: int,
    mode: str,
    playlist_id: int | None = None,
    url: str | None = None,
):
    """Download from Qobuz to staging, verify, import, scan, then re-match."""

    # Задание выполняет полный pipeline (ограничения RESTRICT, assessment
    # §3.5 и §7):
    #   staging → верификация → перенос в библиотеку → scan → matching.
    # Скачанное НИКОГДА не пишется в MUSIC_LIBRARY_PATH напрямую: запись в
    # библиотеку делает только worker (в docker-compose rw-mount есть лишь у
    # него; backend работает read-only), только после верификации mutagen.
    #
    # Lease/heartbeat-паттерн скопирован с run_matching_task:
    #   - старт атомарно переводит job в running и назначает lock_owner;
    #   - _renew_job_lease обновляет heartbeat_at и payload-прогресс —
    #     именно по heartbeat API распознаёт зависшие задания (stale-cutoff);
    #   - если lease отобран (новый запрос погасил задание как stale),
    #     JobLeaseLost останавливает работу без записи в чужой job.
    db = SessionLocal()
    task_id = str(self.request.id or f"direct-qobuz-{job_id}")[:64]
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job.status in (JobStatus.done, JobStatus.failed):
            # Повторная доставка уже завершённой задачи (acks_late): просто
            # возвращаем сохранённый результат, ничего не выполняя заново.
            return json.loads(job.payload or "{}")
        if job.user_id is None:
            raise ValueError("Qobuz job has no owner")

        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "qobuz_download",
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
            # Задание уже взял другой worker или его погасили как stale —
            # этот экземпляр тихо завершается, ничего не выполняя.
            db.rollback()
            return {"status": "already_running", "job_id": job_id}
        db.commit()
        db.expire_all()

        client = create_qobuz_client(db)
        downloads: dict = {}
        collected_files: list = []
        if mode == "url":
            # Режим download-url: скачать один альбом/трек по ссылке.
            # Перед долгим сетевым этапом обновляем heartbeat и фазу, чтобы
            # задание не выглядело зависшим для stale-cutoff.
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {"phase": "downloading", "mode": mode, "url": url},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            collected_files = download_url_to_staging(
                client,
                url,
                settings.qobuz_staging_path,
                settings.qobuz_quality,
                settings.qobuz_embed_art,
            )
            downloads = {
                "url": url,
                "downloaded": len(collected_files),
                "files": [str(path) for path in collected_files],
            }
        elif mode == "fetch_missing":
            playlist = db.get(Playlist, playlist_id)
            if playlist is None or playlist.user_id != job.user_id:
                raise ValueError(f"Playlist {playlist_id} does not exist")

            def update_download_progress(summary):
                # Прогресс по каждому треку → heartbeat + payload. Это и
                # защита от stale-cutoff на долгих последовательных скачках
                # (лимит QOBUZ_MAX_TRACKS_PER_RUN), и живой прогресс для
                # фронтенда через GET /api/jobs/{id}.
                _renew_job_lease(
                    db,
                    job_id,
                    task_id,
                    payload=json.dumps(
                        {
                            "phase": (
                                "batch_pause"
                                if summary.get("batch_state") == "paused"
                                else "downloading"
                            ),
                            "mode": mode,
                            "playlist_id": playlist_id,
                            "downloads": summary,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                )

            downloads, collected_files = fetch_missing_tracks(
                db,
                playlist,
                client,
                progress_callback=update_download_progress,
                job_id=job_id,
            )
        else:
            raise ValueError(f"Unsupported qobuz download mode: {mode}")

        _renew_job_lease(
            db,
            job_id,
            task_id,
            payload=json.dumps(
                {
                    "phase": "importing",
                    "mode": mode,
                    "playlist_id": playlist_id,
                    "downloads": downloads,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        # Этап 2 pipeline: перенос верифицированного аудио из staging в
        # библиотеку. Существующие файлы не перезаписываются (конфликты
        # остаются в staging, попадают в отчёт), не-аудио отброшено ещё на
        # верификации. Запись идёт только здесь, в worker'е (assessment section 6).
        import_report = import_files_to_library(
            collected_files,
            settings.qobuz_staging_path,
            settings.music_library_path,
        )
        if mode == "fetch_missing":
            mark_downloads_stored(
                downloads,
                import_report,
                settings.qobuz_staging_path,
                settings.music_library_path,
            )
            item_ids = [
                int(entry["item_id"])
                for entry in downloads.get("items", [])
                if entry.get("item_id") is not None
            ]
            playlist_items = {
                item.id: item
                for item in db.scalars(
                    select(PlaylistItem).where(PlaylistItem.id.in_(item_ids))
                )
            }
            record_qobuz_download_attempts(
                db,
                downloads,
                playlist_items,
                job_id,
            )
        _renew_job_lease(
            db,
            job_id,
            task_id,
            payload=json.dumps(
                {
                    "phase": "scanning",
                    "mode": mode,
                    "playlist_id": playlist_id,
                    "downloads": downloads,
                    "import": import_report,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        def mark_scan_lock_acquired():
            # Скан захватил process-level блокировку библиотеки → подтверждаем
            # lease задания перед длинным этапом (тот же приём, что в
            # scan_library_task).
            _claim_job_lease(db, job_id, task_id)

        def update_scan_progress(summary):
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "scanning",
                        "mode": mode,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": summary.to_dict(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        try:
            # Этап 3 pipeline: инлайн-скан библиотеки, чтобы новые файлы
            # попали в каталог (треки/альбомы/Files) сразу, без ожидания
            # ручного /api/library/scan. Блокировка скана та же, что и у
            # обычной scan_library_task, — конкуренции двух сканов не будет.
            scan_summary = scan_library(
                db,
                settings.music_library_path,
                progress_callback=update_scan_progress,
                lock_acquired_callback=mark_scan_lock_acquired,
            )
            scan_payload: dict = {"status": "completed", **scan_summary.to_dict()}
        except ScanAlreadyRunning:
            # Другой скан уже идёт: задание НЕ падает. Файлы уже в библиотеке
            # и попадут в каталог тем сканом, поэтому помечаем этап deferred
            # и пропускаем matching (матчить по неполному каталогу нельзя).
            db.rollback()
            scan_payload = {
                "status": "deferred",
                "reason": "another library scan is already running",
            }

        storage_payload = None
        if scan_payload.get("status") == "completed":
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "replicating",
                        "mode": mode,
                        "playlist_id": playlist_id,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": scan_payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            storage_payload = replicate_imported_files(db, import_report)

        matching_payload = None
        if mode == "fetch_missing" and scan_payload.get("status") == "completed":
            # Этап 4 pipeline (только fetch-missing): повторный матчинг
            # плейлиста — скачанные треки должны перейти MISSING → READY.
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "matching",
                        "mode": mode,
                        "playlist_id": playlist_id,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": scan_payload,
                        "storage": storage_payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            matching_summary = run_matching(
                db, job.user_id, playlist_id=playlist_id
            )
            matching_payload = matching_summary.to_dict()

        final_payload = json.dumps(
            {
                "phase": "completed",
                "mode": mode,
                "playlist_id": playlist_id,
                "downloads": downloads,
                "import": import_report,
                "scan": scan_payload,
                "storage": storage_payload,
                "matching": matching_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _finish_job(db, job_id, task_id, final_payload)
        return json.loads(final_payload)
    except (QobuzConfigurationError, QobuzAuthError) as exc:
        # Credentials/configuration problems cannot be fixed by a retry.
        #
        # Ошибки конфигурации и авторизации завершают задание БЕЗ retry:
        # повтор с теми же неверными креденшелами/протухшим токеном ничего не
        # изменит, а лишние попытки логина рискуют вызвать временный бан
        # аккаунта (assessment section 7: риск rate-limit/бан Qobuz). В error
        # записываем только имя типа — без текстов, потенциально содержащих
        # данные ответа провайдера.
        db.rollback()
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.failed,
                error=f"Qobuz download failed ({type(exc).__name__})",
                finished_at=utcnow(),
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
        return {"status": "failed", "job_id": job_id, "error": type(exc).__name__}
    except JobLeaseLost:
        # Lease отобран (задание погасили как stale или заменили): тихо
        # останавливаемся, НЕ трогая чужой job — все дальнейшие записи
        # выполняет владелец актуального lease.
        db.rollback()
        return {"status": "lease_lost", "job_id": job_id}
    except Exception as exc:
        # Все прочие сбои (сеть, провайдер, диск) считаются потенциально
        # временными: стандартный retry-паттерн проекта — job возвращается в
        # pending, Celery повторяет с backoff countdown=min(120, 2**retries),
        # после исчерпания max_retries задание фиксируется failed.
        db.rollback()
        retrying = self.request.retries < self.max_retries
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "qobuz_download",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.pending if retrying else JobStatus.failed,
                error=(
                    f"Qobuz download attempt {self.request.retries + 1} failed "
                    f"({type(exc).__name__}); retry scheduled"
                    if retrying
                    else f"Qobuz download failed ({type(exc).__name__})"
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
        db.close()


@celery.task(name="provider_health_check")
def provider_health_check_task(provider: str, job_id: int | None = None):
    """Run a provider check in a worker and persist only non-sensitive results."""

    db = SessionLocal()
    try:
        job = db.get(Job, job_id) if job_id is not None else None
        if job_id is not None and job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job is not None:
            if job.status in (JobStatus.done, JobStatus.failed):
                return {"status": job.status.value, "job_id": job.id}
            job.status = JobStatus.running
            job.heartbeat_at = utcnow()
            db.commit()
        snapshot = run_provider_health_check(db, provider, worker_healthy=True)
        result = {
            "provider": snapshot["provider"],
            "configured": snapshot["configured"],
            "states": {
                component: snapshot[component]["state"]
                for component in ("account", "provider_api", "sidecar", "worker")
            },
        }
        if job is not None:
            job.status = JobStatus.done
            job.payload = json.dumps(result, separators=(",", ":"))
            job.error = None
            job.finished_at = utcnow()
            job.heartbeat_at = utcnow()
            db.commit()
        return result
    except Exception as exc:
        db.rollback()
        if job_id is not None:
            job = db.get(Job, job_id)
            if job is not None and job.status not in (JobStatus.done, JobStatus.failed):
                job.status = JobStatus.failed
                job.error = f"Provider health check failed ({type(exc).__name__})"
                job.finished_at = utcnow()
                job.heartbeat_at = utcnow()
                db.commit()
        return {"status": "failed", "provider": str(provider), "error": type(exc).__name__}
    finally:
        db.close()


@celery.task(name="storage_health_check")
def storage_health_check_task(
    job_id: int | None = None,
    account_id: int | None = None,
):
    """Check one or all Drive accounts without persisting credential material."""

    db = SessionLocal()
    try:
        job = db.get(Job, job_id) if job_id is not None else None
        if job_id is not None and job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job is not None:
            if job.status in (JobStatus.done, JobStatus.failed):
                return {"status": job.status.value, "job_id": job.id}
            job.status = JobStatus.running
            job.error = None
            job.heartbeat_at = utcnow()
            db.commit()

        query = select(StorageAccount).where(
            StorageAccount.provider == "google_drive"
        )
        if account_id is not None:
            query = query.where(StorageAccount.id == account_id)
        else:
            query = query.where(StorageAccount.enabled.is_(True))
        accounts = list(db.scalars(query.order_by(StorageAccount.id)))
        if account_id is not None and not accounts:
            raise ValueError("Storage account does not exist")

        items = []
        for account in accounts:
            checked = health_check_account(db, account)
            db.commit()
            items.append(
                {
                    "account_id": checked.id,
                    "state": checked.state,
                    "detail_code": checked.detail_code,
                }
            )
        result = {"provider": "google_drive", "items": items}
        if job is not None:
            job = db.get(Job, job.id)
            job.status = JobStatus.done
            job.payload = json.dumps(result, separators=(",", ":"))
            job.error = None
            job.finished_at = utcnow()
            job.heartbeat_at = utcnow()
            db.commit()
        return result
    except Exception as exc:
        db.rollback()
        if job_id is not None:
            job = db.get(Job, job_id)
            if job is not None and job.status not in (JobStatus.done, JobStatus.failed):
                job.status = JobStatus.failed
                job.error = f"Storage health check failed ({type(exc).__name__})"
                job.finished_at = utcnow()
                job.heartbeat_at = utcnow()
                db.commit()
        return {"status": "failed", "provider": "google_drive", "error": type(exc).__name__}
    finally:
        db.close()


@celery.task(
    bind=True,
    name="storage_migration",
    acks_late=True,
    reject_on_worker_lost=True,
)
def storage_migration_task(self, job_id: int):
    """Move local catalog files to Drive and evict verified local originals."""

    db = SessionLocal()
    task_id = str(self.request.id or f"direct-storage-{job_id}")[:64]
    try:
        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "storage_migration",
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
            job = db.get(Job, job_id)
            if job is None:
                return {"status": "missing_job", "job_id": job_id}
            return {"status": "already_finished", "job_id": job_id}
        db.commit()

        def update_progress(summary):
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {"phase": "moving", "storage": summary},
                    separators=(",", ":"),
                ),
            )

        summary = migrate_local_library(db, progress_callback=update_progress)
        result = {"phase": "completed", "storage": summary}
        _finish_job(
            db,
            job_id,
            task_id,
            json.dumps(result, separators=(",", ":")),
        )
        return result
    except JobLeaseLost:
        db.rollback()
        return {"status": "lease_lost", "job_id": job_id}
    except Exception as exc:
        db.rollback()
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "storage_migration",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.failed,
                error=f"Storage migration failed ({type(exc).__name__})",
                finished_at=utcnow(),
                heartbeat_at=utcnow(),
                lock_owner=None,
            )
            .returning(Job.id)
            .execution_options(synchronize_session=False)
        )
        if updated_id is not None:
            db.commit()
        else:
            db.rollback()
        return {"status": "failed", "job_id": job_id, "error": type(exc).__name__}
    finally:
        db.close()


@celery.task(name="storage_reconcile")
def storage_reconcile_task():
    """Retry pending uploads, evict durable local sources and sweep stale cache."""

    db = SessionLocal()
    try:
        cache = cleanup_expired_storage_cache()
        if settings.storage_primary_backend != "google_drive":
            return {"status": "local_primary", "cache": cache}
        healthy_account = db.scalar(
            select(StorageAccount.id).where(
                StorageAccount.provider == "google_drive",
                StorageAccount.enabled.is_(True),
                StorageAccount.state == "healthy",
            ).limit(1)
        )
        if healthy_account is None:
            return {"status": "not_configured", "cache": cache}
        return {
            "status": "completed",
            "storage": migrate_local_library(db),
            "cache": cache,
        }
    finally:
        db.close()


@celery.task(
    bind=True,
    name="yandex_download",
    max_retries=2,
    acks_late=True,
    reject_on_worker_lost=True,
)
def yandex_download_task(self, job_id: int, playlist_id: int):
    """Fetch missing tracks through yandex-music, import, scan and re-match."""

    db = SessionLocal()
    task_id = str(self.request.id or f"direct-yandex-{job_id}")[:64]
    try:
        job = db.get(Job, job_id)
        if job is None:
            return {"status": "missing_job", "job_id": job_id}
        if job.status in (JobStatus.done, JobStatus.failed):
            return json.loads(job.payload or "{}")
        if job.user_id is None:
            raise ValueError("Yandex job has no owner")
        started_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "yandex_download",
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

        playlist = db.get(Playlist, playlist_id)
        if playlist is None or playlist.user_id != job.user_id:
            raise ValueError(f"Playlist {playlist_id} does not exist")
        client = create_yandex_acquisition_client(db)

        def update_download_progress(summary):
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": (
                            "batch_pause"
                            if summary.get("batch_state") == "paused"
                            else "downloading"
                        ),
                        "playlist_id": playlist_id,
                        "downloads": summary,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        downloads, collected_files = fetch_missing_yandex_tracks(
            db,
            playlist,
            client,
            progress_callback=update_download_progress,
            job_id=job_id,
        )
        _renew_job_lease(
            db,
            job_id,
            task_id,
            payload=json.dumps(
                {
                    "phase": "importing",
                    "playlist_id": playlist_id,
                    "downloads": downloads,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        import_report = import_files_to_library(
            collected_files,
            settings.yandex_staging_path,
            settings.music_library_path,
        )
        mark_downloads_stored(
            downloads,
            import_report,
            settings.yandex_staging_path,
            settings.music_library_path,
        )
        item_ids = [
            int(entry["item_id"])
            for entry in downloads.get("items", [])
            if entry.get("item_id") is not None
        ]
        playlist_items = {
            item.id: item
            for item in db.scalars(
                select(PlaylistItem).where(PlaylistItem.id.in_(item_ids))
            )
        }
        record_yandex_download_attempts(db, downloads, playlist_items, job_id)

        _renew_job_lease(
            db,
            job_id,
            task_id,
            payload=json.dumps(
                {
                    "phase": "scanning",
                    "playlist_id": playlist_id,
                    "downloads": downloads,
                    "import": import_report,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )

        def mark_scan_lock_acquired():
            _claim_job_lease(db, job_id, task_id)

        def update_scan_progress(summary):
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "scanning",
                        "playlist_id": playlist_id,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": summary.to_dict(),
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        try:
            scan_summary = scan_library(
                db,
                settings.music_library_path,
                progress_callback=update_scan_progress,
                lock_acquired_callback=mark_scan_lock_acquired,
            )
            scan_payload: dict = {"status": "completed", **scan_summary.to_dict()}
        except ScanAlreadyRunning:
            db.rollback()
            scan_payload = {
                "status": "deferred",
                "reason": "another library scan is already running",
            }

        storage_payload = None
        if scan_payload.get("status") == "completed":
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "replicating",
                        "playlist_id": playlist_id,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": scan_payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            storage_payload = replicate_imported_files(db, import_report)

        matching_payload = None
        if scan_payload.get("status") == "completed":
            _renew_job_lease(
                db,
                job_id,
                task_id,
                payload=json.dumps(
                    {
                        "phase": "matching",
                        "playlist_id": playlist_id,
                        "downloads": downloads,
                        "import": import_report,
                        "scan": scan_payload,
                        "storage": storage_payload,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )
            matching_payload = run_matching(
                db, job.user_id, playlist_id=playlist_id
            ).to_dict()

        final_payload = json.dumps(
            {
                "phase": "completed",
                "playlist_id": playlist_id,
                "downloads": downloads,
                "import": import_report,
                "scan": scan_payload,
                "storage": storage_payload,
                "matching": matching_payload,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        _finish_job(db, job_id, task_id, final_payload)
        return json.loads(final_payload)
    except (YandexAcquisitionConfigurationError, YandexAcquisitionAuthError) as exc:
        db.rollback()
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "yandex_download",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.failed,
                error=f"Yandex download failed ({type(exc).__name__})",
                finished_at=utcnow(),
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
        return {"status": "failed", "job_id": job_id, "error": type(exc).__name__}
    except JobLeaseLost:
        db.rollback()
        return {"status": "lease_lost", "job_id": job_id}
    except Exception as exc:
        db.rollback()
        retrying = self.request.retries < self.max_retries
        updated_id = db.scalar(
            update(Job)
            .where(
                Job.id == job_id,
                Job.type == "yandex_download",
                Job.status.in_([JobStatus.pending, JobStatus.running]),
                or_(Job.lock_owner.is_(None), Job.lock_owner == task_id),
            )
            .values(
                status=JobStatus.pending if retrying else JobStatus.failed,
                error=(
                    f"Yandex download attempt {self.request.retries + 1} failed "
                    f"({type(exc).__name__}); retry scheduled"
                    if retrying
                    else f"Yandex download failed ({type(exc).__name__})"
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
            raise self.retry(exc=exc, countdown=min(120, 2**self.request.retries))
        raise
    finally:
        db.close()

