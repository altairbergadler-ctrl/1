from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

import httpx
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.models import Album, Track
from app.services.scanner import normalize_catalog_key

_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")
_LUCENE_SPECIAL_RE = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


class MusicBrainzError(RuntimeError):
    """Base error for MusicBrainz enrichment."""


class MusicBrainzConfigurationError(MusicBrainzError):
    """The service cannot safely call MusicBrainz."""


class MusicBrainzCacheError(MusicBrainzError):
    """Redis is unavailable, so caching/rate limiting cannot be guaranteed."""


@dataclass(slots=True, frozen=True)
class ReleaseCandidate:
    mbid: str
    title: str
    artist: str
    score: int
    date: str | None
    track_count: int | None


class MusicBrainzClient:
    cache_prefix = "musicbrainz:v1:http"
    rate_limit_key = "musicbrainz:v1:rate:slot"

    def __init__(
        self,
        *,
        redis_client: Any | None = None,
        http_client: httpx.Client | None = None,
        base_url: str | None = None,
        user_agent: str | None = None,
        cache_ttl_seconds: int | None = None,
        negative_cache_ttl_seconds: int | None = None,
        rate_limit_seconds: float | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        max_retries: int = 2,
    ) -> None:
        self.base_url = (base_url or settings.musicbrainz_base_url).rstrip("/")
        self.user_agent = (user_agent or settings.musicbrainz_user_agent).strip()
        if not self.user_agent or "your-email-or-url" in self.user_agent.casefold():
            raise MusicBrainzConfigurationError(
                "MUSICBRAINZ_USER_AGENT must include the application name, "
                "version and maintainer contact"
            )

        self.redis = redis_client or Redis.from_url(
            settings.redis_url, decode_responses=True
        )
        self.cache_ttl_seconds = (
            cache_ttl_seconds
            if cache_ttl_seconds is not None
            else settings.musicbrainz_cache_ttl_seconds
        )
        self.negative_cache_ttl_seconds = (
            negative_cache_ttl_seconds
            if negative_cache_ttl_seconds is not None
            else settings.musicbrainz_negative_cache_ttl_seconds
        )
        self.rate_limit_seconds = (
            rate_limit_seconds
            if rate_limit_seconds is not None
            else settings.musicbrainz_rate_limit_seconds
        )
        self.sleeper = sleeper
        self.max_retries = max_retries
        self._owns_http_client = http_client is None
        self.http = http_client or httpx.Client(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
        )

    def close(self) -> None:
        if self._owns_http_client:
            self.http.close()

    def __enter__(self) -> MusicBrainzClient:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    @staticmethod
    def _cache_key(path: str, params: dict[str, Any]) -> str:
        canonical = json.dumps(
            [path, sorted((key, str(value)) for key, value in params.items())],
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return f"{MusicBrainzClient.cache_prefix}:{digest}"

    def _read_cache(self, key: str) -> dict[str, Any] | None:
        try:
            cached = self.redis.get(key)
        except RedisError as exc:
            raise MusicBrainzCacheError(f"Redis cache is unavailable: {exc}") from exc
        if cached is None:
            return None
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        try:
            value = json.loads(cached)
        except (TypeError, json.JSONDecodeError):
            try:
                self.redis.delete(key)
            except RedisError:
                pass
            return None
        return value if isinstance(value, dict) else None

    def _write_cache(self, key: str, value: dict[str, Any], *, negative: bool) -> None:
        ttl = self.negative_cache_ttl_seconds if negative else self.cache_ttl_seconds
        try:
            self.redis.setex(
                key,
                ttl,
                json.dumps(value, ensure_ascii=False, separators=(",", ":")),
            )
        except RedisError as exc:
            raise MusicBrainzCacheError(f"Redis cache is unavailable: {exc}") from exc

    def _acquire_rate_slot(self) -> None:
        ttl_ms = max(1000, round(self.rate_limit_seconds * 1000))
        while True:
            try:
                acquired = self.redis.set(
                    self.rate_limit_key,
                    "1",
                    nx=True,
                    px=ttl_ms,
                )
                if acquired:
                    return
                remaining_ms = self.redis.pttl(self.rate_limit_key)
            except RedisError as exc:
                raise MusicBrainzCacheError(
                    f"Redis rate limiter is unavailable: {exc}"
                ) from exc
            wait_seconds = (
                max(0.01, remaining_ms / 1000)
                if isinstance(remaining_ms, int) and remaining_ms > 0
                else self.rate_limit_seconds
            )
            self.sleeper(wait_seconds)

    def _get_json(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        request_params = {**params, "fmt": "json"}
        cache_key = self._cache_key(path, request_params)
        cached = self._read_cache(cache_key)
        if cached is not None:
            return cached

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            self._acquire_rate_slot()
            cached = self._read_cache(cache_key)
            if cached is not None:
                return cached
            try:
                response = self.http.get(
                    f"{self.base_url}/{path.lstrip('/')}",
                    params=request_params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": self.user_agent,
                    },
                )
                if response.status_code in _RETRYABLE_STATUS_CODES:
                    raise httpx.HTTPStatusError(
                        f"Retryable MusicBrainz response: {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise MusicBrainzError(
                        "MusicBrainz returned a non-object JSON body"
                    )
                negative = not payload.get("releases") if path == "release" else False
                self._write_cache(cache_key, payload, negative=negative)
                return payload
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                last_error = exc
                retryable = isinstance(exc, httpx.TransportError) or (
                    exc.response.status_code in _RETRYABLE_STATUS_CODES
                )
                if not retryable or attempt >= self.max_retries:
                    raise MusicBrainzError(str(exc)) from exc
                delay = float(2**attempt)
                if isinstance(exc, httpx.HTTPStatusError):
                    retry_after = exc.response.headers.get("Retry-After")
                    try:
                        delay = max(delay, float(retry_after))
                    except (TypeError, ValueError):
                        pass
                self.sleeper(delay)
        raise MusicBrainzError(str(last_error or "MusicBrainz request failed"))

    @staticmethod
    def _escape_lucene(value: str) -> str:
        return _LUCENE_SPECIAL_RE.sub(r"\\\1", value)

    def search_release(self, artist: str, album: str) -> list[dict[str, Any]]:
        escaped_artist = self._escape_lucene(artist)
        escaped_album = self._escape_lucene(album)
        payload = self._get_json(
            "release",
            {
                "query": f'artist:"{escaped_artist}" AND release:"{escaped_album}"',
                "limit": 5,
            },
        )
        releases = payload.get("releases", [])
        return releases if isinstance(releases, list) else []

    def lookup_release(self, release_mbid: str) -> dict[str, Any]:
        return self._get_json(
            f"release/{release_mbid}",
            {"inc": "recordings+isrcs+artist-credits"},
        )

    def find_release(
        self,
        artist: str,
        album: str,
        *,
        year: int | None = None,
        track_count: int | None = None,
    ) -> ReleaseCandidate | None:
        expected_artist = normalize_catalog_key(artist)
        expected_album = normalize_catalog_key(album)
        candidates: list[tuple[tuple[int, int, int], ReleaseCandidate]] = []

        for item in self.search_release(artist, album):
            mbid = str(item.get("id", ""))
            title = str(item.get("title", ""))
            credited_artist = _artist_credit_name(item.get("artist-credit"))
            try:
                score = int(item.get("score", 0))
            except (TypeError, ValueError):
                score = 0
            if (
                not mbid
                or score < 90
                or normalize_catalog_key(title) != expected_album
                or normalize_catalog_key(credited_artist) != expected_artist
            ):
                continue

            date = str(item.get("date")) if item.get("date") else None
            candidate_track_count = _to_positive_int(item.get("track-count"))
            year_match = int(bool(year and date and date.startswith(str(year))))
            count_match = int(
                bool(
                    track_count
                    and candidate_track_count
                    and track_count == candidate_track_count
                )
            )
            candidate = ReleaseCandidate(
                mbid=mbid,
                title=title,
                artist=credited_artist,
                score=score,
                date=date,
                track_count=candidate_track_count,
            )
            candidates.append(((score, year_match, count_match), candidate))

        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0], reverse=True)
        if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
            return None
        return candidates[0][1]


