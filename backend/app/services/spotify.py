from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlsplit

import spotipy
from redis import Redis
from redis.exceptions import RedisError
from spotipy.cache_handler import MemoryCacheHandler
from spotipy.oauth2 import SpotifyOAuth
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    Match,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    utcnow,
)
from app.services.credentials import (
    CredentialError,
)
from app.services.user_credentials import (
    get_user_credential_payload,
    save_user_credential,
)

SPOTIFY_SCOPES = (
    "playlist-read-private",
    "playlist-read-collaborative",
    "user-library-read",
)
SPOTIFY_PLAYLIST_ITEMS_PAGE_SIZE = 50
_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")
_SPOTIFY_PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9]{22}$")


class SpotifyServiceError(RuntimeError):
    """Base error for Spotify OAuth and playlist imports."""


class SpotifyConfigurationError(SpotifyServiceError):
    """Spotify cannot be used because required configuration is missing."""


class SpotifyOAuthStateError(SpotifyServiceError):
    """The OAuth callback state is missing, expired, invalid, or already used."""


class SpotifyOAuthStateStorageError(SpotifyOAuthStateError):
    """The shared OAuth state store could not be reached."""


class SpotifyTokenError(SpotifyServiceError):
    """Spotify returned an unusable token response."""


class SpotifyProviderError(SpotifyServiceError):
    """Spotify could not complete a remote OAuth operation."""


class SpotifyImportError(SpotifyServiceError):
    """Spotify returned an unusable playlist response."""


class OAuthStateStore(Protocol):
    """One-time OAuth state storage; implementations must consume atomically."""

    def issue(self) -> str: ...

    def consume(self, state: str) -> bool: ...


class SpotifyOAuthBackend(Protocol):
    def get_authorize_url(self, state: str | None = None) -> str: ...

    def get_access_token(
        self,
        code: str,
        *,
        as_dict: bool = True,
        check_cache: bool = False,
    ) -> Mapping[str, Any]: ...

    def refresh_access_token(self, refresh_token: str) -> Mapping[str, Any]: ...


class SpotifyApiClient(Protocol):
    def playlist(
        self,
        playlist_id: str,
        *,
        fields: str | None = None,
    ) -> Mapping[str, Any]: ...

    def current_user_playlists(
        self, *, limit: int, offset: int
    ) -> Mapping[str, Any]: ...

    def playlist_items(
        self,
        playlist_id: str,
        *,
        limit: int,
        offset: int,
        additional_types: tuple[str, ...],
    ) -> Mapping[str, Any]: ...


@dataclass(slots=True, frozen=True)
class SpotifyToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime | None
    token_type: str = "Bearer"
    scope: str | None = None


@dataclass(slots=True, frozen=True)
class SpotifyAuthorizationRequest:
    url: str
    state: str


@dataclass(slots=True, frozen=True)
class SpotifyPlaylistRecord:
    external_id: str
    name: str
    snapshot_hash: str


@dataclass(slots=True, frozen=True)
class SpotifyTrackRecord:
    playlist_external_id: str
    snapshot_hash: str
    position: int
    artist_raw: str
    title_raw: str
    album_raw: str
    isrc: str | None
    duration_ms: int | None
    external_track_id: str | None


@dataclass(slots=True)
class SpotifyImportSummary:
    discovered: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    tracks_imported: int = 0
    skipped_items: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RedisOAuthStateStore:
    """Redis-backed one-time states, safe across API processes and restarts."""

    key_prefix = "spotify:oauth:state:v1"

    def __init__(
        self,
        redis_client: Any,
        *,
        ttl_seconds: int = 10 * 60,
    ) -> None:
        if ttl_seconds < 60:
            raise ValueError("OAuth state TTL must be at least 60 seconds")
        self.redis = redis_client
        self.ttl_seconds = ttl_seconds

    @classmethod
    def _key(cls, state: str) -> str:
        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        return f"{cls.key_prefix}:{digest}"

    def issue(self) -> str:
        return self.issue_with_context("1")

    def issue_with_context(self, context: str) -> str:
        if not context or len(context) > 256:
            raise SpotifyOAuthStateError("OAuth state context is invalid")
        for _ in range(3):
            state = secrets.token_urlsafe(32)
            try:
                stored = self.redis.set(
                    self._key(state),
                    context,
                    ex=self.ttl_seconds,
                    nx=True,
                )
            except RedisError as exc:
                raise SpotifyOAuthStateStorageError(
                    "OAuth state storage is unavailable"
                ) from exc
            if stored:
                return state
        raise SpotifyOAuthStateError("Could not allocate a unique OAuth state")

    def consume(self, state: str) -> bool:
        return self.consume_context(state) is not None

    def consume_context(self, state: str) -> str | None:
        if not state:
            return False
        try:
            value = self.redis.getdel(self._key(state))
            return str(value) if value is not None else None
        except RedisError as exc:
            raise SpotifyOAuthStateStorageError(
                "OAuth state storage is unavailable"
            ) from exc


