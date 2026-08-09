from __future__ import annotations

import hashlib
import os
import re
import unicodedata
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from mutagen import File as MutagenFile
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Album,
    Artist,
    File as LibraryFile,
    Match,
    MatchStatus,
    Track,
    utcnow,
)
from app.services.normalize import (
    normalize_album,
    normalize_artist,
    normalize_text,
    normalize_title,
)

SUPPORTED_EXTENSIONS = frozenset({".flac", ".alac", ".wav", ".dsf", ".dff", ".ape"})
_ALBUM_RE = re.compile(
    r"^(?P<title>.+?)(?:\s*\((?P<year>(?:19|20)\d{2})\))?"
    r"(?:\s*\[[^\]]+\])?$"
)
_TRACK_RE = re.compile(
    r"^(?:(?P<disc>\d+)[.-])?(?P<track>\d{1,3})\s*[-–—]\s*"
    r"(?P<title>.+)$"
)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE_RE = re.compile(r"\s+")
_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")


class ScannerError(RuntimeError):
    """Base error for a library scan."""


class LibraryPathError(ScannerError):
    """The configured library path cannot be scanned."""


class MetadataReadError(ScannerError):
    """An audio file could not be parsed by Mutagen."""


class ScanAlreadyRunning(ScannerError):
    """Another worker already owns the PostgreSQL library scan lock."""


@dataclass(slots=True, frozen=True)
class PathFallback:
    artist: str
    album: str
    title: str
    year: int | None
    track_no: int | None
    disc_no: int | None


@dataclass(slots=True, frozen=True)
class AudioMetadata:
    path: str
    sha1: str
    format: str
    size_bytes: int
    bit_depth: int | None
    sample_rate: int | None
    duration_ms: int | None
    artist: str
    album: str
    title: str
    year: int | None
    track_no: int | None
    disc_no: int | None
    isrc: str | None
    artist_mbid: str | None
    album_mbid: str | None
    track_mbid: str | None


@dataclass(slots=True, frozen=True)
class ScanIssue:
    path: str
    error: str


