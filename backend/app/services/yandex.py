from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Playlist, PlaylistItem, PlaylistSource, ServiceEnum
from app.services.credentials import CredentialError, get_credential_payload
from app.services.normalize import normalize_playlist_item

YANDEX_TRACK_BATCH_SIZE = 50


class YandexClient(Protocol):
    """Subset of yandex-music used by the importer and its test doubles."""

    def users_playlists_list(self, *args: Any, **kwargs: Any) -> Sequence[Any]: ...

    def users_likes_tracks(self, *args: Any, **kwargs: Any) -> Any: ...

    def users_playlists(
        self, kind: str | int, user_id: str | int | None = None, **kwargs: Any
    ) -> Any: ...

    def tracks(self, track_ids: Sequence[str], **kwargs: Any) -> Sequence[Any]: ...


class YandexConfigurationError(ValueError):
    pass


class YandexImportError(RuntimeError):
    pass


@dataclass(slots=True, frozen=True)
class YandexTrackData:
    position: int
    artist_raw: str | None
    title_raw: str | None
    album_raw: str | None
    artist_norm: str
    title_norm: str
    album_norm: str
    isrc: str | None
    duration_ms: int | None
    external_track_id: str


@dataclass(slots=True)
class YandexImportSummary:
    imported: int = 0
    updated: int = 0
    skipped: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, int | list[str]]:
        return {
            "imported": self.imported,
            "updated": self.updated,
            "skipped": self.skipped,
            "failed": self.failed,
            "errors": list(self.errors),
        }


def create_yandex_client(token: str) -> YandexClient:
    """Build an initialized production client without persisting the token."""

    if not token or not token.strip():
        raise YandexConfigurationError("A Yandex Music token is required")

    from yandex_music import Client

    return Client(token.strip()).init()


def build_yandex_snapshot_hash(
    *,
    external_id: str,
    name: str,
    revision: str | int | None,
    snapshot: str | int | None,
    tracks: Sequence[YandexTrackData],
) -> str:
    """Return a stable content/version hash suitable for incremental imports."""

    payload = {
        "schema": 1,
        "playlist": {
            "external_id": external_id,
            "name": name,
            "revision": None if revision is None else str(revision),
            "snapshot": None if snapshot is None else str(snapshot),
        },
        "tracks": [asdict(track) for track in tracks],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def import_yandex_playlists(
    db: Session,
    source: PlaylistSource,
    *,
    client: YandexClient | None = None,
) -> YandexImportSummary:
    """Import every playlist visible to a configured Yandex source.

    ``users_playlists_list`` is a single-list endpoint in yandex-music. Playlist
    details are fetched individually and lightweight track references are
    resolved through the batch-capable ``tracks`` endpoint.
    """

    _validate_source(source)
    yandex = client or create_yandex_client(_source_token(db, source))

    try:
        remote_playlists = list(yandex.users_playlists_list() or [])
    except Exception as exc:
        raise YandexImportError("Could not list Yandex Music playlists") from exc

    summary = YandexImportSummary()
    for remote_summary in remote_playlists:
        external_id = _safe_playlist_id(remote_summary)
        try:
            remote_playlist = _load_full_playlist(yandex, remote_summary)
            result = _save_remote_playlist(db, source, yandex, remote_playlist)
            setattr(summary, result, getattr(summary, result) + 1)
        except Exception as exc:
            db.rollback()
            if isinstance(exc, SQLAlchemyError):
                raise
            summary.failed += 1
            summary.errors.append(f"{external_id}: {type(exc).__name__}")

    try:
        liked_playlist = _load_liked_playlist(yandex)
        if liked_playlist is not None:
            result = _save_remote_playlist(db, source, yandex, liked_playlist)
            setattr(summary, result, getattr(summary, result) + 1)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, SQLAlchemyError):
            raise
        summary.failed += 1
        summary.errors.append(f"liked: {type(exc).__name__}")

    return summary


def refresh_yandex_playlist(
    db: Session,
    playlist: Playlist,
    *,
    client: YandexClient | None = None,
) -> YandexImportSummary:
    """Refresh a single stored playlist using its stable ``owner:kind`` id."""

    source = playlist.source
    if source is None:
        source = db.get(PlaylistSource, playlist.source_id)
    if source is None:
        raise YandexConfigurationError("Playlist source does not exist")
    _validate_source(source)

    yandex = client or create_yandex_client(_source_token(db, source))
    owner_id, kind = _split_playlist_id(playlist.external_id)
    try:
        if kind == "liked":
            remote = _load_liked_playlist(yandex)
        else:
            remote = yandex.users_playlists(kind, user_id=owner_id)
            if isinstance(remote, Sequence) and not isinstance(remote, (str, bytes)):
                remote = remote[0] if remote else None
        if remote is None:
            raise YandexImportError("Yandex Music playlist was not found")
        result = _save_remote_playlist(db, source, yandex, remote)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, (YandexConfigurationError, YandexImportError)):
            raise
        raise YandexImportError("Could not refresh Yandex Music playlist") from exc

    summary = YandexImportSummary()
    setattr(summary, result, 1)
    return summary