def create_spotify_state_store(
    *,
    redis_client: Any | None = None,
    ttl_seconds: int = 10 * 60,
) -> RedisOAuthStateStore:
    client = redis_client or Redis.from_url(settings.redis_url, decode_responses=True)
    return RedisOAuthStateStore(client, ttl_seconds=ttl_seconds)


def create_spotify_oauth(config: Any = settings) -> SpotifyOAuth:
    client_id = str(config.spotify_client_id or "").strip()
    client_secret = str(config.spotify_client_secret or "").strip()
    redirect_uri = str(config.spotify_redirect_uri or "").strip()
    if not client_id or not client_secret or not redirect_uri:
        raise SpotifyConfigurationError(
            "SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET and SPOTIFY_REDIRECT_URI "
            "must be configured"
        )
    return SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=" ".join(SPOTIFY_SCOPES),
        open_browser=False,
        cache_handler=MemoryCacheHandler(),
    )


def create_spotify_authorization(
    state_store: OAuthStateStore,
    *,
    oauth: SpotifyOAuthBackend | None = None,
    context: str | None = None,
) -> SpotifyAuthorizationRequest:
    backend = oauth or create_spotify_oauth()
    if context is None:
        state = state_store.issue()
    elif hasattr(state_store, "issue_with_context"):
        state = state_store.issue_with_context(context)
    else:
        raise SpotifyOAuthStateStorageError("OAuth state binding is unavailable")
    return SpotifyAuthorizationRequest(
        url=backend.get_authorize_url(state=state),
        state=state,
    )


def validate_spotify_state(
    state_store: OAuthStateStore,
    state: str,
    *,
    expected_context: str | None = None,
) -> None:
    if not state or len(state) > 512:
        raise SpotifyOAuthStateError("OAuth state is invalid, expired, or already used")
    if expected_context is None:
        valid = state_store.consume(state)
    elif hasattr(state_store, "consume_context"):
        valid = secrets.compare_digest(
            state_store.consume_context(state) or "",
            expected_context,
        )
    else:
        valid = False
    if not valid:
        raise SpotifyOAuthStateError("OAuth state is invalid, expired, or already used")


def _parse_token(
    token_info: Any,
    *,
    fallback_refresh_token: str | None = None,
    now: datetime | None = None,
) -> SpotifyToken:
    if not isinstance(token_info, Mapping):
        raise SpotifyTokenError("Spotify token response is invalid")
    access_token = str(token_info.get("access_token") or "").strip()
    if not access_token:
        raise SpotifyTokenError("Spotify token response has no access token")

    refresh_token = str(token_info.get("refresh_token") or "").strip()
    refresh_token = refresh_token or fallback_refresh_token
    expires_at: datetime | None = None
    raw_expires_at = token_info.get("expires_at")
    raw_expires_in = token_info.get("expires_in")
    try:
        if raw_expires_at is not None:
            expires_at = datetime.fromtimestamp(float(raw_expires_at), tz=UTC).replace(
                tzinfo=None
            )
        elif raw_expires_in is not None:
            current = now or utcnow()
            expires_at = current + timedelta(seconds=max(0, int(raw_expires_in)))
    except (TypeError, ValueError, OSError) as exc:
        raise SpotifyTokenError("Spotify token expiry is invalid") from exc

    return SpotifyToken(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        token_type=str(token_info.get("token_type") or "Bearer"),
        scope=str(token_info.get("scope") or "").strip() or None,
    )


def exchange_spotify_code(
    code: str,
    state: str,
    state_store: OAuthStateStore,
    *,
    oauth: SpotifyOAuthBackend | None = None,
    expected_context: str | None = None,
) -> SpotifyToken:
    if not code or len(code) > 4096:
        raise SpotifyTokenError("Spotify callback has no authorization code")
    validate_spotify_state(
        state_store,
        state,
        expected_context=expected_context,
    )
    backend = oauth or create_spotify_oauth()
    try:
        response = backend.get_access_token(
            code,
            as_dict=True,
            check_cache=False,
        )
    except Exception as exc:
        raise SpotifyProviderError("Spotify token exchange failed") from exc
    return _parse_token(response)


