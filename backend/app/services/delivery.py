"""Bit-perfect file, archive and M3U8 preparation for Stage 4."""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterable

import zipstream
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Album, File as LibraryFile, MatchStatus, Playlist, PlaylistItem, Track
from app.services.google_drive import DriveDownload, GoogleDriveError
from app.services.storage import cleanup_expired_storage_cache, client_for_account

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
class RemoteDeliveryRef:
    account_id: int
    remote_file_id: str
    remote_name: str
    size_bytes: int
    sha1: str


@dataclass(frozen=True, slots=True)
class DeliveryEntry:
    path: Path | None
    filename: str
    artist: str
    title: str
    album: str
    duration_ms: int | None
    remote: RemoteDeliveryRef | None = None


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


def _safe_library_path(value: str | None) -> Path:
    if not value:
        raise DeliveryFileUnavailable("Matched file is unavailable")
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


def _remote_ref(file: LibraryFile) -> RemoteDeliveryRef | None:
    locations = [
        location
        for location in file.drive_locations
        if location.state == "healthy"
        and location.account.enabled
        and location.account.state == "healthy"
    ]
    locations.sort(
        key=lambda location: (
            int(location.account.priority),
            -int(location.account_id),
        ),
        reverse=True,
    )
    if not locations:
        return None
    location = locations[0]
    return RemoteDeliveryRef(
        account_id=int(location.account_id),
        remote_file_id=str(location.remote_file_id),
        remote_name=str(location.remote_name),
        size_bytes=int(location.size_bytes),
        sha1=str(location.sha1).casefold(),
    )


def _best_playable_file(
    track: Track,
) -> tuple[LibraryFile, Path | None, RemoteDeliveryRef | None]:
    unavailable = False
    for file in sorted(track.files, key=_quality_key, reverse=True):
        try:
            return file, _safe_library_path(file.path), _remote_ref(file)
        except DeliveryFileUnavailable:
            remote = _remote_ref(file)
            if remote is not None:
                return file, None, remote
            unavailable = True
    if unavailable or track.files:
        raise DeliveryFileUnavailable("Matched file is unavailable")
    raise DeliveryFileUnavailable("Matched track has no file")


def _entry_for_track(
    track: Track,
    *,
    filename_prefix: str | None = None,
) -> DeliveryEntry:
    file, path, remote = _best_playable_file(track)
    artist = track.album.artist.name
    remote_suffix = Path(remote.remote_name).suffix if remote is not None else ""
    extension = (
        (path.suffix if path is not None else "")
        or remote_suffix
        or (f".{file.format}" if file.format else "")
    )
    stem = safe_filename(f"{artist} - {track.title}", fallback=f"track-{track.id}")
    filename = f"{stem}{extension.casefold()}"
    if filename_prefix:
        filename = f"{filename_prefix} - {filename}"
    return DeliveryEntry(
        path=path,
        remote=remote,
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
        if entry.path is None:
            raise DeliveryFileUnavailable("Remote file was not materialized")
        archive.write(str(entry.path), arcname=entry.filename)
    if m3u8 is not None:
        archive.writestr("playlist.m3u8", m3u8.encode("utf-8"))
    return archive


def open_remote_download(
    db: Session,
    entry: DeliveryEntry,
    *,
    range_header: str | None = None,
) -> DriveDownload:
    if entry.remote is None:
        raise DeliveryFileUnavailable("Matched file is unavailable")
    from app.models import StorageAccount

    account = db.get(StorageAccount, entry.remote.account_id)
    if account is None or not account.enabled or account.state != "healthy":
        raise DeliveryFileUnavailable("Google Drive account is unavailable")
    try:
        return client_for_account(db, account).open_download(
            entry.remote.remote_file_id,
            range_header=range_header,
        )
    except GoogleDriveError as exc:
        raise DeliveryFileUnavailable("Google Drive download is unavailable") from exc


def materialize_remote_entries(
    db: Session,
    entries: Iterable[DeliveryEntry],
) -> tuple[list[DeliveryEntry], Callable[[], None] | None]:
    prepared = list(entries)
    remote_entries = [entry for entry in prepared if entry.path is None]
    if not remote_entries:
        return prepared, None

    required_bytes = sum(
        int(entry.remote.size_bytes)
        for entry in remote_entries
        if entry.remote is not None
    )
    if required_bytes > settings.storage_cache_max_bytes:
        raise DeliveryFileUnavailable("Download exceeds the temporary cache limit")

    cleanup_expired_storage_cache()
    cache_root = Path(settings.storage_cache_path).expanduser()
    try:
        cache_root.mkdir(parents=True, exist_ok=True)
        cache_root = cache_root.resolve(strict=True)
        if shutil.disk_usage(cache_root).free < required_bytes + 64 * 1024 * 1024:
            raise DeliveryFileUnavailable("Temporary cache has insufficient space")
        temporary = tempfile.TemporaryDirectory(prefix="audiofeel-", dir=cache_root)
    except OSError as exc:
        raise DeliveryFileUnavailable("Temporary cache is unavailable") from exc

    output: list[DeliveryEntry] = []
    try:
        for index, entry in enumerate(prepared):
            if entry.path is not None:
                output.append(entry)
                continue
            if entry.remote is None:
                raise DeliveryFileUnavailable("Matched file is unavailable")
            suffix = Path(entry.filename).suffix[:32]
            target = Path(temporary.name) / f"{index:06d}{suffix}"
            download = open_remote_download(db, entry)
            digest = hashlib.sha1()
            size = 0
            try:
                with target.open("xb") as destination:
                    for chunk in download.response.iter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        destination.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            except OSError as exc:
                raise DeliveryFileUnavailable("Temporary cache write failed") from exc
            finally:
                download.close()
            if (
                size != entry.remote.size_bytes
                or digest.hexdigest().casefold() != entry.remote.sha1
            ):
                raise DeliveryFileUnavailable("Google Drive file verification failed")
            output.append(replace(entry, path=target))
        return output, temporary.cleanup
    except Exception:
        temporary.cleanup()
        raise