def _load_liked_playlist(client: YandexClient) -> dict[str, Any] | None:
    """Represent the special liked-tracks collection as a regular playlist."""

    liked = client.users_likes_tracks()
    if liked is None:
        return None
    owner_id = _get(liked, "uid")
    if owner_id is None:
        raise YandexImportError("Yandex liked tracks have no owner")
    tracks = list(_get(liked, "tracks", default=[]) or [])
    return {
        "owner": {"uid": owner_id},
        "uid": owner_id,
        "kind": "liked",
        "title": "Мне нравится",
        "revision": _get(liked, "revision"),
        "snapshot": None,
        "track_count": len(tracks),
        "tracks": tracks,
    }


def _save_remote_playlist(
    db: Session,
    source: PlaylistSource,
    client: YandexClient,
    remote_playlist: Any,
) -> Literal["imported", "updated", "skipped"]:
    external_id = _playlist_id(remote_playlist)
    name = str(_get(remote_playlist, "title", default="") or "").strip()
    if not name:
        name = f"Yandex playlist {external_id}"

    remote_tracks = list(_get(remote_playlist, "tracks", default=[]) or [])
    tracks = _resolve_tracks(client, remote_tracks)
    snapshot_hash = build_yandex_snapshot_hash(
        external_id=external_id,
        name=name,
        revision=_get(remote_playlist, "revision"),
        snapshot=_get(remote_playlist, "snapshot"),
        tracks=tracks,
    )

    existing = db.scalar(
        select(Playlist).where(
            Playlist.source_id == source.id,
            Playlist.external_id == external_id,
        )
    )
    if existing is not None and existing.snapshot_hash == snapshot_hash:
        return "skipped"

    action: Literal["imported", "updated"]
    if existing is None:
        existing = Playlist(source_id=source.id, external_id=external_id, name=name)
        db.add(existing)
        action = "imported"
    else:
        action = "updated"

    existing.name = name
    existing.snapshot_hash = snapshot_hash
    existing.track_count = len(tracks)
    _reconcile_items(existing, tracks)
    db.commit()
    return action


def _reconcile_items(playlist: Playlist, tracks: Sequence[YandexTrackData]) -> None:
    """Update rows by position, preserving stable ids and invalidating stale matches."""

    existing_by_position = {item.position: item for item in playlist.items}
    incoming_positions: set[int] = set()
    for track in tracks:
        incoming_positions.add(track.position)
        item = existing_by_position.get(track.position)
        if item is None:
            item = PlaylistItem(position=track.position)
            playlist.items.append(item)
        elif _item_matching_signature(item) != _track_matching_signature(track):
            # A match belongs to the old remote track and must not survive a
            # changed id/metadata row when Stage 4 has already run.
            item.match = None

        item.artist_raw = track.artist_raw
        item.title_raw = track.title_raw
        item.album_raw = track.album_raw
        item.artist_norm = track.artist_norm
        item.title_norm = track.title_norm
        item.album_norm = track.album_norm
        item.isrc = track.isrc
        item.duration_ms = track.duration_ms
        item.external_track_id = track.external_track_id

    for item in list(playlist.items):
        if item.position not in incoming_positions:
            playlist.items.remove(item)


def _item_matching_signature(item: PlaylistItem) -> tuple[Any, ...]:
    return (
        item.external_track_id,
        item.artist_norm,
        item.title_norm,
        item.album_norm,
        item.isrc,
        item.duration_ms,
    )


def _track_matching_signature(track: YandexTrackData) -> tuple[Any, ...]:
    return (
        track.external_track_id,
        track.artist_norm,
        track.title_norm,
        track.album_norm,
        track.isrc,
        track.duration_ms,
    )


def _load_full_playlist(client: YandexClient, summary: Any) -> Any:
    # The list response can already contain complete tracks (notably for an
    # empty playlist). Current Yandex responses also use ``tracks=[]`` as a
    # placeholder while reporting a non-zero ``track_count``; those summaries
    # still require the detail request.
    tracks = _get(summary, "tracks")
    track_count = _get(summary, "track_count", "trackCount")
    if tracks is not None:
        try:
            tracks_are_complete = track_count is None or len(tracks) >= int(track_count)
        except (TypeError, ValueError):
            tracks_are_complete = False
        if tracks_are_complete:
            return summary

    owner_id, kind = _playlist_identity(summary)
    full = client.users_playlists(kind, user_id=owner_id)
    if isinstance(full, Sequence) and not isinstance(full, (str, bytes)):
        full = full[0] if full else None
    if full is None:
        raise YandexImportError("Yandex Music playlist details are unavailable")
    return full