def refresh_spotify_token(
    refresh_token: str,
    *,
    oauth: SpotifyOAuthBackend | None = None,
) -> SpotifyToken:
    if not refresh_token:
        raise SpotifyTokenError("Spotify refresh token is missing")
    backend = oauth or create_spotify_oauth()
    try:
        response = backend.refresh_access_token(refresh_token)
    except Exception as exc:
        raise SpotifyProviderError("Spotify token refresh failed") from exc
    return _parse_token(response, fallback_refresh_token=refresh_token)


def _is_spotify_source(source: PlaylistSource) -> bool:
    service = (
        source.service.value
        if isinstance(source.service, ServiceEnum)
        else source.service
    )
    return service == ServiceEnum.spotify.value


def save_spotify_token(
    db: Session,
    token: SpotifyToken | Mapping[str, Any],
    *,
    source: PlaylistSource,
) -> PlaylistSource:
    parsed = token if isinstance(token, SpotifyToken) else _parse_token(token)
    if not _is_spotify_source(source) or source.user_id is None:
        raise SpotifyConfigurationError("Playlist source is not Spotify")

    previous_refresh = None
    try:
        previous_refresh = get_user_credential_payload(
            db, source.user_id, "spotify"
        ).get("refresh_token")
    except CredentialError:
        previous_refresh = None
    save_user_credential(
        db,
        source.user_id,
        "spotify",
        {
            "access_token": parsed.access_token,
            "refresh_token": parsed.refresh_token or previous_refresh,
        },
    )
    source.expires_at = parsed.expires_at
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise
    db.refresh(source)
    return source


def spotify_playlist_id_from_url(url: str) -> str:
    """Validate a public open.spotify.com URL and return its playlist id.

    The upstream URL is reconstructed from this identifier by Spotipy, so a
    submitted URL can never turn the backend into an arbitrary URL fetcher.
    """

    candidate = str(url or "").strip()
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError as exc:
        raise SpotifyConfigurationError("Spotify playlist URL is invalid") from exc
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "open.spotify.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
    ):
        raise SpotifyConfigurationError(
            "Use an https://open.spotify.com/playlist/... URL"
        )

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0].casefold().startswith("intl-"):
        parts = parts[1:]
    if len(parts) != 2 or parts[0].casefold() != "playlist":
        raise SpotifyConfigurationError("Spotify URL must point to a playlist")
    playlist_id = parts[1]
    if not _SPOTIFY_PLAYLIST_ID_RE.fullmatch(playlist_id):
        raise SpotifyConfigurationError("Spotify playlist id is invalid")
    return playlist_id


def canonical_spotify_playlist_url(url: str) -> str:
    return f"https://open.spotify.com/playlist/{spotify_playlist_id_from_url(url)}"


def _spotify_client_for_source(
    db: Session,
    source: PlaylistSource,
    *,
    oauth: SpotifyOAuthBackend | None = None,
) -> SpotifyApiClient:
    if not _is_spotify_source(source):
        raise SpotifyConfigurationError("Playlist source is not Spotify")
    try:
        credential = get_user_credential_payload(db, source.user_id, "spotify")
    except CredentialError:
        raise SpotifyTokenError("Spotify source has no credential")
    if source.expires_at is not None and source.expires_at <= utcnow() + timedelta(
        seconds=30
    ):
        refreshed = refresh_spotify_token(
            str(credential.get("refresh_token") or ""), oauth=oauth
        )
        source = save_spotify_token(db, refreshed, source=source)
        credential = get_user_credential_payload(db, source.user_id, "spotify")
    access_token = str(credential.get("access_token") or "").strip()
    if not access_token:
        raise SpotifyTokenError("Spotify source has no access token")
    return spotipy.Spotify(
        auth=access_token,
        requests_timeout=15,
        retries=3,
        status_retries=3,
        backoff_factor=0.5,
    )


def _page_items(page: Mapping[str, Any], *, operation: str) -> list[Any]:
    items = page.get("items")
    if not isinstance(items, list):
        raise SpotifyImportError(f"Spotify {operation} response has no items list")
    return items


def iter_spotify_playlists(client: SpotifyApiClient) -> Iterator[SpotifyPlaylistRecord]:
    offset = 0
    while True:
        page = client.current_user_playlists(limit=50, offset=offset)
        items = _page_items(page, operation="playlist")
        for raw in items:
            if not isinstance(raw, Mapping):
                continue
            external_id = str(raw.get("id") or "").strip()
            snapshot_hash = str(raw.get("snapshot_id") or "").strip()
            if not external_id or not snapshot_hash:
                continue
            yield SpotifyPlaylistRecord(
                external_id=external_id,
                name=str(raw.get("name") or "Untitled playlist").strip()
                or "Untitled playlist",
                snapshot_hash=snapshot_hash,
            )
        if not page.get("next"):
            return
        if not items:
            raise SpotifyImportError("Spotify playlist pagination did not advance")
        offset += len(items)


