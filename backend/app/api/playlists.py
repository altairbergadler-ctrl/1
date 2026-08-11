import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.api.matching import _queue_matching_job
from app.config import settings
from app.db import get_db
from app.models import (
    Job,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    utcnow,
)
from app.schemas import (
    JobOut,
    PlaylistDetailOut,
    PlaylistContentImportIn,
    PlaylistContentImportOut,
    PlaylistImportIn,
    PlaylistItemsOut,
    PlaylistItemStatus,
    PlaylistListOut,
    PlaylistUrlImportIn,
)
from app.services.credentials import has_credential
from app.services.playlist_converter import (
    PlaylistConversionError,
    convert_playlist_content,
    save_converted_playlist,
)
from app.services.spotify import (
    SpotifyConfigurationError,
    canonical_spotify_playlist_url,
)
from app.workers.tasks import import_playlists_task

router = APIRouter(dependencies=[Depends(require_auth)])

_IMPORT_QUEUE_LOCK_NAMESPACE = 20260808


def _status_counts_subquery():
    return (
        select(
            PlaylistItem.playlist_id.label("playlist_id"),
            func.count(PlaylistItem.id).label("item_total"),
            func.count(case((Match.status == MatchStatus.ready, 1))).label("ready"),
            func.count(case((Match.status == MatchStatus.missing, 1))).label("missing"),
            func.count(case((Match.status == MatchStatus.needs_review, 1))).label(
                "review"
            ),
        )
        .outerjoin(Match, Match.playlist_item_id == PlaylistItem.id)
        .group_by(PlaylistItem.playlist_id)
        .subquery()
    )


def _playlist_query(playlist_id: int | None = None):
    counts = _status_counts_subquery()
    statement = (
        select(
            Playlist,
            PlaylistSource.service.label("service"),
            func.coalesce(counts.c.item_total, 0).label("item_total"),
            func.coalesce(counts.c.ready, 0).label("ready"),
            func.coalesce(counts.c.missing, 0).label("missing"),
            func.coalesce(counts.c.review, 0).label("review"),
        )
        .join(PlaylistSource, PlaylistSource.id == Playlist.source_id)
        .outerjoin(counts, counts.c.playlist_id == Playlist.id)
    )
    return (
        statement.where(Playlist.id == playlist_id)
        if playlist_id is not None
        else statement
    )


def _serialize_playlist(row) -> dict:
    playlist = row[0]
    total = int(row.item_total or 0)
    ready = int(row.ready or 0)
    missing = int(row.missing or 0)
    review = int(row.review or 0)
    unmatched = max(0, total - ready - missing - review)
    return {
        "id": playlist.id,
        "source_id": playlist.source_id,
        "service": row.service.value,
        "external_id": playlist.external_id,
        "name": playlist.name,
        "snapshot_hash": playlist.snapshot_hash,
        "track_count": total,
        "updated_at": playlist.updated_at,
        "summary": {
            "ready": ready,
            "missing": missing,
            "review": review,
            "unmatched": unmatched,
            "collected_percent": round((ready / total * 100) if total else 0.0, 2),
        },
    }


def _active_job_covers_request(
    job: Job,
    playlist_id: int | None,
    url: str | None,
) -> bool:
    try:
        payload = json.loads(job.payload or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or payload.get("source_id") != job.source_id:
        return False
    active_playlist_id = payload.get("playlist_id")
    active_url = payload.get("url")
    if url is not None:
        return active_url == url
    return active_url is None and (
        active_playlist_id is None or active_playlist_id == playlist_id
    )


def _reuse_active_job_or_raise(
    db: Session,
    active_job: Job,
    *,
    playlist_id: int | None,
    url: str | None,
) -> Job:
    db.commit()
    db.refresh(active_job)
    if _active_job_covers_request(active_job, playlist_id, url):
        return active_job
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "job_id": active_job.id,
            "message": "Another playlist import is active for this source",
        },
    )


