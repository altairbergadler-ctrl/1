"""User-scoped controls for the fair acquisition workflow queue."""

import json

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf
from app.db import get_db
from app.models import Job, JobStatus, User, utcnow
from app.schemas import AcquisitionQueueIn, JobOut
from app.services.acquisition_queue import (
    kick_acquisition_dispatcher,
    queue_acquisition_job,
)

router = APIRouter(dependencies=[Depends(require_auth)])


def _owned_job(db: Session, user_id: int, job_id: int) -> Job:
    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.type == "acquisition_workflow",
            Job.user_id == user_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Acquisition job not found")
    return job


@router.post(
    "/playlists/{playlist_id}",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_playlist(
    playlist_id: int,
    payload: AcquisitionQueueIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    try:
        return queue_acquisition_job(
            db,
            user_id=current_user.id,
            playlist_id=playlist_id,
            update_quality=payload.update_quality,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Playlist not found") from exc


@router.get("/playlists/{playlist_id}", response_model=JobOut | None)
def playlist_status(
    playlist_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    return db.scalar(
        select(Job)
        .where(
            Job.type == "acquisition_workflow",
            Job.playlist_id == playlist_id,
            Job.user_id == current_user.id,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
    )


@router.post(
    "/jobs/{job_id}/pause",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def pause_after_batch(
    job_id: int,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, current_user.id, job_id)
    if job.status in (JobStatus.done, JobStatus.failed):
        raise HTTPException(status_code=409, detail="Acquisition job is not active")
    now = utcnow()
    if job.status == JobStatus.running:
        job.pause_requested_at = now
    else:
        job.paused_at = now
        job.next_run_at = None
        try:
            payload = json.loads(job.payload or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        payload["phase"] = "paused"
        payload["current_stage"] = "paused"
        payload["pause_reason"] = "manual"
        job.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    job.heartbeat_at = now
    db.commit()
    db.refresh(job)
    return job


@router.post(
    "/jobs/{job_id}/resume",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def resume(
    job_id: int,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    job = _owned_job(db, current_user.id, job_id)
    if job.status != JobStatus.pending or job.paused_at is None:
        raise HTTPException(status_code=409, detail="Acquisition job is not paused")
    try:
        payload = json.loads(job.payload or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    payload["phase"] = "queued"
    payload["current_stage"] = "queued"
    payload.pop("pause_reason", None)
    now = utcnow()
    job.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    job.paused_at = None
    job.pause_requested_at = None
    job.next_run_at = now
    job.heartbeat_at = now
    db.commit()
    db.refresh(job)
    kick_acquisition_dispatcher()
    return job