def _normalize_isrc(value: Any) -> str | None:
    normalized = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return normalized if _ISRC_RE.fullmatch(normalized) else None


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _track_from_item(
    playlist: SpotifyPlaylistRecord,
    raw_item: Any,
    *,
    position: int,
) -> SpotifyTrackRecord | None:
    if not isinstance(raw_item, Mapping):
        return None
    raw_track = raw_item.get("track") or raw_item.get("item")
    if not isinstance(raw_track, Mapping):
        return None
    if raw_track.get("type") not in (None, "track"):
        return None

    artists = raw_track.get("artists") or []
    artist_names = [
        str(artist.get("name") or "").strip()
        for artist in artists
        if isinstance(artist, Mapping) and str(artist.get("name") or "").strip()
    ]
    album = raw_track.get("album")
    album_name = album.get("name") if isinstance(album, Mapping) else ""
    external_ids = raw_track.get("external_ids")
    isrc = external_ids.get("isrc") if isinstance(external_ids, Mapping) else None
    external_track_id = str(raw_track.get("id") or "").strip() or None

    return SpotifyTrackRecord(
        playlist_external_id=playlist.external_id,
        snapshot_hash=playlist.snapshot_hash,
        position=position,
        artist_raw=", ".join(artist_names),
        title_raw=str(raw_track.get("name") or "").strip(),
        album_raw=str(album_name or "").strip(),
        isrc=_normalize_isrc(isrc),
        duration_ms=_positive_int(raw_track.get("duration_ms")),
        external_track_id=external_track_id,
    )


def fetch_spotify_tracks(
    client: SpotifyApiClient,
    playlist: SpotifyPlaylistRecord,
) -> tuple[list[SpotifyTrackRecord], int]:
    records: list[SpotifyTrackRecord] = []
    skipped = 0
    offset = 0
    while True:
        page = client.playlist_items(
            playlist.external_id,
            limit=SPOTIFY_PLAYLIST_ITEMS_PAGE_SIZE,
            offset=offset,
            additional_types=("track",),
        )
        items = _page_items(page, operation="playlist-items")
        for index, raw_item in enumerate(items):
            record = _track_from_item(playlist, raw_item, position=offset + index)
            if record is None:
                skipped += 1
            else:
                records.append(record)
        if not page.get("next"):
            return records, skipped
        if not items:
            raise SpotifyImportError("Spotify track pagination did not advance")
        offset += len(items)


def _normalizer() -> Callable[[str], str]:
    from app.services.normalize import normalize_text

    return normalize_text


def _replace_playlist_items(
    db: Session,
    playlist: Playlist,
    records: list[SpotifyTrackRecord],
    *,
    normalizer: Callable[[str], str],
) -> None:
    existing_items = list(
        db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == playlist.id))
    )
    for item in existing_items:
        match = db.scalar(select(Match).where(Match.playlist_item_id == item.id))
        if match is not None:
            db.delete(match)
        db.delete(item)
    db.flush()

    for record in records:
        db.add(
            PlaylistItem(
                playlist_id=playlist.id,
                position=record.position,
                artist_raw=record.artist_raw,
                title_raw=record.title_raw,
                album_raw=record.album_raw,
                artist_norm=normalizer(record.artist_raw),
                title_norm=normalizer(record.title_raw),
                album_norm=normalizer(record.album_raw),
                isrc=record.isrc,
                duration_ms=record.duration_ms,
                external_track_id=record.external_track_id,
            )
        )


def _upsert_playlist(
    db: Session,
    source: PlaylistSource,
    remote: SpotifyPlaylistRecord,
    client: SpotifyApiClient,
    summary: SpotifyImportSummary,
    *,
    normalizer: Callable[[str], str],
) -> Playlist:
    playlist = db.scalar(
        select(Playlist).where(
            Playlist.source_id == source.id,
            Playlist.external_id == remote.external_id,
        )
    )
    if playlist is not None and playlist.snapshot_hash == remote.snapshot_hash:
        playlist.name = remote.name
        summary.unchanged += 1
        return playlist

    records, skipped = fetch_spotify_tracks(client, remote)
    if playlist is None:
        playlist = Playlist(
            source_id=source.id,
            user_id=source.user_id,
            external_id=remote.external_id,
            name=remote.name,
        )
        db.add(playlist)
        db.flush()
        summary.created += 1
    else:
        summary.updated += 1

    _replace_playlist_items(db, playlist, records, normalizer=normalizer)
    playlist.name = remote.name
    playlist.snapshot_hash = remote.snapshot_hash
    playlist.track_count = len(records)
    playlist.updated_at = utcnow()
    summary.tracks_imported += len(records)
    summary.skipped_items += skipped
    return playlist