def _queue_import_job(
    db: Session,
    source_id: int,
    *,
    playlist_id: int | None = None,
    url: str | None = None,
) -> Job:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :source_id)"),
            {
                "namespace": _IMPORT_QUEUE_LOCK_NAMESPACE,
                "source_id": source_id,
            },
        )

    now = utcnow()
    cutoff = now - timedelta(seconds=settings.playlist_import_job_stale_seconds)
    db.execute(
        update(Job)
        .where(
            Job.type == "import_playlists",
            Job.source_id == source_id,
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Playlist import job expired before completion",
            finished_at=now,
            heartbeat_at=now,
            lock_owner=None,
        )
        .execution_options(synchronize_session=False)
    )
    active_job = db.scalar(
        select(Job)
        .where(
            Job.type == "import_playlists",
            Job.source_id == source_id,
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(
            case((Job.status == JobStatus.running, 0), else_=1),
            Job.created_at,
            Job.id,
        )
    )
    if active_job is not None:
        return _reuse_active_job_or_raise(
            db,
            active_job,
            playlist_id=playlist_id,
            url=url,
        )

    payload = {"source_id": source_id}
    if playlist_id is not None:
        payload["playlist_id"] = playlist_id
    if url is not None:
        payload["url"] = url
    job = Job(
        type="import_playlists",
        source_id=source_id,
        status=JobStatus.pending,
        payload=json.dumps(payload, separators=(",", ":")),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    try:
        db.commit()
    except IntegrityError:
        # The partial UNIQUE index is the final guard if a caller bypassed or
        # raced the PostgreSQL advisory lock (and for concurrent SQLite tests).
        db.rollback()
        active_job = db.scalar(
            select(Job)
            .where(
                Job.type == "import_playlists",
                Job.source_id == source_id,
                Job.status.in_([JobStatus.pending, JobStatus.running]),
            )
            .order_by(Job.created_at, Job.id)
        )
        if active_job is None:
            raise
        return _reuse_active_job_or_raise(
            db,
            active_job,
            playlist_id=playlist_id,
            url=url,
        )
    db.refresh(job)
    try:
        if url is None:
            import_playlists_task.delay(job.id, source_id, playlist_id)
        else:
            import_playlists_task.delay(job.id, source_id, playlist_id, url)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue playlist import ({type(exc).__name__})"
        job.finished_at = utcnow()
        job.heartbeat_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Import queue is unavailable"},
        ) from exc
    return job


@router.post(
    "/import",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def import_playlists(payload: PlaylistImportIn, db: Session = Depends(get_db)):
    source = db.get(PlaylistSource, payload.source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="Playlist source not found")
    if source.service == ServiceEnum.manual:
        raise HTTPException(
            status_code=422,
            detail="Manual playlists must be imported from content",
        )
    return _queue_import_job(db, source.id)


@router.post(
    "/import-url",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def import_playlist_url(
    payload: PlaylistUrlImportIn,
    db: Session = Depends(get_db),
):
    try:
        url = canonical_spotify_playlist_url(payload.url)
    except SpotifyConfigurationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.spotify)
    )
    if source is None or not has_credential(db, ServiceEnum.spotify.value):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Connect Spotify before importing a playlist",
        )
    return _queue_import_job(db, source.id, url=url)


@router.post(
    "/import-content",
    response_model=PlaylistContentImportOut,
    status_code=status.HTTP_201_CREATED,
)
def import_playlist_content(
    payload: PlaylistContentImportIn,
    db: Session = Depends(get_db),
):
    try:
        converted = convert_playlist_content(payload.content, payload.format)
        playlist = save_converted_playlist(
            db,
            name=payload.name,
            content=payload.content,
            converted=converted,
        )
        matching_job = _queue_matching_job(db, playlist.id)
    except PlaylistConversionError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "playlist_id": playlist.id,
        "imported": len(converted.tracks),
        "skipped": converted.skipped,
        "format": converted.format,
        "matching_job": matching_job,
    }


@router.get("", response_model=PlaylistListOut)
def list_playlists(
    limit: int = Query(default=100, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    total = db.scalar(select(func.count(Playlist.id))) or 0
    rows = db.execute(
        _playlist_query()
        .order_by(Playlist.updated_at.desc(), Playlist.id)
        .offset(offset)
        .limit(limit)
    ).all()
    return {"items": [_serialize_playlist(row) for row in rows], "total": total}


@router.get("/{playlist_id}", response_model=PlaylistDetailOut)
def get_playlist(playlist_id: int, db: Session = Depends(get_db)):
    row = db.execute(_playlist_query(playlist_id)).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _serialize_playlist(row)


@router.get("/{playlist_id}/items", response_model=PlaylistItemsOut)
def get_playlist_items(
    playlist_id: int,
    item_status: PlaylistItemStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    if db.get(Playlist, playlist_id) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")

    statement = (
        select(PlaylistItem, Match)
        .outerjoin(Match, Match.playlist_item_id == PlaylistItem.id)
        .where(PlaylistItem.playlist_id == playlist_id)
    )
    if item_status == PlaylistItemStatus.unmatched:
        statement = statement.where(Match.id.is_(None))
    elif item_status is not None:
        statement = statement.where(Match.status == MatchStatus(item_status.value))

    total = db.scalar(select(func.count()).select_from(statement.subquery())) or 0
    rows = db.execute(
        statement.order_by(PlaylistItem.position).offset(offset).limit(limit)
    ).all()
    items = []
    for item, match in rows:
        items.append(
            {
                "id": item.id,
                "position": item.position,
                "artist_raw": item.artist_raw,
                "title_raw": item.title_raw,
                "album_raw": item.album_raw,
                "artist_norm": item.artist_norm,
                "title_norm": item.title_norm,
                "album_norm": item.album_norm,
                "isrc": item.isrc,
                "duration_ms": item.duration_ms,
                "external_track_id": item.external_track_id,
                "status": match.status.value if match else "UNMATCHED",
                "match_id": match.id if match else None,
                "track_id": match.track_id if match else None,
                "confidence": match.confidence if match else None,
                "method": match.method if match else None,
            }
        )
    return {"items": items, "total": total}


@router.post(
    "/{playlist_id}/refresh",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def refresh_playlist(playlist_id: int, db: Session = Depends(get_db)):
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    if playlist.source.service == ServiceEnum.manual:
        raise HTTPException(
            status_code=422,
            detail="Manual playlists are replaced by importing a new file",
        )
    return _queue_import_job(db, playlist.source_id, playlist_id=playlist.id)