@dataclass(slots=True)
class ScanSummary:
    discovered: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    moved: int = 0
    duplicate_content: int = 0
    removed: int = 0
    failed: int = 0
    album_ids: list[int] = field(default_factory=list)
    issues: list[ScanIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_catalog_key(value: str) -> str:
    """Backward-compatible generic entry point for catalog comparisons."""

    return normalize_text(value)


def clean_tag_text(value: Any, *, limit: int = 512) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = _CONTROL_RE.sub(" ", text)
    return _SPACE_RE.sub(" ", text).strip()[:limit]


def iter_audio_files(root: str | Path) -> Iterable[Path]:
    root_path = Path(root).expanduser().resolve()
    if not root_path.exists():
        raise LibraryPathError(f"Music library path does not exist: {root_path}")
    if not root_path.is_dir():
        raise LibraryPathError(f"Music library path is not a directory: {root_path}")

    def raise_walk_error(error: OSError) -> None:
        raise LibraryPathError(f"Cannot traverse music library: {error}") from error

    for current_root, directory_names, file_names in os.walk(
        root_path,
        followlinks=False,
        onerror=raise_walk_error,
    ):
        directory_names.sort(key=str.casefold)
        for file_name in sorted(file_names, key=str.casefold):
            path = Path(current_root, file_name)
            if (
                not path.is_symlink()
                and path.resolve().is_relative_to(root_path)
                and path.suffix.casefold() in SUPPORTED_EXTENSIONS
            ):
                yield path


def sha1_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha1()
    with Path(path).open("rb") as source:
        while chunk := source.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def parse_path_fallback(path: str | Path, root: str | Path) -> PathFallback:
    file_path = Path(path)
    root_path = Path(root)
    try:
        relative = file_path.relative_to(root_path)
    except ValueError:
        relative = file_path

    parts = relative.parts
    artist = clean_tag_text(parts[-3]) if len(parts) >= 3 else "Unknown Artist"
    album_folder = clean_tag_text(parts[-2]) if len(parts) >= 2 else "Unknown Album"

    album_match = _ALBUM_RE.match(album_folder)
    if album_match:
        album = clean_tag_text(album_match.group("title")) or "Unknown Album"
        year_text = album_match.group("year")
        year = int(year_text) if year_text else None
    else:
        album = album_folder or "Unknown Album"
        year = None

    track_match = _TRACK_RE.match(file_path.stem)
    if track_match:
        title = clean_tag_text(track_match.group("title")) or file_path.stem
        track_no = int(track_match.group("track"))
        disc_text = track_match.group("disc")
        disc_no = int(disc_text) if disc_text else None
    else:
        title = clean_tag_text(file_path.stem) or "Unknown Track"
        track_no = None
        disc_no = None

    return PathFallback(
        artist=artist or "Unknown Artist",
        album=album or "Unknown Album",
        title=title,
        year=year,
        track_no=track_no,
        disc_no=disc_no,
    )


def _normalized_tag_name(name: Any) -> str:
    return re.sub(r"[\s_-]+", "", str(name).casefold())


def _first_scalar(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "text"):
        return _first_scalar(value.text)
    if hasattr(value, "data"):
        return _first_scalar(value.data)
    if isinstance(value, bytes):
        for encoding in ("utf-8", "utf-16", "latin-1"):
            try:
                return value.decode(encoding).strip("\x00")
            except UnicodeDecodeError:
                continue
        return None
    if isinstance(value, (list, tuple)):
        return _first_scalar(value[0]) if value else None
    return value


def _tag_value(tags: Mapping[str, Any] | None, aliases: Iterable[str]) -> Any:
    if not tags:
        return None

    normalized_aliases = [_normalized_tag_name(alias) for alias in aliases]
    for alias in aliases:
        try:
            value = tags.get(alias)
        except (AttributeError, KeyError, TypeError, ValueError):
            value = None
        if value is not None:
            scalar = _first_scalar(value)
            if scalar is not None and not (
                isinstance(scalar, str) and not scalar.strip()
            ):
                return scalar

    try:
        keys = list(tags.keys())
    except AttributeError:
        return None
    for key in keys:
        normalized_key = _normalized_tag_name(key)
        if any(
            normalized_key == alias or normalized_key.endswith(alias)
            for alias in normalized_aliases
        ):
            try:
                scalar = _first_scalar(tags[key])
                if scalar is not None and not (
                    isinstance(scalar, str) and not scalar.strip()
                ):
                    return scalar
            except (KeyError, TypeError, ValueError):
                continue
    return None


def _text_tag(
    easy_tags: Mapping[str, Any] | None,
    raw_tags: Mapping[str, Any] | None,
    aliases: Iterable[str],
) -> str:
    value = _tag_value(easy_tags, aliases)
    if value is None:
        value = _tag_value(raw_tags, aliases)
    return clean_tag_text(value)


def _parse_index(value: Any) -> int | None:
    scalar = _first_scalar(value)
    if scalar is None:
        return None
    if isinstance(scalar, int):
        return scalar if scalar > 0 else None
    match = re.match(r"\s*(\d+)", str(scalar))
    if not match:
        return None
    number = int(match.group(1))
    return number if number > 0 else None


def _parse_year(value: Any) -> int | None:
    scalar = _first_scalar(value)
    if scalar is None:
        return None
    match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", str(scalar))
    return int(match.group(1)) if match else None


def _normalize_isrc(value: Any) -> str | None:
    scalar = _first_scalar(value)
    if scalar is None:
        return None
    candidate = re.sub(r"[^A-Za-z0-9]", "", str(scalar)).upper()
    return candidate if _ISRC_RE.fullmatch(candidate) else None


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _validate_container(path: Path, audio: Any) -> None:
    expected_types = {
        ".flac": {"FLAC"},
        ".alac": {"MP4"},
        ".wav": {"WAVE"},
        ".dsf": {"DSF"},
        ".dff": {"DSDIFF"},
        ".ape": {"MonkeysAudio"},
    }
    class_name = type(audio).__name__
    if class_name not in expected_types[path.suffix.casefold()]:
        raise MetadataReadError(
            f"File content ({class_name}) does not match {path.suffix} extension"
        )
    if path.suffix.casefold() == ".alac":
        codec = str(getattr(getattr(audio, "info", None), "codec", "")).casefold()
        if codec and codec != "alac":
            raise MetadataReadError("The .alac file does not contain the ALAC codec")


def read_audio_metadata(
    path: str | Path,
    root: str | Path,
    *,
    sha1: str | None = None,
) -> AudioMetadata:
    file_path = Path(path).resolve()
    root_path = Path(root).resolve()
    if not file_path.is_relative_to(root_path):
        raise LibraryPathError(f"Audio file is outside the library root: {file_path}")
    fallback = parse_path_fallback(file_path, root_path)

    try:
        audio_easy = MutagenFile(file_path, easy=True)
        audio_raw = MutagenFile(file_path, easy=False)
    except Exception as exc:
        raise MetadataReadError(f"Mutagen could not read metadata: {exc}") from exc
    audio = audio_raw or audio_easy
    if audio is None:
        raise MetadataReadError("Mutagen did not recognize the audio container")
    _validate_container(file_path, audio)

    easy_tags = getattr(audio_easy, "tags", None)
    raw_tags = getattr(audio_raw, "tags", None)
    info = getattr(audio, "info", None)

    artist = _text_tag(
        easy_tags,
        raw_tags,
        ("albumartist", "album artist", "artist", "TPE2", "TPE1", "aART", "©ART"),
    )
    album = _text_tag(easy_tags, raw_tags, ("album", "TALB", "©alb"))
    title = _text_tag(easy_tags, raw_tags, ("title", "TIT2", "©nam"))
    date_value = _tag_value(easy_tags, ("date", "year"))
    if date_value is None:
        date_value = _tag_value(raw_tags, ("date", "year", "TDRC", "TYER", "©day"))
    track_value = _tag_value(easy_tags, ("tracknumber", "track"))
    if track_value is None:
        track_value = _tag_value(raw_tags, ("tracknumber", "track", "TRCK", "trkn"))
    disc_value = _tag_value(easy_tags, ("discnumber", "disc"))
    if disc_value is None:
        disc_value = _tag_value(raw_tags, ("discnumber", "disc", "TPOS", "disk"))

    isrc_value = _tag_value(easy_tags, ("isrc",))
    if isrc_value is None:
        isrc_value = _tag_value(raw_tags, ("isrc", "TSRC"))

    artist_mbid = (
        _text_tag(
            easy_tags,
            raw_tags,
            ("musicbrainz_artistid", "musicbrainz artist id"),
        )
        or None
    )
    album_mbid = (
        _text_tag(
            easy_tags,
            raw_tags,
            ("musicbrainz_albumid", "musicbrainz album id"),
        )
        or None
    )
    track_mbid = (
        _text_tag(
            easy_tags,
            raw_tags,
            (
                "musicbrainz_trackid",
                "musicbrainz recording id",
                "ufid:http://musicbrainz.org",
            ),
        )
        or None
    )

    length = getattr(info, "length", None)
    duration_ms = None
    if isinstance(length, (int, float)) and length >= 0:
        duration_ms = round(length * 1000)

    bit_depth = _positive_int(
        getattr(info, "bits_per_sample", None)
        or getattr(info, "bit_depth", None)
        or getattr(info, "sample_size", None)
    )
    if file_path.suffix.casefold() in {".dsf", ".dff"} and bit_depth is None:
        bit_depth = 1

    return AudioMetadata(
        path=str(file_path),
        sha1=sha1 or sha1_file(file_path),
        format=file_path.suffix.casefold().lstrip("."),
        size_bytes=file_path.stat().st_size,
        bit_depth=bit_depth,
        sample_rate=_positive_int(getattr(info, "sample_rate", None)),
        duration_ms=duration_ms,
        artist=artist or fallback.artist,
        album=album or fallback.album,
        title=title or fallback.title,
        year=_parse_year(date_value) or fallback.year,
        track_no=_parse_index(track_value) or fallback.track_no,
        disc_no=_parse_index(disc_value) or fallback.disc_no,
        isrc=_normalize_isrc(isrc_value),
        artist_mbid=artist_mbid,
        album_mbid=album_mbid,
        track_mbid=track_mbid,
    )


def _get_or_create_artist(db: Session, metadata: AudioMetadata) -> Artist:
    name_norm = normalize_artist(metadata.artist)
    artist = db.scalar(select(Artist).where(Artist.name_norm == name_norm))
    if artist is None:
        artist = Artist(
            name=metadata.artist,
            name_norm=name_norm,
            mbid=metadata.artist_mbid,
        )
        db.add(artist)
        db.flush()
    elif metadata.artist_mbid and artist.mbid != metadata.artist_mbid:
        artist.mbid = metadata.artist_mbid
    return artist


def _get_or_create_album(db: Session, artist: Artist, metadata: AudioMetadata) -> Album:
    title_norm = normalize_album(metadata.album)
    statement = select(Album).where(
        Album.artist_id == artist.id,
        Album.title_norm == title_norm,
    )
    statement = (
        statement.where(Album.year.is_(None))
        if metadata.year is None
        else statement.where(Album.year == metadata.year)
    )
    album = db.scalar(statement)
    if album is None:
        album = Album(
            artist=artist,
            title=metadata.album,
            title_norm=title_norm,
            year=metadata.year,
            mbid=metadata.album_mbid,
        )
        db.add(album)
        db.flush()
    elif metadata.album_mbid and album.mbid != metadata.album_mbid:
        album.mbid = metadata.album_mbid
    return album


def _nullable_equals(column: Any, value: int | None) -> Any:
    return column.is_(None) if value is None else column == value


def _get_or_create_track(db: Session, album: Album, metadata: AudioMetadata) -> Track:
    title_norm = normalize_title(metadata.title)
    track = db.scalar(
        select(Track).where(
            Track.album_id == album.id,
            Track.title_norm == title_norm,
            _nullable_equals(Track.track_no, metadata.track_no),
            _nullable_equals(Track.disc_no, metadata.disc_no),
        )
    )
    if track is None:
        track = Track(
            album=album,
            title=metadata.title,
            title_norm=title_norm,
            track_no=metadata.track_no,
            disc_no=metadata.disc_no,
            duration_ms=metadata.duration_ms,
            isrc=metadata.isrc,
            mbid=metadata.track_mbid,
        )
        db.add(track)
        db.flush()
    else:
        if track.duration_ms is None and metadata.duration_ms is not None:
            track.duration_ms = metadata.duration_ms
        if metadata.isrc and track.isrc != metadata.isrc:
            track.isrc = metadata.isrc
        if metadata.track_mbid and track.mbid != metadata.track_mbid:
            track.mbid = metadata.track_mbid
    return track


def _apply_replacement_metadata(track: Track, metadata: AudioMetadata) -> None:
    track.duration_ms = metadata.duration_ms
    track.isrc = metadata.isrc
    track.mbid = metadata.track_mbid


def _carry_catalog_enrichment(old_track: Track, new_track: Track) -> None:
    """Preserve enrichment when normalization relinks the same audio content."""

    old_album = old_track.album
    new_album = new_track.album
    if new_album.artist.mbid is None:
        new_album.artist.mbid = old_album.artist.mbid
    if new_album.mbid is None:
        new_album.mbid = old_album.mbid
    if new_track.isrc is None:
        new_track.isrc = old_track.isrc
    if new_track.mbid is None:
        new_track.mbid = old_track.mbid


def _prune_orphaned_track(db: Session, track_id: int | None) -> None:
    if track_id is None:
        return
    has_file = db.scalar(
        select(LibraryFile.id).where(LibraryFile.track_id == track_id).limit(1)
    )
    has_match = db.scalar(select(Match.id).where(Match.track_id == track_id).limit(1))
    if has_file is not None or has_match is not None:
        return

    track = db.get(Track, track_id)
    if track is None:
        return
    album_id = track.album_id
    db.delete(track)
    db.flush()
    if (
        db.scalar(select(Track.id).where(Track.album_id == album_id).limit(1))
        is not None
    ):
        return

    album = db.get(Album, album_id)
    if album is None:
        return
    artist_id = album.artist_id
    db.delete(album)
    db.flush()
    if db.scalar(select(Album.id).where(Album.artist_id == artist_id).limit(1)) is None:
        artist = db.get(Artist, artist_id)
        if artist is not None:
            db.delete(artist)
            db.flush()


@contextmanager
def _scan_execution_lock(db: Session):
    bind = db.get_bind()
    if bind.dialect.name != "postgresql":
        yield
        return

    connection = bind.connect()
    acquired = False
    try:
        acquired = bool(
            connection.scalar(
                text("SELECT pg_try_advisory_lock(:lock_id)"),
                {"lock_id": 2026080603},
            )
        )
        if not acquired:
            raise ScanAlreadyRunning("A library scan is already running")
        yield
    finally:
        if acquired:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"),
                {"lock_id": 2026080603},
            )
            connection.commit()
        connection.close()