def import_spotify_playlists(
    db: Session,
    source: PlaylistSource,
    client: SpotifyApiClient | None = None,
    *,
    normalizer: Callable[[str], str] | None = None,
) -> SpotifyImportSummary:
    if source.id is None or not _is_spotify_source(source):
        raise SpotifyConfigurationError("A persisted Spotify source is required")
    api = client or _spotify_client_for_source(db, source)
    normalize = normalizer or _normalizer()
    summary = SpotifyImportSummary()
    seen: set[str] = set()
    try:
        for remote in iter_spotify_playlists(api):
            if remote.external_id in seen:
                continue
            seen.add(remote.external_id)
            summary.discovered += 1
            playlist_summary = SpotifyImportSummary()
            try:
                _upsert_playlist(
                    db,
                    source,
                    remote,
                    api,
                    playlist_summary,
                    normalizer=normalize,
                )
                db.commit()
                summary.created += playlist_summary.created
                summary.updated += playlist_summary.updated
                summary.unchanged += playlist_summary.unchanged
                summary.tracks_imported += playlist_summary.tracks_imported
                summary.skipped_items += playlist_summary.skipped_items
            except Exception as exc:
                db.rollback()
                if isinstance(exc, SQLAlchemyError):
                    raise
                summary.failed += 1
                summary.errors.append(f"{remote.external_id}: {type(exc).__name__}")
    except Exception:
        db.rollback()
        raise
    return summary


def import_spotify_playlist_url(
    db: Session,
    url: str,
    source: PlaylistSource,
    client: SpotifyApiClient | None = None,
    *,
    normalizer: Callable[[str], str] | None = None,
) -> tuple[SpotifyImportSummary, Playlist]:
    """Import exactly one playlist through the connected Spotify account."""

    playlist_id = spotify_playlist_id_from_url(url)
    if source.id is None or not _is_spotify_source(source):
        raise SpotifyTokenError("Connect Spotify before importing a playlist")
    api = client or _spotify_client_for_source(db, source)
    try:
        raw = api.playlist(
            playlist_id,
            fields="id,name,snapshot_id,type",
        )
    except Exception as exc:
        raise SpotifyProviderError(
            "Spotify could not read this public playlist"
        ) from exc
    if not isinstance(raw, Mapping) or raw.get("type") not in (None, "playlist"):
        raise SpotifyImportError("Spotify returned an invalid playlist response")
    external_id = str(raw.get("id") or "").strip()
    snapshot_hash = str(raw.get("snapshot_id") or "").strip()
    if external_id != playlist_id or not snapshot_hash:
        raise SpotifyImportError("Spotify playlist metadata is incomplete")
    remote = SpotifyPlaylistRecord(
        external_id=external_id,
        name=str(raw.get("name") or "Untitled playlist").strip()
        or "Untitled playlist",
        snapshot_hash=snapshot_hash,
    )

    summary = SpotifyImportSummary(discovered=1)
    try:
        playlist = _upsert_playlist(
            db,
            source,
            remote,
            api,
            summary,
            normalizer=normalizer or _normalizer(),
        )
        db.commit()
        db.refresh(playlist)
    except Exception as exc:
        db.rollback()
        if isinstance(exc, (SQLAlchemyError, SpotifyServiceError)):
            raise
        raise SpotifyProviderError(
            "Spotify could not return the public playlist tracks"
        ) from exc
    return summary, playlist


def refresh_spotify_playlist(
    db: Session,
    playlist: Playlist,
    client: SpotifyApiClient | None = None,
    *,
    normalizer: Callable[[str], str] | None = None,
) -> SpotifyImportSummary:
    source = playlist.source
    if source is None or not _is_spotify_source(source):
        raise SpotifyConfigurationError("Playlist is not attached to Spotify")
    api = client or _spotify_client_for_source(db, source)
    normalize = normalizer or _normalizer()
    summary = SpotifyImportSummary()
    try:
        remote = next(
            (
                candidate
                for candidate in iter_spotify_playlists(api)
                if candidate.external_id == playlist.external_id
            ),
            None,
        )
        if remote is None:
            raise SpotifyImportError("Spotify playlist is no longer available")
        summary.discovered = 1
        _upsert_playlist(
            db,
            source,
            remote,
            api,
            summary,
            normalizer=normalize,
        )
        db.commit()
    except Exception:
        db.rollback()
        raise
    return summary
