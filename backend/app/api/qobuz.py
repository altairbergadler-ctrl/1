import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import settings
from app.db import get_db
from app.models import Job, JobStatus, Playlist, utcnow
from app.schemas import (
    JobOut,
    QobuzConnectOut,
    QobuzDownloadUrlIn,
    QobuzFetchMissingIn,
    QobuzSearchOut,
    QobuzStatusOut,
)
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    create_qobuz_client,
    is_qobuz_configured,
    search_albums,
    search_tracks,
)
from app.workers.tasks import qobuz_download_task

router = APIRouter(dependencies=[Depends(require_auth)])

_QOBUZ_QUEUE_LOCK_ID = 2026081005


def _credentials_present() -> bool:
    if str(settings.qobuz_auth_token or "").strip():
        return True
    return bool(
        str(settings.qobuz_email or "").strip() and settings.qobuz_password
    )


def _require_configured() -> None:
    if not is_qobuz_configured(settings):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qobuz is not configured",
        )


def _make_client():
    try:
        return create_qobuz_client()
    except QobuzConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qobuz is not configured",
        ) from exc
    except QobuzAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Qobuz credentials were rejected",
        ) from exc
    except QobuzProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Qobuz is unavailable",
        ) from exc


@router.get("/status", response_model=QobuzStatusOut)
def qobuz_status():
    return {
        "enabled": settings.qobuz_enabled,
        "configured": _credentials_present(),
        "quality": settings.qobuz_quality,
        "max_tracks_per_run": settings.qobuz_max_tracks_per_run,
    }


@router.post("/connect", response_model=QobuzConnectOut)
def qobuz_connect():
    _require_configured()
    client = _make_client()
    return {"connected": True, "label": getattr(client, "label", None)}


@router.get("/search", response_model=QobuzSearchOut)
def qobuz_search(
    q: str = Query(min_length=1, max_length=256),
    kind: str = Query(default="track", alias="type", pattern="^(track|album)$"),
    limit: int = Query(default=10, ge=1, le=50),
):
    _require_configured()
    client = _make_client()
    try:
        candidates = (
            search_tracks(client, q, limit)
            if kind == "track"
            else search_albums(client, q, limit)
        )
    except QobuzAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Qobuz credentials were rejected",
        ) from exc
    except QobuzProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Qobuz is unavailable",
        ) from exc
    return {
        "items": [
            {
                "kind": kind,
                "qobuz_id": candidate.qobuz_id,
                "artist": candidate.artist,
                "title": candidate.title,
                "album": candidate.album,
                "duration_ms": candidate.duration_ms,
                "isrc": candidate.isrc,
                "hires": candidate.hires,
                "url": candidate.url,
            }
            for candidate in candidates
        ]
    }


def _queue_qobuz_job(
    db: Session,
    *,
    mode: str,
    playlist_id: int | None = None,
    url: str | None = None,
) -> Job:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _QOBUZ_QUEUE_LOCK_ID},
        )

    now = utcnow()
    cutoff = now - timedelta(seconds=settings.qobuz_download_job_stale_seconds)
    stale_job_ids = db.scalars(
        update(Job)
        .where(
            Job.type == "qobuz_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Qobuz download job expired before completion",
            finished_at=now,
            lock_owner=None,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).all()

    active_job = db.scalar(
        select(Job)
        .where(
            Job.type == "qobuz_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(Job.created_at.desc())
    )
    if active_job is not None:
        if stale_job_ids:
            db.commit()
        return active_job

    job = Job(
        type="qobuz_download",
        status=JobStatus.pending,
        heartbeat_at=utcnow(),
        payload=json.dumps(
            {"mode": mode, "playlist_id": playlist_id, "url": url},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        qobuz_download_task.delay(job.id, mode, playlist_id, url)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue qobuz download job ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Qobuz download queue is unavailable"},
        ) from exc
    return job


@router.post(
    "/download-url",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def qobuz_download_url(payload: QobuzDownloadUrlIn, db: Session = Depends(get_db)):
    _require_configured()
    return _queue_qobuz_job(db, mode="url", url=payload.url.strip())


@router.post(
    "/fetch-missing",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def qobuz_fetch_missing(payload: QobuzFetchMissingIn, db: Session = Depends(get_db)):
    _require_configured()
    if db.get(Playlist, payload.playlist_id) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _queue_qobuz_job(db, mode="fetch_missing", playlist_id=payload.playlist_id)