def upsert_audio_file(db: Session, metadata: AudioMetadata) -> tuple[str, int]:
    now = utcnow()
    same_hash = db.scalar(select(LibraryFile).where(LibraryFile.sha1 == metadata.sha1))
    same_path = db.scalar(select(LibraryFile).where(LibraryFile.path == metadata.path))

    if same_hash is not None:
        same_hash.scanned_at = now
        same_hash.size_bytes = metadata.size_bytes
        same_hash.format = metadata.format
        same_hash.bit_depth = metadata.bit_depth
        same_hash.sample_rate = metadata.sample_rate
        if same_hash.path == metadata.path:
            old_track = same_hash.track
            old_track_id = same_hash.track_id
            artist = _get_or_create_artist(db, metadata)
            album = _get_or_create_album(db, artist, metadata)
            track = _get_or_create_track(db, album, metadata)
            if old_track_id != track.id:
                _carry_catalog_enrichment(old_track, track)
                same_hash.track = track
                db.flush()
                _prune_orphaned_track(db, old_track_id)
                return "updated", album.id
            return "unchanged", album.id

        album_id = same_hash.track.album_id

        if same_path is not None and same_path.id != same_hash.id:
            stale_track_id = same_path.track_id
            db.delete(same_path)
            db.flush()
            _prune_orphaned_track(db, stale_track_id)
        if not Path(same_hash.path).exists():
            old_track = same_hash.track
            old_track_id = same_hash.track_id
            artist = _get_or_create_artist(db, metadata)
            album = _get_or_create_album(db, artist, metadata)
            track = _get_or_create_track(db, album, metadata)
            if old_track_id != track.id:
                _carry_catalog_enrichment(old_track, track)
            same_hash.track = track
            same_hash.path = metadata.path
            db.flush()
            if old_track_id != track.id:
                _prune_orphaned_track(db, old_track_id)
            return "moved", album.id
        return "duplicate_content", album_id

    artist = _get_or_create_artist(db, metadata)
    album = _get_or_create_album(db, artist, metadata)
    track = _get_or_create_track(db, album, metadata)

    if same_path is not None:
        old_track_id = same_path.track_id
        if old_track_id == track.id:
            _apply_replacement_metadata(track, metadata)
        same_path.track = track
        same_path.sha1 = metadata.sha1
        same_path.format = metadata.format
        same_path.bit_depth = metadata.bit_depth
        same_path.sample_rate = metadata.sample_rate
        same_path.size_bytes = metadata.size_bytes
        same_path.scanned_at = now
        db.flush()
        if old_track_id != track.id:
            _prune_orphaned_track(db, old_track_id)
        return "updated", album.id

    db.add(
        LibraryFile(
            track=track,
            path=metadata.path,
            format=metadata.format,
            bit_depth=metadata.bit_depth,
            sample_rate=metadata.sample_rate,
            size_bytes=metadata.size_bytes,
            sha1=metadata.sha1,
            scanned_at=now,
        )
    )
    db.flush()
    return "added", album.id


