from __future__ import annotations

import hashlib
import os
import time
import warnings
from io import BytesIO
from pathlib import Path

from fastapi import Request
from fastapi.responses import Response
from mutagen import File as MutagenFile
from PIL import Image
from sqlalchemy.orm import Session

from app.config import settings
from app.opensubsonic.catalog import album_id, find_album, find_song, visible_tracks
from app.opensubsonic.protocol import OpenSubsonicError
from app.services.delivery import (
    DeliveryEntry,
    DeliveryFileUnavailable,
    _safe_library_path,
    best_playable_file,
    open_remote_download,
    playable_file_quality,
)


def _source_file(db: Session, user_id: int, public_id: str):
    if public_id.startswith("ca:al:"):
        album = find_album(db, user_id, "al:" + public_id.removeprefix("ca:al:"))
        if album is None:
            return None
        tracks = [track for track in visible_tracks(db, user_id) if track.album_id == album.id]
    elif public_id.startswith("ca:so:"):
        track = find_song(db, user_id, "so:" + public_id.removeprefix("ca:so:"))
        tracks = [track] if track is not None else []
    else:
        return None
    sources = []
    for track in tracks:
        try:
            sources.append(best_playable_file(track))
        except DeliveryFileUnavailable:
            continue
    return max(sources, key=lambda row: playable_file_quality(row[0]), default=None)


def _bounded_read(path: Path) -> bytes | None:
    try:
        size = path.stat().st_size
        if size <= 0 or size > settings.opensubsonic_artwork_max_input_bytes:
            return None
        return path.read_bytes()
    except OSError:
        return None


def _nearby(path: Path) -> bytes | None:
    names = {"cover", "folder", "front"}
    for candidate in sorted(path.parent.iterdir(), key=lambda row: row.name.casefold()):
        if candidate.stem.casefold() in names and candidate.suffix.casefold() in {
            ".jpg",
            ".jpeg",
            ".png",
            ".webp",
        }:
            try:
                contained = _safe_library_path(str(candidate))
            except DeliveryFileUnavailable:
                continue
            value = _bounded_read(contained)
            if value:
                return value
    return None


def _embedded(source) -> bytes | None:
    try:
        audio = MutagenFile(source)
    except Exception:
        return None
    if audio is None:
        return None
    pictures = getattr(audio, "pictures", None)
    if pictures:
        data = bytes(pictures[0].data)
        return data if len(data) <= settings.opensubsonic_artwork_max_input_bytes else None
    tags = getattr(audio, "tags", None)
    if tags is None:
        return None
    if hasattr(tags, "getall"):
        values = tags.getall("APIC")
        if values:
            data = bytes(values[0].data)
            return data if len(data) <= settings.opensubsonic_artwork_max_input_bytes else None
    covers = tags.get("covr") if hasattr(tags, "get") else None
    if covers:
        data = bytes(covers[0])
        return data if len(data) <= settings.opensubsonic_artwork_max_input_bytes else None
    return None


def _remote_embedded(db: Session, remote) -> bytes | None:
    # Drive-backed audio is never materialized just to discover artwork. A bounded prefix range
    # is enough for normal FLAC/ID3 metadata while keeping memory and network use predictable.
    limit = settings.opensubsonic_artwork_remote_prefix_bytes
    entry = DeliveryEntry(
        path=None,
        remote=remote,
        filename=remote.remote_name,
        artist="",
        title="",
        album="",
        duration_ms=None,
    )
    try:
        download = open_remote_download(
            db,
            entry,
            range_header=f"bytes=0-{limit - 1}",
        )
    except DeliveryFileUnavailable:
        return None
    data = bytearray()
    try:
        for chunk in download.response.iter_bytes(chunk_size=64 * 1024):
            remaining = limit - len(data)
            if remaining <= 0:
                break
            data.extend(chunk[:remaining])
            if len(data) >= limit:
                break
    except Exception:
        return None
    finally:
        download.close()
    return _embedded(BytesIO(data)) if data else None


def _cleanup_cache(root: Path) -> None:
    now = time.time()
    rows = []
    total = 0
    try:
        for path in root.glob("*.jpg"):
            stat = path.stat()
            if now - stat.st_mtime > settings.opensubsonic_artwork_cache_ttl_seconds:
                path.unlink(missing_ok=True)
                continue
            rows.append((stat.st_mtime, stat.st_size, path))
            total += stat.st_size
        for _mtime, size, path in sorted(rows):
            if total <= settings.opensubsonic_artwork_cache_max_bytes:
                break
            path.unlink(missing_ok=True)
            total -= size
    except OSError:
        return


def _render(data: bytes, source_sha1: str, requested_size: int) -> tuple[bytes, str]:
    cache_root = Path(settings.opensubsonic_artwork_cache_path).expanduser()
    cache_key = hashlib.sha256(
        f"v1:{source_sha1}:{requested_size}".encode()
    ).hexdigest()
    target = cache_root / f"{cache_key}.jpg"
    cached = _bounded_read(target) if target.exists() else None
    if cached:
        return cached, "image/jpeg"
    try:
        Image.MAX_IMAGE_PIXELS = settings.opensubsonic_artwork_max_pixels
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(data)) as image:
                image.load()
                if image.width * image.height > settings.opensubsonic_artwork_max_pixels:
                    raise ValueError("Artwork dimensions exceed the limit")
                image.thumbnail((requested_size, requested_size), Image.Resampling.LANCZOS)
                if image.mode not in {"RGB", "L"}:
                    canvas = Image.new("RGB", image.size, "white")
                    if "A" in image.getbands():
                        canvas.paste(image, mask=image.getchannel("A"))
                    else:
                        canvas.paste(image)
                    image = canvas
                elif image.mode == "L":
                    image = image.convert("RGB")
                output = BytesIO()
                image.save(output, format="JPEG", quality=90, optimize=True)
                rendered = output.getvalue()
    except Exception as exc:
        raise OpenSubsonicError(70, "Resource not found") from exc
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        temporary = cache_root / f".{cache_key}.{os.getpid()}.tmp"
        temporary.write_bytes(rendered)
        temporary.replace(target)
        _cleanup_cache(cache_root)
    except OSError:
        pass
    return rendered, "image/jpeg"


def cover_art_response(
    request: Request, db: Session, user_id: int, public_id: str
) -> Response:
    source = _source_file(db, user_id, public_id)
    if source is None:
        raise OpenSubsonicError(70, "Resource not found")
    _file, path, remote = source
    data = (_nearby(path) or _embedded(path)) if path is not None else None
    if not data and remote is not None:
        data = _remote_embedded(db, remote)
    if not data:
        raise OpenSubsonicError(70, "Resource not found")
    try:
        size = int(request.query_params.get("size", "512"))
    except ValueError as exc:
        raise OpenSubsonicError(0, "Invalid size") from exc
    if size < 1 or size > settings.opensubsonic_artwork_max_output_pixels:
        raise OpenSubsonicError(0, "Invalid size")
    rendered, media_type = _render(data, hashlib.sha256(data).hexdigest(), size)
    return Response(
        b"" if request.method == "HEAD" else rendered,
        media_type=media_type,
        headers={
            "Content-Length": str(len(rendered)),
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )
