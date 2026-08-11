import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf
from app.config import settings
from app.db import get_db
from app.models import (
    Job,
    JobScope,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    User,
    utcnow,
)
from app.schemas import (
    JobOut,
    MatchingRunIn,
    MatchResolveIn,
    MatchResolveOut,
    ReviewListOut,
)
from app.services.matcher import (
    MatchMethod,
    get_review_candidates,
    load_catalog,
)
from app.workers.tasks import run_matching_task

router = APIRouter(dependencies=[Depends(require_auth)])

_MATCH_QUEUE_LOCK_NAMESPACE = 20260808


def _payload_playlist_id(job: Job) -> int | None:
    try:
        payload = json.loads(job.payload or "{}")
    except (TypeError, ValueError):
        return None
    value = payload.get("playlist_id") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


def _active_job_covers(active_job: Job, playlist_id: int | None) -> bool:
    active_playlist_id = _payload_playlist_id(active_job)
    return active_playlist_id is None or active_playlist_id == playlist_id


def _find_active_job(db: Session, user_id: int) -> Job | None:
    return db.scalar(
        select(Job)
        .where(
            Job.type == "run_matching",
            Job.user_id == user_id,
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(
            case((Job.status == JobStatus.running, 0), else_=1),
            Job.created_at,
            Job.id,
        )
    )


def _reuse_or_conflict(
    db: Session,
    active_job: Job,
    user_id: int,
    playlist_id: int | None,
) -> Job:
    db.commit()
    db.refresh(active_job)
    if active_job.user_id == user_id and _active_job_covers(active_job, playlist_id):
        return active_job
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Another matching job is active",
    )


def _queue_matching_job(db: Session, user_id: int, playlist_id: int | None) -> Job:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :user_id)"),
            {"namespace": _MATCH_QUEUE_LOCK_NAMESPACE, "user_id": user_id},
        )

    now = utcnow()
    cutoff = now - timedelta(seconds=settings.matching_job_stale_seconds)
    db.execute(
        update(Job)
        .where(
            Job.type == "run_matching",
            Job.user_id == user_id,
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Matching job expired before completion",
            finished_at=now,
            heartbeat_at=now,
            lock_owner=None,
        )
        .execution_options(synchronize_session=False)
    )
    active_job = _find_active_job(db, user_id)
    if active_job is not None:
        return _reuse_or_conflict(db, active_job, user_id, playlist_id)

    job = Job(
        type="run_matching",
        playlist_id=playlist_id,
        user_id=user_id,
        scope=JobScope.user,
        status=JobStatus.pending,
        payload=json.dumps(
            {"playlist_id": playlist_id},
            separators=(",", ":"),
        ),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        active_job = _find_active_job(db, user_id)
        if active_job is None:
            raise
        return _reuse_or_conflict(db, active_job, user_id, playlist_id)
    db.refresh(job)
    try:
        run_matching_task.delay(job.id, user_id, playlist_id)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue matching job ({type(exc).__name__})"
        job.finished_at = utcnow()
        job.heartbeat_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Matching queue is unavailable"},
        ) from exc
    return job


@router.post(
    "/run",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def run_matching(
    payload: MatchingRunIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    if payload.playlist_id is not None and db.scalar(
        select(Playlist.id).where(
            Playlist.id == payload.playlist_id,
            Playlist.user_id == current_user.id,
        )
    ) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _queue_matching_job(db, current_user.id, payload.playlist_id)


@router.get("/review", response_model=ReviewListOut)
def review_queue(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    base = (
        select(Match, PlaylistItem, Playlist)
        .join(PlaylistItem, PlaylistItem.id == Match.playlist_item_id)
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(
            Match.status == MatchStatus.needs_review,
            Playlist.user_id == current_user.id,
        )
    )
    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    rows = db.execute(
        base.order_by(Playlist.updated_at.desc(), Playlist.id, PlaylistItem.position)
        .offset(offset)
        .limit(limit)
    ).all()
    catalog = load_catalog(db)
    items = []
    for match, item, playlist in rows:
        candidates = get_review_candidates(
            db,
            item,
            limit=5,
            catalog=catalog,
        )
        items.append(
            {
                "match_id": match.id,
                "playlist_id": playlist.id,
                "playlist_name": playlist.name,
                "playlist_item_id": item.id,
                "position": item.position,
                "artist_raw": item.artist_raw,
                "title_raw": item.title_raw,
                "album_raw": item.album_raw,
                "duration_ms": item.duration_ms,
                "confidence": float(match.confidence or 0.0),
                "candidates": candidates,
            }
        )
    return {"items": items, "total": total}


@router.post("/{match_id}/resolve", response_model=MatchResolveOut)
def resolve_match(
    match_id: int,
    payload: MatchResolveIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    match = db.scalar(
        select(Match)
        .join(PlaylistItem, PlaylistItem.id == Match.playlist_item_id)
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(Match.id == match_id, Playlist.user_id == current_user.id)
        .with_for_update()
    )
    if match is None:
        raise HTTPException(status_code=404, detail="Match not found")
    if match.status != MatchStatus.needs_review:
        raise HTTPException(status_code=409, detail="Match is not awaiting review")

    if payload.track_id is None:
        match.track_id = None
        match.confidence = 1.0
        match.method = MatchMethod.manual_missing.value
        match.status = MatchStatus.missing
    else:
        candidates = get_review_candidates(db, match.playlist_item, limit=20)
        candidate_ids = {candidate.track_id for candidate in candidates}
        if payload.track_id not in candidate_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Track is not a current review candidate",
            )
        match.track_id = payload.track_id
        match.confidence = 1.0
        match.method = MatchMethod.manual.value
        match.status = MatchStatus.ready
    db.commit()
    db.refresh(match)
    return {
        "match_id": match.id,
        "playlist_item_id": match.playlist_item_id,
        "track_id": match.track_id,
        "confidence": float(match.confidence or 0.0),
        "method": match.method,
        "status": match.status,
    }