def _prune_missing_files(
    db: Session,
    root_path: Path,
    present_paths: set[Path],
) -> int:
    """Remove catalog rows for files no longer present under the scanned root."""

    removed = 0
    library_files = db.scalars(select(LibraryFile)).all()
    for library_file in library_files:
        stored_path = Path(library_file.path).expanduser().resolve()
        if not stored_path.is_relative_to(root_path) or stored_path in present_paths:
            continue

        track_id = library_file.track_id
        db.delete(library_file)
        db.flush()
        removed += 1

        has_file = db.scalar(
            select(LibraryFile.id).where(LibraryFile.track_id == track_id).limit(1)
        )
        if has_file is not None:
            continue

        for match in db.scalars(select(Match).where(Match.track_id == track_id)):
            match.track_id = None
            match.confidence = 0.0
            match.method = "none"
            match.status = MatchStatus.missing
        db.flush()
        _prune_orphaned_track(db, track_id)

    return removed


def _scan_library_unlocked(
    db: Session,
    root: str | Path | None = None,
    progress_callback: Callable[[ScanSummary], None] | None = None,
) -> ScanSummary:
    root_path = Path(root or settings.music_library_path).expanduser().resolve()
    paths = list(iter_audio_files(root_path))
    present_paths = {path.resolve() for path in paths}
    summary = ScanSummary(discovered=len(paths))
    touched_album_ids: set[int] = set()

    for path in paths:
        digest: str | None = None
        try:
            stat_before = path.stat()
            digest = sha1_file(path)
            metadata = read_audio_metadata(path, root_path, sha1=digest)
            stat_after = path.stat()
            if (
                stat_before.st_size != stat_after.st_size
                or stat_before.st_mtime_ns != stat_after.st_mtime_ns
            ):
                raise ScannerError("File changed while it was being scanned")

            action, album_id = upsert_audio_file(db, metadata)
            db.commit()
            setattr(summary, action, getattr(summary, action) + 1)
            touched_album_ids.add(album_id)
        except IntegrityError:
            db.rollback()
            existing = (
                db.scalar(select(LibraryFile).where(LibraryFile.sha1 == digest))
                if digest is not None
                else None
            )
            if existing is not None:
                existing.scanned_at = utcnow()
                db.commit()
                summary.unchanged += 1
                touched_album_ids.add(existing.track.album_id)
            else:
                raise
        except SQLAlchemyError:
            db.rollback()
            raise
        except (OSError, ScannerError) as exc:
            db.rollback()
            summary.failed += 1
            summary.issues.append(ScanIssue(str(path), f"{type(exc).__name__}: {exc}"))
        summary.album_ids = sorted(touched_album_ids)
        if progress_callback is not None:
            progress_callback(summary)

    try:
        summary.removed = _prune_missing_files(db, root_path, present_paths)
        db.commit()
    except SQLAlchemyError:
        db.rollback()
        raise
    if progress_callback is not None and summary.removed:
        progress_callback(summary)

    return summary


def scan_library(
    db: Session,
    root: str | Path | None = None,
    progress_callback: Callable[[ScanSummary], None] | None = None,
    lock_acquired_callback: Callable[[], None] | None = None,
) -> ScanSummary:
    with _scan_execution_lock(db):
        if lock_acquired_callback is not None:
            lock_acquired_callback()
        return _scan_library_unlocked(db, root, progress_callback)
