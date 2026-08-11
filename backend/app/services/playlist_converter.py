"""Convert user-supplied playlist text into matchable playlist records."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import uuid
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Playlist, PlaylistItem, PlaylistSource, ServiceEnum
from app.services.normalize import normalize_playlist_item


MAX_PLAYLIST_TRACKS = 5_000
_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")
_SPOTIFY_TRACK_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")
_NUMBERING_RE = re.compile(r"^\s*\d+[.)]\s+")


class PlaylistConversionError(ValueError):
    pass


@dataclass(frozen=True)
class ConvertedTrack:
    artist: str
    title: str
    album: str = ""
    isrc: str | None = None
    duration_ms: int | None = None
    external_track_id: str | None = None


@dataclass(frozen=True)
class ConvertedPlaylist:
    tracks: list[ConvertedTrack]
    skipped: int
    format: str


def _header_key(value: object) -> str:
    return re.sub(r"[^a-zа-яё0-9]+", "", str(value or "").casefold())


_FIELD_ALIASES = {
    "artist": {
        "artist",
        "artists",
        "artistname",
        "artistnames",
        "исполнитель",
        "исполнители",
    },
    "title": {
        "title",
        "name",
        "song",
        "track",
        "trackname",
        "tracktitle",
        "трек",
        "название",
    },
    "album": {"album", "albumname", "альбом"},
    "isrc": {"isrc"},
    "duration_ms": {
        "duration",
        "durationms",
        "trackdurationms",
        "длительность",
        "длительностьмс",
    },
    "external_track_id": {
        "trackid",
        "trackuri",
        "spotifytrackuri",
        "spotifyuri",
        "uri",
    },
}


def _clean_text(value: object, *, limit: int = 512) -> str:
    text = " ".join(str(value or "").replace("\x00", " ").split()).strip()
    return text if len(text) <= limit else ""


def _normalize_isrc(value: object) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return normalized if _ISRC_RE.fullmatch(normalized) else None


def _duration_ms(value: object) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if ":" in raw:
        try:
            parts = [float(part) for part in raw.split(":")]
        except ValueError:
            return None
        if len(parts) == 2:
            seconds = parts[0] * 60 + parts[1]
        elif len(parts) == 3:
            seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
        else:
            return None
        return max(0, round(seconds * 1000))
    try:
        parsed = int(float(raw))
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _spotify_track_id(value: object) -> str | None:
    raw = str(value or "").strip()
    candidate = raw
    if raw.casefold().startswith("spotify:track:"):
        candidate = raw.rsplit(":", 1)[-1]
    elif raw.casefold().startswith("https://open.spotify.com/track/"):
        parts = [part for part in urlsplit(raw).path.split("/") if part]
        candidate = parts[-1] if len(parts) == 2 else ""
    return candidate if _SPOTIFY_TRACK_ID_RE.fullmatch(candidate) else None


def _track(
    artist: object,
    title: object,
    *,
    album: object = "",
    isrc: object = None,
    duration_ms: object = None,
    external_track_id: object = None,
) -> ConvertedTrack | None:
    clean_artist = _clean_text(artist)
    clean_title = _clean_text(title)
    clean_album = _clean_text(album)
    if not clean_artist or not clean_title:
        return None
    return ConvertedTrack(
        artist=clean_artist,
        title=clean_title,
        album=clean_album,
        isrc=_normalize_isrc(isrc),
        duration_ms=_duration_ms(duration_ms),
        external_track_id=_spotify_track_id(external_track_id),
    )


def _split_artist_title(value: str) -> tuple[str, str] | None:
    candidate = _NUMBERING_RE.sub("", value.strip())
    for separator in ("\t", " — ", " – ", " - "):
        if separator in candidate:
            artist, title = candidate.split(separator, 1)
            if artist.strip() and title.strip():
                return artist.strip(), title.strip()
    return None


def _csv_mapping(fieldnames: list[str]) -> dict[str, str]:
    normalized = {_header_key(header): header for header in fieldnames if header}
    mapping = {}
    for field, aliases in _FIELD_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[field] = normalized[alias]
                break
    if "artist" not in mapping or "title" not in mapping:
        raise PlaylistConversionError(
            "CSV must contain artist and track title columns"
        )
    return mapping


def _parse_csv(content: str) -> ConvertedPlaylist:
    try:
        dialect = csv.Sniffer().sniff(content[:16_384], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(content), dialect=dialect)
    if not reader.fieldnames:
        raise PlaylistConversionError("CSV header is missing")
    mapping = _csv_mapping(reader.fieldnames)
    tracks: list[ConvertedTrack] = []
    skipped = 0
    for row in reader:
        converted = _track(
            row.get(mapping["artist"]),
            row.get(mapping["title"]),
            album=row.get(mapping.get("album", ""), ""),
            isrc=row.get(mapping.get("isrc", "")),
            duration_ms=row.get(mapping.get("duration_ms", "")),
            external_track_id=row.get(mapping.get("external_track_id", "")),
        )
        if converted is None:
            skipped += 1
        else:
            tracks.append(converted)
    return ConvertedPlaylist(tracks=tracks, skipped=skipped, format="csv")


def _parse_text(content: str) -> ConvertedPlaylist:
    tracks: list[ConvertedTrack] = []
    skipped = 0
    for line in content.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        fields = [field.strip() for field in candidate.split("\t")]
        if len(fields) >= 2:
            converted = _track(
                fields[0],
                fields[1],
                album=fields[2] if len(fields) > 2 else "",
            )
        else:
            split = _split_artist_title(candidate)
            converted = _track(*split) if split else None
        if converted is None:
            skipped += 1
        else:
            tracks.append(converted)
    return ConvertedPlaylist(tracks=tracks, skipped=skipped, format="text")


def _parse_m3u(content: str) -> ConvertedPlaylist:
    tracks: list[ConvertedTrack] = []
    skipped = 0
    pending: tuple[str, int | None] | None = None
    for line in content.splitlines():
        candidate = line.strip()
        if not candidate or candidate == "#EXTM3U":
            continue
        if candidate.startswith("#EXTINF:"):
            metadata = candidate[len("#EXTINF:") :]
            raw_duration, separator, label = metadata.partition(",")
            seconds = _duration_ms(raw_duration)
            pending = (label.strip(), seconds * 1000 if seconds is not None else None)
            if not separator:
                pending = None
                skipped += 1
            continue
        if candidate.startswith("#"):
            continue
        label, duration = pending or (candidate, None)
        pending = None
        if pending is None and label == candidate:
            label = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", candidate.rsplit("/", 1)[-1].rsplit("\\", 1)[-1])
        split = _split_artist_title(label)
        converted = _track(*split, duration_ms=duration) if split else None
        if converted is None:
            skipped += 1
        else:
            tracks.append(converted)
    if pending is not None:
        skipped += 1
    return ConvertedPlaylist(tracks=tracks, skipped=skipped, format="m3u")


def _detect_format(content: str) -> Literal["csv", "m3u", "text"]:
    stripped = content.lstrip("\ufeff\r\n\t ")
    if stripped.startswith("#EXTM3U") or "#EXTINF:" in stripped[:4096]:
        return "m3u"
    first_line = stripped.splitlines()[0] if stripped else ""
    header_keys = {_header_key(value) for value in re.split(r"[,;\t]", first_line)}
    if header_keys & _FIELD_ALIASES["artist"] and header_keys & _FIELD_ALIASES["title"]:
        return "csv"
    return "text"


def convert_playlist_content(
    content: str,
    format_hint: Literal["auto", "csv", "m3u", "text"] = "auto",
) -> ConvertedPlaylist:
    normalized = str(content or "").replace("\r\n", "\n").replace("\r", "\n")
    if not normalized.strip():
        raise PlaylistConversionError("Playlist content is empty")
    selected = _detect_format(normalized) if format_hint == "auto" else format_hint
    converted = {
        "csv": _parse_csv,
        "m3u": _parse_m3u,
        "text": _parse_text,
    }[selected](normalized)
    if not converted.tracks:
        raise PlaylistConversionError("No valid artist and track title rows found")
    if len(converted.tracks) > MAX_PLAYLIST_TRACKS:
        raise PlaylistConversionError(
            f"Playlist exceeds the {MAX_PLAYLIST_TRACKS} track limit"
        )
    return converted


def save_converted_playlist(
    db: Session,
    *,
    user_id: int,
    name: str,
    content: str,
    converted: ConvertedPlaylist,
) -> Playlist:
    clean_name = _clean_text(name)
    if not clean_name:
        raise PlaylistConversionError("Playlist name is invalid")
    source = db.scalar(
        select(PlaylistSource).where(
            PlaylistSource.user_id == user_id,
            PlaylistSource.service == ServiceEnum.manual,
        )
    )
    if source is None:
        source = PlaylistSource(user_id=user_id, service=ServiceEnum.manual)
        db.add(source)
        db.flush()
    playlist = Playlist(
        source=source,
        user_id=user_id,
        external_id=f"manual:{uuid.uuid4().hex}",
        name=clean_name,
        snapshot_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        track_count=len(converted.tracks),
    )
    db.add(playlist)
    db.flush()
    for position, track in enumerate(converted.tracks):
        normalized = normalize_playlist_item(track.artist, track.title, track.album)
        db.add(
            PlaylistItem(
                playlist=playlist,
                position=position,
                artist_raw=track.artist,
                title_raw=track.title,
                album_raw=track.album,
                artist_norm=normalized["artist_norm"],
                title_norm=normalized["title_norm"],
                album_norm=normalized["album_norm"],
                isrc=track.isrc,
                duration_ms=track.duration_ms,
                external_track_id=track.external_track_id,
            )
        )
    db.commit()
    db.refresh(playlist)
    return playlist
