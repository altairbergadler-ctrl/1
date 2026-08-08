"""Bit-perfect file, archive and M3U8 preparation for Stage 4."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import zipstream
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Album, File as LibraryFile, MatchStatus, Playlist, PlaylistItem, Track

_INVALID_FILENAME = re.compile(r"[<>:\"/\\|?*\x00-\x1f]")
_REPEATED_WHITESPACE = re.compile(r"\s+")


class DeliveryError(RuntimeError):
    """Base class for controlled delivery failures."""


class DeliveryResourceNotFound(DeliveryError):
    pass


class DeliveryNotReady(DeliveryError):
    pass


class DeliveryFileUnavailable(DeliveryError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryEntry:
    path: Path
    filename: str
    artist: str
    title: str
    album: str
    duration_ms: int | None


def safe_filename(value: str, *, fallback: str = "download", limit: int = 180) -> str:
    cleaned = _INVALID_FILENAME.sub("_", value)
    cleaned = _REPEATED_WHITESPACE.sub(" ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:limit].rstrip(" .") or fallback


def _quality_key(file: LibraryFile) -> tuple[int, int, int, int]:
    return (
        int(file.bit_depth or 0),
        int(file.sample_rate or 0),
        int(file.size_bytes or 0),
        -int(file.id or 0),
    )


def _safe_library_path(value: str) -> Path:
    try:
        root = Path(settings.music_library_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise DeliveryFileUnavailable("Music library is unavailable") from exc

    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DeliveryFileUnavailable("Matched file is unavailable") from exc
    if not resolved.is_file() or not resolved.is_relative_to(root):
        raise DeliveryFileUnavailable("Matched file is unavailable")
    return resolved


def _best_playable_file(track: Track) -> tuple[LibraryFile, Path]:
    unavailable = False
    for file in sorted(track.files, key=_quality_key, reverse=True):
        try:
            return file, _safe_library_path(file.path)
        except DeliveryFileUnavailable:
            unavailable = True
    if unavailable or track.files:
        raise DeliveryFileUnavailable("Matched file is unavailable")
    raise DeliveryFileUnavailable("Matched track has no file")


def _entry_for_track(
    track: Track,
    *,
    filename_prefix: str | None = None,
) -> DeliveryEntry:
    file, path = _best_playable_file(track)
    artist = track.album.artist.name
    extension = path.suffix or (f".{file.format}" if file.format else "")
    stem = safe_filename(f"{artist} - {track.title}", fallback=f"track-{track.id}")
    filename = f"{stem}{extension.casefold()}"
    if filename_prefix:
        filename = f"{filename_prefix} - {filename}"
    return DeliveryEntry(
        path=path,
        filename=filename,
        artist=artist,
        title=track.title,
        album=track.album.title,
        duration_ms=track.duration_ms,
    )


def playlist_item_entry(db: Session, item_id: int) -> DeliveryEntry:
    item = db.get(PlaylistItem, item_id)
    if item is None:
        raise DeliveryResourceNotFound("Playlist item not found")
    if (
        item.match is None
        or item.match.status != MatchStatus.ready
        or item.match.track is None
    ):
        raise DeliveryNotReady("Playlist item is not ready")
    return _entry_for_track(item.match.track)


def playlist_entries(db: Session, playlist_id: int) -> tuple[Playlist, list[DeliveryEntry]]:
    playlist = db.get(Playlist, playlist_id)
    if playlist is None:
        raise DeliveryResourceNotFound("Playlist not found")
    items = db.scalars(
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist.id)
        .order_by(PlaylistItem.position, PlaylistItem.id)
    )
    entries: list[DeliveryEntry] = []
    for item in items:
        if item.match is None or item.match.status != MatchStatus.ready:
            continue
        if item.match.track is None:
            raise DeliveryFileUnavailable("Matched file is unavailable")
        entries.append(
            _entry_for_track(
                item.match.track,
                filename_prefix=f"{item.position + 1:03d}",
            )
        )
    if not entries:
        raise DeliveryNotReady("Playlist has no ready tracks")
    return playlist, entries


def album_entries(db: Session, album_id: int) -> tuple[Album, list[DeliveryEntry]]:
    album = db.get(Album, album_id)
    if album is None:
        raise DeliveryResourceNotFound("Album not found")
    tracks = list(
        db.scalars(
            select(Track)
            .where(Track.album_id == album.id)
            .order_by(Track.disc_no, Track.track_no, Track.title, Track.id)
        )
    )
    entries: list[DeliveryEntry] = []
    for index, track in enumerate(tracks, start=1):
        if not track.files:
            continue
        if track.track_no is not None:
            prefix = (
                f"{track.disc_no}-{track.track_no:02d}"
                if track.disc_no and track.disc_no > 1
                else f"{track.track_no:02d}"
            )
        else:
            prefix = f"{index:02d}"
        entries.append(_entry_for_track(track, filename_prefix=prefix))
    if not entries:
        raise DeliveryNotReady("Album has no playable files")
    return album, entries


def build_m3u8(entries: Iterable[DeliveryEntry]) -> str:
    lines = ["#EXTM3U"]
    for entry in entries:
        duration_seconds = (
            str(max(0, round(entry.duration_ms / 1000)))
            if entry.duration_ms is not None
            else "-1"
        )
        display = f"{entry.artist} - {entry.title}".replace("\n", " ").replace(
            "\r", " "
        )
        lines.extend([f"#EXTINF:{duration_seconds},{display}", entry.filename])
    return "\n".join(lines) + "\n"


def build_stored_zip(
    entries: Iterable[DeliveryEntry],
    *,
    m3u8: str | None = None,
):
    archive = zipstream.ZipFile(
        mode="w",
        compression=zipstream.ZIP_STORED,
        allowZip64=True,
    )
    for entry in entries:
        archive.write(str(entry.path), arcname=entry.filename)
    if m3u8 is not None:
        archive.writestr("playlist.m3u8", m3u8.encode("utf-8"))
    return archive
