import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import settings
from app.db import get_db
from app.models import (
    Job,
    JobStatus,
    Playlist,
    PlaylistSource,
    ServiceEnum,
    utcnow,
)
from app.schemas import (
    JobOut,
    YandexDownloadEligibilityOut,
    YandexDownloadStatusOut,
    YandexFetchMissingIn,
)
from app.services.credentials import has_credential
from app.services.yandex_acquisition import (
    YANDEX_LOSSLESS_AVAILABLE,
    yandex_download_eligibility,
)
from app.workers.tasks import yandex_download_task

router = APIRouter(dependencies=[Depends(require_auth)])
_YANDEX_QUEUE_LOCK_ID = 2026081006


def _source_configured(db: Session) -> bool:
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.yandex)
    )
    return bool(
        source is not None
        and (has_credential(db, "yandex") or str(source.access_token or "").strip())
    )


def _signer_configured() -> bool:
    return len(settings.yandex_internal_token.strip()) >= 16


def _configured(db: Session) -> bool:
    return _source_configured(db) and _signer_configured()


def _require_configured(db: Session) -> None:
    if (
        not settings.yandex_download_enabled
        or not YANDEX_LOSSLESS_AVAILABLE
        or not _configured(db)
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Yandex lossless download is not available",
        )


@router.get("/status", response_model=YandexDownloadStatusOut)
def yandex_status(db: Session = Depends(get_db)):
    signer_available = YANDEX_LOSSLESS_AVAILABLE and _signer_configured()
    return {
        "enabled": settings.yandex_download_enabled and signer_available,
        "configured": _configured(db),
        "supported_codecs": ["flac", "aac", "mp3"] if signer_available else [],
        "lossless_supported": signer_available,
        "max_tracks_per_run": settings.yandex_max_tracks_per_run,
        "batch_delay_seconds": settings.yandex_batch_delay_seconds,
    }


def _payload_playlist_id(payload: str | None) -> int | None:
    try:
        data = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("playlist_id")
    if value is None and isinstance(data.get("downloads"), dict):
        value = data["downloads"].get("playlist_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/download-status/{playlist_id}", response_model=JobOut | None)
def yandex_download_status(playlist_id: int, db: Session = Depends(get_db)):
    if db.get(Playlist, playlist_id) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    jobs = db.scalars(
        select(Job)
        .where(Job.type == "yandex_download")
        .order_by(Job.created_at.desc(), Job.id.desc())
    )
    return next(
        (job for job in jobs if _payload_playlist_id(job.payload) == playlist_id),
        None,
    )


@router.get(
    "/download-eligibility/{playlist_id}",
    response_model=YandexDownloadEligibilityOut,
)
def yandex_eligibility(playlist_id: int, db: Session = Depends(get_db)):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return yandex_download_eligibility(db, playlist)


def _queue_yandex_job(db: Session, playlist_id: int) -> Job:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _YANDEX_QUEUE_LOCK_ID},
        )

    now = utcnow()
    cutoff = now - timedelta(seconds=settings.yandex_download_job_stale_seconds)
    stale_job_ids = db.scalars(
        update(Job)
        .where(
            Job.type == "yandex_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Yandex download job expired before completion",
            finished_at=now,
            lock_owner=None,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).all()
    active_job = db.scalar(
        select(Job)
        .where(
            Job.type == "yandex_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(Job.created_at.desc())
    )
    if active_job is not None:
        if _payload_playlist_id(active_job.payload) == playlist_id:
            if stale_job_ids:
                db.commit()
            return active_job
        if stale_job_ids:
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "job_id": active_job.id,
                "message": "Another Yandex download is already running",
            },
        )

    job = Job(
        type="yandex_download",
        status=JobStatus.pending,
        heartbeat_at=utcnow(),
        payload=json.dumps(
            {"playlist_id": playlist_id},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        yandex_download_task.delay(job.id, playlist_id)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue Yandex download job ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Yandex download queue is unavailable"},
        ) from exc
    return job


@router.post(
    "/fetch-missing",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def yandex_fetch_missing(
    payload: YandexFetchMissingIn,
    db: Session = Depends(get_db),
):
    _require_configured(db)
    if db.get(Playlist, payload.playlist_id) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _queue_yandex_job(db, payload.playlist_id)
