from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.auth import require_auth
from app.db import get_db
from app.models import User
from app.services.delivery import (
    DeliveryFileUnavailable,
    DeliveryNotReady,
    DeliveryResourceNotFound,
    album_entries,
    build_m3u8,
    build_stored_zip,
    materialize_remote_entries,
    open_remote_download,
    playlist_entries,
    playlist_item_entry,
    safe_filename,
)

router = APIRouter(dependencies=[Depends(require_auth)])


@router.get("/track/{item_id}")
def download_track(
    item_id: int,
    request: Request,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        entry = playlist_item_entry(db, item_id, current_user.id)
    except DeliveryResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeliveryNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeliveryFileUnavailable as exc:
        raise HTTPException(status_code=409, detail="Matched file is unavailable") from exc
    if entry.path is not None:
        return FileResponse(
            entry.path,
            filename=entry.filename,
            media_type="application/octet-stream",
        )
    try:
        download = open_remote_download(
            db,
            entry,
            range_header=request.headers.get("range"),
        )
    except DeliveryFileUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Matched file is temporarily unavailable",
        ) from exc
    headers = {
        "Accept-Ranges": download.response.headers.get("Accept-Ranges", "bytes"),
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(entry.filename)}",
    }
    for name in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        value = download.response.headers.get(name)
        if value:
            headers[name] = value
    return StreamingResponse(
        download.response.iter_bytes(chunk_size=1024 * 1024),
        status_code=download.response.status_code,
        media_type=download.response.headers.get(
            "Content-Type", "application/octet-stream"
        ),
        headers=headers,
        background=BackgroundTask(download.close),
    )


@router.get("/album/{album_id}")
def download_album(
    album_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        album, entries = album_entries(db, album_id, current_user.id)
    except DeliveryResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeliveryNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeliveryFileUnavailable as exc:
        raise HTTPException(status_code=409, detail="Matched file is unavailable") from exc
    try:
        entries, cleanup = materialize_remote_entries(db, entries)
    except DeliveryFileUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Album files are temporarily unavailable",
        ) from exc
    filename = safe_filename(
        f"{album.artist.name} - {album.title}.zip",
        fallback=f"album-{album.id}.zip",
    )
    return StreamingResponse(
        build_stored_zip(entries),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
        background=BackgroundTask(cleanup) if cleanup is not None else None,
    )


@router.get("/playlist/{playlist_id}")
def download_playlist(
    playlist_id: int,
    mode: Literal["matched"] = "matched",
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    del mode
    try:
        playlist, entries = playlist_entries(db, playlist_id, current_user.id)
    except DeliveryResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeliveryNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeliveryFileUnavailable as exc:
        raise HTTPException(status_code=409, detail="Matched file is unavailable") from exc
    try:
        entries, cleanup = materialize_remote_entries(db, entries)
    except DeliveryFileUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="Playlist files are temporarily unavailable",
        ) from exc
    filename = safe_filename(
        f"{playlist.name}.zip",
        fallback=f"playlist-{playlist.id}.zip",
    )
    manifest = build_m3u8(entries)
    return StreamingResponse(
        build_stored_zip(entries, m3u8=manifest),
        media_type="application/zip",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
        background=BackgroundTask(cleanup) if cleanup is not None else None,
    )


@router.get("/playlist/{playlist_id}/m3u8")
def download_playlist_m3u8(
    playlist_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        playlist, entries = playlist_entries(db, playlist_id, current_user.id)
    except DeliveryResourceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except DeliveryNotReady as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DeliveryFileUnavailable as exc:
        raise HTTPException(status_code=409, detail="Matched file is unavailable") from exc
    filename = safe_filename(
        f"{playlist.name}.m3u8",
        fallback=f"playlist-{playlist.id}.m3u8",
    )
    return PlainTextResponse(
        build_m3u8(entries),
        media_type="audio/x-mpegurl; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}",
        },
    )
