"""Database-backed fair queue for provider-neutral acquisition workflows."""

from __future__ import annotations

import json

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Job, JobScope, JobStatus, Playlist, utcnow
from app.workers.celery_app import celery

_QUEUE_LOCK_NAMESPACE = 20260813


def _initial_payload(playlist_id: int, update_quality: bool) -> dict:
    return {
        "phase": "queued",
        "current_stage": "queued",
        "playlist_id": playlist_id,
        "update_quality": bool(update_quality),
        "batch_size": settings.acquisition_batch_size,
        "current_batch": 0,
        "batch_count": 0,
        "total_positions": 0,
        "processed_positions": 0,
        "downloaded_files": 0,
        "drive_uploaded_files": 0,
        "local_evicted_files": 0,
        "providers": {
            "qobuz": {"checked": 0, "available": 0, "selected": 0, "failed": 0},
            "yandex": {"checked": 0, "available": 0, "selected": 0, "failed": 0},
        },
    }


def _active_job(db: Session, playlist_id: int) -> Job | None:
    return db.scalar(
        select(Job)
        .where(
            Job.type == "acquisition_workflow",
            Job.playlist_id == playlist_id,
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(Job.created_at, Job.id)
    )


def queue_acquisition_job(
    db: Session,
    *,
    user_id: int,
    playlist_id: int,
    update_quality: bool = False,
) -> Job:
    """Create or reuse one active workflow for a user-owned playlist."""

    playlist = db.get(Playlist, playlist_id)
    if playlist is None or playlist.user_id != user_id:
        raise ValueError("Playlist does not belong to the acquisition owner")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :playlist_id)"),
            {"namespace": _QUEUE_LOCK_NAMESPACE, "playlist_id": playlist_id},
        )
    existing = _active_job(db, playlist_id)
    if existing is not None:
        db.commit()
        db.refresh(existing)
        return existing

    now = utcnow()
    job = Job(
        type="acquisition_workflow",
        playlist_id=playlist_id,
        user_id=user_id,
        scope=JobScope.user,
        status=JobStatus.pending,
        payload=json.dumps(
            _initial_payload(playlist_id, update_quality),
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        heartbeat_at=now,
        next_run_at=now,
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = _active_job(db, playlist_id)
        if existing is None:
            raise
        return existing
    db.refresh(job)
    kick_acquisition_dispatcher()
    return job


def queue_source_playlists(
    db: Session,
    *,
    user_id: int,
    source_id: int,
    playlist_id: int | None,
    update_quality: bool,
) -> list[Job]:
    statement = select(Playlist.id).where(
        Playlist.user_id == user_id,
        Playlist.source_id == source_id,
    )
    if playlist_id is not None:
        statement = statement.where(Playlist.id == playlist_id)
    jobs = []
    for candidate_id in db.scalars(statement.order_by(Playlist.id)):
        jobs.append(
            queue_acquisition_job(
                db,
                user_id=user_id,
                playlist_id=int(candidate_id),
                update_quality=update_quality,
            )
        )
    return jobs


def kick_acquisition_dispatcher(*, countdown: float | int = 0) -> None:
    if not settings.acquisition_enabled or settings.celery_task_always_eager:
        return
    try:
        celery.send_task(
            "acquisition_dispatch",
            countdown=max(0, float(countdown)),
        )
    except Exception:
        # Celery Beat is a durable fallback; an import must not fail after its
        # playlist transaction merely because this best-effort wake-up failed.
        return