def _artist_credit_name(credit: Any) -> str:
    if not isinstance(credit, list):
        return ""
    parts: list[str] = []
    for entry in credit:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        artist = entry.get("artist")
        if not name and isinstance(artist, dict):
            name = artist.get("name")
        if name:
            parts.append(str(name))
        if entry.get("joinphrase"):
            parts.append(str(entry["joinphrase"]))
    return "".join(parts).strip()


def _to_positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_isrc(value: Any) -> str | None:
    candidate = re.sub(r"[^A-Za-z0-9]", "", str(value or "")).upper()
    return candidate if _ISRC_RE.fullmatch(candidate) else None


def _remote_tracks(release: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    media = release.get("media", [])
    if not isinstance(media, list):
        return result
    for medium in media:
        if not isinstance(medium, dict):
            continue
        disc_no = _to_positive_int(medium.get("position")) or 1
        remote_tracks = medium.get("tracks", [])
        if not isinstance(remote_tracks, list):
            continue
        for item in remote_tracks:
            if not isinstance(item, dict):
                continue
            recording = item.get("recording")
            if not isinstance(recording, dict):
                recording = {}
            isrcs = {
                valid
                for value in recording.get("isrcs", [])
                if (valid := _normalize_isrc(value)) is not None
            }
            result.append(
                {
                    "disc_no": disc_no,
                    "track_no": _to_positive_int(item.get("position")),
                    "title": str(recording.get("title") or item.get("title") or ""),
                    "duration_ms": _to_positive_int(
                        recording.get("length") or item.get("length")
                    ),
                    "mbid": str(recording.get("id") or ""),
                    "isrcs": sorted(isrcs),
                }
            )
    return result


def _duration_matches(local: int | None, remote: int | None) -> bool:
    return local is None or remote is None or abs(local - remote) <= 2000


def _match_remote_track(
    local_track: Track, remote_tracks: Iterable[dict[str, Any]]
) -> dict[str, Any] | None:
    expected_title = normalize_catalog_key(local_track.title)
    remote_list = list(remote_tracks)
    if local_track.track_no is not None:
        disc_no = local_track.disc_no or 1
        positional = [
            item
            for item in remote_list
            if item["disc_no"] == disc_no
            and item["track_no"] == local_track.track_no
            and normalize_catalog_key(item["title"]) == expected_title
            and _duration_matches(local_track.duration_ms, item["duration_ms"])
        ]
        if len(positional) == 1:
            return positional[0]

    by_title = [
        item
        for item in remote_list
        if normalize_catalog_key(item["title"]) == expected_title
        and _duration_matches(local_track.duration_ms, item["duration_ms"])
    ]
    return by_title[0] if len(by_title) == 1 else None


def enrich_album(
    db: Session,
    album_id: int,
    client: MusicBrainzClient,
) -> dict[str, Any]:
    album = db.scalar(
        select(Album)
        .where(Album.id == album_id)
        .options(selectinload(Album.artist), selectinload(Album.tracks))
    )
    if album is None:
        db.rollback()
        return {"album_id": album_id, "status": "missing_local_album", "tracks": 0}

    artist_name = album.artist.name
    album_title = album.title
    album_year = album.year
    existing_album_mbid = album.mbid
    track_count = len(album.tracks)
    db.commit()

    if existing_album_mbid:
        release_mbid = existing_album_mbid
    else:
        candidate = client.find_release(
            artist_name,
            album_title,
            year=album_year,
            track_count=track_count,
        )
        if candidate is None:
            return {"album_id": album_id, "status": "not_found", "tracks": 0}
        release_mbid = candidate.mbid
    release = client.lookup_release(release_mbid)
    remote_tracks = _remote_tracks(release)

    album = db.scalar(
        select(Album)
        .where(Album.id == album_id)
        .options(selectinload(Album.artist), selectinload(Album.tracks))
    )
    if album is None:
        db.rollback()
        return {"album_id": album_id, "status": "missing_local_album", "tracks": 0}

    if not album.mbid:
        album.mbid = release_mbid

    credit = release.get("artist-credit")
    if isinstance(credit, list) and len(credit) == 1:
        remote_artist = credit[0].get("artist", {})
        if (
            isinstance(remote_artist, dict)
            and normalize_catalog_key(str(remote_artist.get("name", "")))
            == normalize_catalog_key(album.artist.name)
            and not album.artist.mbid
        ):
            album.artist.mbid = str(remote_artist.get("id") or "") or None

    matched = 0
    for local_track in album.tracks:
        remote = _match_remote_track(local_track, remote_tracks)
        if remote is None or not remote["mbid"]:
            continue
        if local_track.mbid and local_track.mbid != remote["mbid"]:
            continue
        if (
            local_track.isrc
            and remote["isrcs"]
            and local_track.isrc not in remote["isrcs"]
        ):
            continue
        if not local_track.mbid:
            local_track.mbid = remote["mbid"]
        if not local_track.isrc and remote["isrcs"]:
            local_track.isrc = remote["isrcs"][0]
        matched += 1
    db.commit()
    return {"album_id": album_id, "status": "enriched", "tracks": matched}


def enrich_albums(
    db: Session,
    album_ids: Iterable[int],
    client: MusicBrainzClient,
    before_album_callback: Callable[[], None] | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    unique_album_ids = sorted(set(album_ids))
    summary: dict[str, Any] = {
        "enriched": 0,
        "not_found": 0,
        "failed": 0,
        "tracks": 0,
        "errors": [],
        "processed": 0,
        "total": len(unique_album_ids),
    }
    for album_id in unique_album_ids:
        if before_album_callback is not None:
            before_album_callback()
        try:
            result = enrich_album(db, album_id, client)
            status = result["status"]
            if status == "enriched":
                summary["enriched"] += 1
                summary["tracks"] += result["tracks"]
            elif status == "not_found":
                summary["not_found"] += 1
            else:
                summary["failed"] += 1
                summary["errors"].append(result)
        except Exception as exc:
            db.rollback()
            summary["failed"] += 1
            summary["errors"].append(
                {"album_id": album_id, "error": type(exc).__name__}
            )
        summary["processed"] += 1
        if progress_callback is not None:
            progress_callback(summary)
    return summary
