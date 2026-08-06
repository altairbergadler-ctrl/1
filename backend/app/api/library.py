import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import distinct, func, or_, select, text, update
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import settings
from app.db import get_db
from app.models import Album, Artist, File, Job, JobStatus, Track, utcnow
from app.schemas import JobOut, LibraryAlbumsOut, LibraryStatsOut
from app.workers.tasks import scan_library_task

router = APIRouter(dependencies=[Depends(require_auth)])


@router.post("/scan", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def scan(db: Session = Depends(get_db)):
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": 2026080602},
        )

    now = utcnow()
    cutoff = now - timedelta(seconds=settings.scan_job_stale_seconds)
    stale_job_ids = db.scalars(
        update(Job)
        .where(
            Job.type == "scan_library",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Scan job expired before completion",
            finished_at=now,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).all()

    active_job = db.scalar(
        select(Job)
        .where(
            Job.type == "scan_library",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(Job.created_at.desc())
    )
    if active_job is not None:
        if stale_job_ids:
            db.commit()
        return active_job

    job = Job(
        type="scan_library",
        status=JobStatus.pending,
        heartbeat_at=utcnow(),
        payload=json.dumps(
            {"path": settings.music_library_path},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        scan_library_task.delay(job.id)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue scan job ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Scan queue is unavailable"},
        ) from exc
    return job


@router.get("/stats", response_model=LibraryStatsOut)
def stats(db: Session = Depends(get_db)):
    files = db.scalar(select(func.count(File.id))) or 0
    tracks = db.scalar(select(func.count(distinct(File.track_id)))) or 0
    albums = (
        db.scalar(
            select(func.count(distinct(Track.album_id)))
            .select_from(File)
            .join(Track, Track.id == File.track_id)
        )
        or 0
    )
    size_bytes = db.scalar(select(func.coalesce(func.sum(File.size_bytes), 0))) or 0
    format_rows = db.execute(
        select(
            File.format,
            func.count(File.id),
            func.coalesce(func.sum(File.size_bytes), 0),
        )
        .group_by(File.format)
        .order_by(File.format)
    ).all()
    formats = {
        (file_format or "unknown"): {"files": count, "bytes": bytes_}
        for file_format, count, bytes_ in format_rows
    }
    return {
        "files": files,
        "tracks": tracks,
        "albums": albums,
        "bytes": size_bytes,
        "formats": formats,
    }


@router.get("/albums", response_model=LibraryAlbumsOut)
def albums(
    q: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    query = (
        select(
            Album.id,
            Album.title,
            Album.year,
            Album.mbid,
            Artist.id.label("artist_id"),
            Artist.name.label("artist_name"),
            Artist.mbid.label("artist_mbid"),
            func.count(distinct(Track.id)).label("track_count"),
            func.count(File.id).label("file_count"),
            func.coalesce(func.sum(File.size_bytes), 0).label("size_bytes"),
        )
        .join(Artist, Artist.id == Album.artist_id)
        .join(Track, Track.album_id == Album.id)
        .join(File, File.track_id == Track.id)
        .group_by(
            Album.id,
            Album.title,
            Album.year,
            Album.mbid,
            Artist.id,
            Artist.name,
            Artist.mbid,
        )
    )
    if q:
        pattern = f"%{q.strip()}%"
        query = query.where(or_(Album.title.ilike(pattern), Artist.name.ilike(pattern)))

    count_query = select(func.count()).select_from(query.order_by(None).subquery())
    total = db.scalar(count_query) or 0
    rows = db.execute(
        query.order_by(Artist.name, Album.year, Album.title).offset(offset).limit(limit)
    ).all()
    return {
        "items": [
            {
                "id": row.id,
                "title": row.title,
                "year": row.year,
                "mbid": row.mbid,
                "artist": {
                    "id": row.artist_id,
                    "name": row.artist_name,
                    "mbid": row.artist_mbid,
                },
                "tracks": row.track_count,
                "files": row.file_count,
                "bytes": row.size_bytes,
            }
            for row in rows
        ],
        "total": total,
    }