def _resolve_tracks(
    client: YandexClient, track_refs: Sequence[Any]
) -> list[YandexTrackData]:
    resolved: dict[str, Any] = {}
    missing_ids: list[str] = []

    for ref in track_refs:
        embedded = _get(ref, "track")
        ref_id = _track_external_id(ref)
        if embedded is not None:
            resolved[ref_id] = embedded
        elif ref_id:
            missing_ids.append(ref_id)

    for batch in _batches(missing_ids, YANDEX_TRACK_BATCH_SIZE):
        for track in client.tracks(batch) or []:
            resolved[_track_external_id(track)] = track
            base_id = _track_base_id(track)
            if base_id:
                resolved.setdefault(base_id, track)

    result: list[YandexTrackData] = []
    for position, ref in enumerate(track_refs):
        external_id = _track_external_id(ref)
        track = resolved.get(external_id)
        if track is None:
            # Some API responses omit the album part from full-track ids.
            track = resolved.get(_track_base_id(ref))
        result.append(_to_track_data(position, external_id, track))
    return result


def _to_track_data(position: int, external_id: str, track: Any) -> YandexTrackData:
    title = _clean_optional(_get(track, "title"))
    version = _clean_optional(_get(track, "version"))
    if title and version and version.casefold() not in title.casefold():
        title = f"{title} ({version})"

    artists = _get(track, "artists", default=[]) or []
    artist = (
        ", ".join(
            name
            for item in artists
            if (name := _clean_optional(_get(item, "name"))) is not None
        )
        or None
    )

    albums = _get(track, "albums", default=[]) or []
    album = None
    if albums:
        album = _clean_optional(_get(albums[0], "title"))

    normalized = normalize_playlist_item(artist, title, album)
    return YandexTrackData(
        position=position,
        artist_raw=artist,
        title_raw=title,
        album_raw=album,
        artist_norm=normalized["artist_norm"],
        title_norm=normalized["title_norm"],
        album_norm=normalized["album_norm"],
        isrc=_normalize_isrc(_get(track, "isrc")),
        duration_ms=_duration_ms(_get(track, "duration_ms", "durationMs")),
        external_track_id=external_id,
    )


def _playlist_identity(playlist: Any) -> tuple[str, str | int]:
    owner = _get(playlist, "owner")
    owner_id = _get(owner, "uid", "id") if owner is not None else None
    if owner_id is None:
        owner_id = _get(playlist, "uid")
    kind = _get(playlist, "kind")
    if owner_id is None or kind is None:
        raise YandexImportError("Yandex playlist has no owner or kind")
    return str(owner_id), kind


def _playlist_id(playlist: Any) -> str:
    owner_id, kind = _playlist_identity(playlist)
    return f"{owner_id}:{kind}"


def _safe_playlist_id(playlist: Any) -> str:
    try:
        return _playlist_id(playlist)
    except Exception:
        return "unknown"


def _split_playlist_id(external_id: str) -> tuple[str, str | int]:
    owner_id, separator, kind = external_id.rpartition(":")
    if not separator or not owner_id or not kind:
        raise YandexConfigurationError("Invalid stored Yandex playlist id")
    return owner_id, int(kind) if kind.isdecimal() else kind


def _track_external_id(track: Any) -> str:
    explicit = _get(track, "track_id", "trackId")
    if explicit:
        return str(explicit)

    track_id = _get(track, "id")
    if track_id is None:
        return ""
    album_id = _get(track, "album_id", "albumId")
    if album_id is None:
        albums = _get(track, "albums", default=[]) or []
        if albums:
            album_id = _get(albums[0], "id")
    return f"{track_id}:{album_id}" if album_id is not None else str(track_id)


def _track_base_id(track: Any) -> str:
    track_id = _get(track, "id")
    return "" if track_id is None else str(track_id)


def _batches(values: Sequence[str], size: int) -> Iterable[list[str]]:
    if size <= 0:
        raise ValueError("Yandex track batch size must be positive")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _get(value: Any, *names: str, default: Any = None) -> Any:
    if value is None:
        return default
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return default


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _duration_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        duration = int(value)
    except (TypeError, ValueError):
        return None
    return duration if duration >= 0 else None


def _normalize_isrc(value: Any) -> str | None:
    cleaned = "".join(
        character for character in str(value or "") if character.isalnum()
    )
    cleaned = cleaned.upper()
    return cleaned if len(cleaned) == 12 else None


def _validate_source(source: PlaylistSource) -> None:
    if source.id is None:
        raise YandexConfigurationError("Yandex source must be persisted first")
    service = (
        source.service.value
        if isinstance(source.service, ServiceEnum)
        else source.service
    )
    if service != ServiceEnum.yandex.value:
        raise YandexConfigurationError("Playlist source is not Yandex Music")


def _source_token(db: Session, source: PlaylistSource) -> str:
    try:
        token = get_credential_payload(db, "yandex").get("token")
    except CredentialError:
        # Transitional only: the migration command clears these fields before
        # the updated worker starts.
        token = source.access_token or settings.yandex_token
    if not token or not token.strip():
        raise YandexConfigurationError("YANDEX_TOKEN is not configured")
    return str(token)
