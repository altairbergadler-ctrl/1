"""Trusted Music Service boundary for the isolated Qobuz downloader.

The third-party qobuz-dl package is not installed in this process.  Provider
credentials and outbound internet access live only in ``qobuz-sidecar``.  The
main worker receives relative staging paths, verifies every file again, and is
the only component allowed to move audio into the library.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

import httpx
from mutagen import File as MutagenFile
from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Match, MatchStatus, Playlist, PlaylistItem, ProviderAttempt
from app.services.credentials import (
    CredentialError,
    credential_envelope,
    get_credential_record,
)
from app.services.matcher import normalize_isrc
from app.services.normalize import normalize_album, normalize_artist, normalize_title

AUDIO_EXTENSIONS = frozenset({".flac"})
_EXACT_DURATION_TOLERANCE_MS = 2_000
_FUZZY_DURATION_TOLERANCE_MS = 3_000
_FUZZY_AUTO_THRESHOLD = 95.0
_FUZZY_COMPONENT_THRESHOLD = 92.0
_FUZZY_AMBIGUITY_GAP = 5.0
_SUPPORTED_URL_TYPES = frozenset({"album", "track"})
_VERSION_MARKERS = {
    "live": re.compile(r"(?<!\w)live(?!\w)", re.IGNORECASE),
    "remix": re.compile(r"(?<!\w)remix(?:ed)?(?!\w)", re.IGNORECASE),
    "cover": re.compile(r"(?<!\w)cover(?!\w)", re.IGNORECASE),
    "acoustic": re.compile(r"(?<!\w)acoustic(?!\w)", re.IGNORECASE),
    "instrumental": re.compile(r"(?<!\w)instrumental(?!\w)", re.IGNORECASE),
    "radio": re.compile(r"(?<!\w)radio\s+edit(?!\w)", re.IGNORECASE),
    "remaster": re.compile(r"(?<!\w)remaster(?:ed)?(?!\w)", re.IGNORECASE),
}


class QobuzServiceError(RuntimeError):
    """Base error for Qobuz integration failures."""


class QobuzConfigurationError(QobuzServiceError):
    """The isolated sidecar is disabled or cannot be configured."""


class QobuzAuthError(QobuzServiceError):
    """Qobuz rejected the provider credential held by the sidecar."""


class QobuzProviderError(QobuzServiceError):
    """Qobuz or the isolated sidecar could not complete an operation."""


class QobuzRateLimitedError(QobuzProviderError):
    """Qobuz explicitly asked the account to slow down."""


@dataclass(frozen=True, slots=True)
class QobuzSearchCandidate:
    qobuz_id: str
    artist: str
    title: str
    album: str
    duration_ms: int | None
    isrc: str | None
    hires: bool
    url: str
    version: str = ""
    maximum_bit_depth: int | None = None
    maximum_sampling_rate: int | None = None

    @property
    def quality_rank(self) -> tuple[int, int, int]:
        return (
            int(self.maximum_bit_depth or 0),
            int(self.maximum_sampling_rate or 0),
            int(self.hires),
        )


class QobuzSidecarClient:
    """Small authenticated client for the private sidecar control API."""

    def __init__(
        self,
        base_url: str,
        internal_token: str,
        credential: dict[str, Any] | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.internal_token = internal_token
        self.credential = credential
        self.label: str | None = None

    def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        read_timeout: float | None = None,
    ) -> dict[str, Any]:
        timeout = httpx.Timeout(
            connect=settings.qobuz_sidecar_connect_timeout_seconds,
            read=read_timeout or settings.qobuz_sidecar_read_timeout_seconds,
            write=30.0,
            pool=5.0,
        )
        request_payload = dict(payload or {})
        if path != "/status" and "credential" not in request_payload:
            if self.credential is None:
                raise QobuzConfigurationError("Qobuz credential is not configured")
            request_payload["credential"] = self.credential
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers={"Authorization": f"Bearer {self.internal_token}"},
                json=request_payload if method != "GET" else None,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise QobuzProviderError("The isolated Qobuz sidecar timed out") from exc
        except httpx.HTTPError as exc:
            raise QobuzProviderError("The isolated Qobuz sidecar is unavailable") from exc
        if response.status_code == 429:
            raise QobuzRateLimitedError("Qobuz rate limit is active")
        if response.status_code in (400, 401):
            raise QobuzAuthError("Qobuz rejected the configured credential")
        if response.status_code == 503:
            raise QobuzConfigurationError("The isolated Qobuz sidecar is not configured")
        if response.status_code >= 400:
            raise QobuzProviderError(
                f"The isolated Qobuz sidecar failed with HTTP {response.status_code}"
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise QobuzProviderError("The isolated Qobuz sidecar returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise QobuzProviderError("The isolated Qobuz sidecar returned an invalid payload")
        return data

    def status(self) -> dict[str, Any]:
        return self._request("GET", "/status")

    def connect(self) -> dict[str, Any]:
        result = self._request("POST", "/connect", payload={})
        self.label = str(result.get("label") or "") or None
        return result

    def validate_credential(self, envelope: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST", "/credentials/validate", payload={"credential": envelope}
        )
        self.label = str(result.get("label") or "") or None
        return result

    def search_tracks(self, query: str, limit: int) -> dict[str, Any]:
        return self._request(
            "POST", "/search", payload={"query": query, "kind": "track", "limit": limit}
        ).get("result", {})

    def search_albums(self, query: str, limit: int) -> dict[str, Any]:
        return self._request(
            "POST", "/search", payload={"query": query, "kind": "album", "limit": limit}
        ).get("result", {})

    def download_track(self, track_id: str, quality: int, embed_art: bool) -> list[str]:
        data = self._request(
            "POST",
            "/download/track",
            payload={"track_id": track_id, "quality": quality, "embed_art": embed_art},
        )
        files = data.get("files")
        return [str(path) for path in files] if isinstance(files, list) else []

    def download_url(self, url: str, quality: int, embed_art: bool) -> list[str]:
        data = self._request(
            "POST",
            "/download/url",
            payload={"url": url, "quality": quality, "embed_art": embed_art},
        )
        files = data.get("files")
        return [str(path) for path in files] if isinstance(files, list) else []


def is_qobuz_configured(config: Any = settings) -> bool:
    return bool(
        config.qobuz_enabled
        and str(config.qobuz_sidecar_url or "").strip()
        and len(str(config.qobuz_internal_token or "").strip()) >= 32
    )


def create_qobuz_client(db: Session) -> QobuzSidecarClient:
    if not is_qobuz_configured():
        raise QobuzConfigurationError(
            "QOBUZ_ENABLED, QOBUZ_SIDECAR_URL and QOBUZ_INTERNAL_TOKEN are required"
        )
    record = get_credential_record(db, "qobuz")
    if record is None:
        raise QobuzConfigurationError("Qobuz credential is not configured")
    client = QobuzSidecarClient(
        settings.qobuz_sidecar_url,
        settings.qobuz_internal_token,
        credential_envelope(record),
    )
    client.connect()
    return client


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _track_candidate(raw: Any) -> QobuzSearchCandidate | None:
    if not isinstance(raw, dict):
        return None
    qobuz_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not qobuz_id or not title:
        return None
    performer = raw.get("performer") if isinstance(raw.get("performer"), dict) else {}
    album = raw.get("album") if isinstance(raw.get("album"), dict) else {}
    duration_seconds = _positive_int(raw.get("duration"))
    return QobuzSearchCandidate(
        qobuz_id=qobuz_id,
        artist=str(performer.get("name") or "").strip(),
        title=title,
        album=str(album.get("title") or "").strip(),
        duration_ms=duration_seconds * 1000 if duration_seconds else None,
        isrc=normalize_isrc(str(raw.get("isrc") or "")),
        hires=bool(raw.get("hires_streamable")),
        url=f"https://play.qobuz.com/track/{qobuz_id}",
        version=str(raw.get("version") or "").strip(),
        maximum_bit_depth=_positive_int(raw.get("maximum_bit_depth")),
        maximum_sampling_rate=_positive_int(raw.get("maximum_sampling_rate")),
    )


def _album_candidate(raw: Any) -> QobuzSearchCandidate | None:
    if not isinstance(raw, dict):
        return None
    qobuz_id = str(raw.get("id") or "").strip()
    title = str(raw.get("title") or "").strip()
    if not qobuz_id or not title:
        return None
    artist = raw.get("artist") if isinstance(raw.get("artist"), dict) else {}
    duration_seconds = _positive_int(raw.get("duration"))
    return QobuzSearchCandidate(
        qobuz_id=qobuz_id,
        artist=str(artist.get("name") or "").strip(),
        title=title,
        album=title,
        duration_ms=duration_seconds * 1000 if duration_seconds else None,
        isrc=None,
        hires=bool(raw.get("hires_streamable")),
        url=f"https://play.qobuz.com/album/{qobuz_id}",
        maximum_bit_depth=_positive_int(raw.get("maximum_bit_depth")),
        maximum_sampling_rate=_positive_int(raw.get("maximum_sampling_rate")),
    )


def _search_items(response: Any, key: str) -> list[Any]:
    if not isinstance(response, dict) or not isinstance(response.get(key), dict):
        return []
    items = response[key].get("items")
    return items if isinstance(items, list) else []


def search_tracks(client: Any, query: str, limit: int) -> list[QobuzSearchCandidate]:
    try:
        response = client.search_tracks(query, limit)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz track search failed") from exc
    return [
        candidate
        for candidate in (_track_candidate(raw) for raw in _search_items(response, "tracks"))
        if candidate is not None
    ]


def search_albums(client: Any, query: str, limit: int) -> list[QobuzSearchCandidate]:
    try:
        response = client.search_albums(query, limit)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz album search failed") from exc
    return [
        candidate
        for candidate in (_album_candidate(raw) for raw in _search_items(response, "albums"))
        if candidate is not None
    ]


def _version_markers(*values: str | None) -> frozenset[str]:
    text = " ".join(unicodedata.normalize("NFKC", value or "") for value in values)
    return frozenset(
        marker for marker, pattern in _VERSION_MARKERS.items() if pattern.search(text)
    )


def _duration_within(left: int | None, right: int | None, tolerance: int) -> bool:
    return left is None or right is None or abs(left - right) <= tolerance


def choose_track_candidate(
    *,
    artist_raw: str | None,
    title_raw: str | None,
    album_raw: str | None,
    isrc: str | None,
    duration_ms: int | None,
    candidates: Iterable[QobuzSearchCandidate],
) -> tuple[QobuzSearchCandidate | None, str]:
    """Choose only a deterministic recording; ambiguous candidates stay missing."""

    candidates = list(candidates)
    item_isrc = normalize_isrc(isrc)
    if item_isrc:
        # ISRC identifies the recording and therefore takes precedence over
        # title/album edition labels.  Duplicate catalogue entries for the
        # same ISRC are resolved only by the best published quality.
        isrc_matches = [candidate for candidate in candidates if candidate.isrc == item_isrc]
        if isrc_matches:
            return max(isrc_matches, key=lambda candidate: candidate.quality_rank), "isrc"

    item_markers = _version_markers(title_raw, album_raw)
    compatible = [
        candidate
        for candidate in candidates
        if item_markers
        == _version_markers(candidate.title, candidate.version, candidate.album)
    ]

    item_artist = normalize_artist(artist_raw)
    item_title = normalize_title(title_raw)
    item_album = normalize_album(album_raw)
    exact = [
        candidate
        for candidate in compatible
        if normalize_artist(candidate.artist) == item_artist
        and normalize_title(f"{candidate.title} {candidate.version}".strip()) == item_title
        and _duration_within(
            duration_ms, candidate.duration_ms, _EXACT_DURATION_TOLERANCE_MS
        )
    ]
    if exact:
        album_exact = [
            candidate
            for candidate in exact
            if item_album and normalize_album(candidate.album) == item_album
        ]
        if album_exact:
            exact = album_exact
        identities = {
            ("isrc", candidate.isrc)
            if candidate.isrc
            else ("qobuz_id", candidate.qobuz_id)
            for candidate in exact
        }
        if len(identities) > 1:
            return None, "ambiguous"
        exact.sort(
            key=lambda candidate: candidate.quality_rank,
            reverse=True,
        )
        return exact[0], "exact"

    if not item_artist or not item_title or duration_ms is None:
        return None, "insufficient_metadata"

    ranked: list[tuple[float, QobuzSearchCandidate]] = []
    for candidate in compatible:
        if candidate.duration_ms is None or not _duration_within(
            duration_ms, candidate.duration_ms, _FUZZY_DURATION_TOLERANCE_MS
        ):
            continue
        artist_score = float(token_set_ratio(item_artist, normalize_artist(candidate.artist)))
        title_score = float(
            token_set_ratio(
                item_title,
                normalize_title(f"{candidate.title} {candidate.version}".strip()),
            )
        )
        combined = float(
            token_set_ratio(
                f"{item_artist} {item_title}",
                f"{normalize_artist(candidate.artist)} "
                f"{normalize_title(f'{candidate.title} {candidate.version}'.strip())}",
            )
        )
        score = min(combined, (artist_score * 0.4) + (title_score * 0.6))
        if (
            score >= _FUZZY_AUTO_THRESHOLD
            and artist_score >= _FUZZY_COMPONENT_THRESHOLD
            and title_score >= _FUZZY_COMPONENT_THRESHOLD
        ):
            ranked.append((score, candidate))
    ranked.sort(key=lambda item: (item[0], item[1].quality_rank), reverse=True)
    if not ranked:
        return None, "not_found"
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < _FUZZY_AMBIGUITY_GAP:
        top, runner_up = ranked[0][1], ranked[1][1]
        same_recording = (
            bool(top.isrc)
            and top.isrc == runner_up.isrc
        ) or top.qobuz_id == runner_up.qobuz_id
        if not same_recording:
            return None, "ambiguous"
    return ranked[0][1], "fuzzy"


def select_best_track_candidate(
    *,
    artist_raw: str | None,
    title_raw: str | None,
    duration_ms: int | None,
    candidates: Iterable[QobuzSearchCandidate],
    album_raw: str | None = None,
    isrc: str | None = None,
) -> QobuzSearchCandidate | None:
    candidate, _reason = choose_track_candidate(
        artist_raw=artist_raw,
        title_raw=title_raw,
        album_raw=album_raw,
        isrc=isrc,
        duration_ms=duration_ms,
        candidates=candidates,
    )
    return candidate


def verify_staging_files(files: Iterable[Path]) -> tuple[list[Path], list[dict]]:
    verified: list[Path] = []
    rejected: list[dict] = []
    for file in files:
        path = Path(file)
        reason: str | None = None
        if path.suffix.casefold() not in AUDIO_EXTENSIONS:
            reason = "unsupported extension"
        elif not path.is_file() or path.stat().st_size <= 0:
            reason = "empty or missing file"
        else:
            try:
                audio = MutagenFile(path)
            except Exception:
                audio = None
            length = getattr(getattr(audio, "info", None), "length", None)
            if audio is None:
                reason = "mutagen could not parse the file"
            elif not isinstance(length, (int, float)) or length <= 0:
                reason = "audio has no duration"
        if reason is None:
            verified.append(path)
        else:
            rejected.append({"path": str(path), "reason": reason})
    return verified, rejected


def _sidecar_paths(relative_paths: Iterable[str], staging_dir: str | Path) -> list[Path]:
    staging = Path(staging_dir).expanduser().resolve()
    paths: list[Path] = []
    for relative_path in relative_paths:
        candidate = (staging / relative_path).resolve()
        if not candidate.is_relative_to(staging):
            raise QobuzProviderError("The Qobuz sidecar returned an unsafe staging path")
        paths.append(candidate)
    verified, _rejected = verify_staging_files(paths)
    return verified


def download_track_to_staging(
    client: Any,
    track_id: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    try:
        relative_paths = client.download_track(str(track_id), int(quality), embed_art)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz track download failed") from exc
    return _sidecar_paths(relative_paths, staging_dir)


def download_url_to_staging(
    client: Any,
    url: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    try:
        relative_paths = client.download_url(str(url), int(quality), embed_art)
    except QobuzServiceError:
        raise
    except Exception as exc:
        raise QobuzProviderError("Qobuz URL download failed") from exc
    return _sidecar_paths(relative_paths, staging_dir)


def _cleanup_empty_staging_dirs(staging: Path) -> None:
    if not staging.exists():
        return
    directories = sorted(
        (path for path in staging.rglob("*") if path.is_dir()),
        key=lambda path: len(path.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass


def import_files_to_library(
    files: Iterable[Path],
    staging_dir: str | Path,
    library_path: str | Path,
) -> dict:
    staging_root = Path(staging_dir).expanduser().resolve()
    library_root = Path(library_path).expanduser().resolve()
    library_root.mkdir(parents=True, exist_ok=True)
    report: dict[str, list] = {"imported": [], "conflicts": [], "rejected": []}
    for file in files:
        source = Path(file).expanduser().resolve()
        try:
            relative = source.relative_to(staging_root)
        except ValueError:
            report["rejected"].append(
                {"path": str(file), "reason": "file is outside the staging area"}
            )
            continue
        target = (library_root / relative).resolve()
        if not target.is_relative_to(library_root):
            report["rejected"].append(
                {"path": str(file), "reason": "target escapes the library root"}
            )
            continue
        if target.exists():
            report["conflicts"].append(
                {"path": str(file), "target": str(target), "reason": "already exists"}
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(target))
        report["imported"].append(str(target))
    _cleanup_empty_staging_dirs(staging_root)
    return report


def provider_lookup_key(item: PlaylistItem) -> str:
    """Stable identity shared by equal playlist rows and future providers."""

    isrc = normalize_isrc(item.isrc)
    if isrc:
        identity = ("isrc", isrc)
    else:
        identity = (
            "metadata",
            item.artist_norm or normalize_artist(item.artist_raw),
            item.title_norm or normalize_title(item.title_raw),
            item.album_norm or normalize_album(item.album_raw),
            str(item.duration_ms or ""),
        )
    return hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()


def missing_provider_items(
    db: Session, playlist: Playlist, provider: str
) -> tuple[list[PlaylistItem], list[PlaylistItem]]:
    statement = (
        select(PlaylistItem)
        .join(Match, Match.playlist_item_id == PlaylistItem.id)
        .where(
            PlaylistItem.playlist_id == playlist.id,
            Match.status == MatchStatus.missing,
        )
        .order_by(PlaylistItem.position, PlaylistItem.id)
    )
    all_items = list(db.scalars(statement))
    lookup_keys = {provider_lookup_key(item) for item in all_items}
    attempted_keys = set()
    if lookup_keys:
        attempts = db.execute(
            select(
                ProviderAttempt.provider,
                ProviderAttempt.lookup_key,
                ProviderAttempt.status,
            ).where(ProviderAttempt.lookup_key.in_(lookup_keys))
        )
        attempted_keys = {
            lookup_key
            for provider_name, lookup_key, status in attempts
            if provider_name == provider or status in {"stored", "conflict"}
        }
    seen_keys = set(attempted_keys)
    eligible: list[PlaylistItem] = []
    for item in all_items:
        lookup_key = provider_lookup_key(item)
        if lookup_key in seen_keys:
            continue
        seen_keys.add(lookup_key)
        eligible.append(item)
    return all_items, eligible


def qobuz_download_eligibility(db: Session, playlist: Playlist) -> dict[str, int]:
    all_missing, eligible = missing_provider_items(db, playlist, "qobuz")
    return {
        "total_missing": len(all_missing),
        "eligible": len(eligible),
        "already_checked": len(all_missing) - len(eligible),
    }


def mark_downloads_stored(
    downloads: dict,
    import_report: dict,
    staging_dir: str | Path,
    library_path: str | Path,
) -> None:
    """Promote per-track download states after files move into the library."""

    staging_root = Path(staging_dir).expanduser().resolve()
    library_root = Path(library_path).expanduser().resolve()
    imported = {
        str(Path(path).expanduser().resolve()) for path in import_report["imported"]
    }
    conflicts = {
        str(Path(entry["target"]).expanduser().resolve())
        for entry in import_report["conflicts"]
    }
    stored = 0
    conflicted = 0
    import_failed = 0
    for entry in downloads.get("items", []):
        files = [Path(path).expanduser().resolve() for path in entry.pop("files", [])]
        if files:
            entry["file_count"] = len(files)
        if entry.get("status") != "downloaded":
            continue
        targets: list[str] = []
        try:
            targets = [
                str((library_root / source.relative_to(staging_root)).resolve())
                for source in files
            ]
        except ValueError:
            targets = []
        if targets and all(target in imported for target in targets):
            entry["status"] = "stored"
            stored += 1
        elif targets and any(target in conflicts for target in targets):
            entry["status"] = "conflict"
            conflicted += 1
        else:
            entry["status"] = "failed"
            entry["error"] = "library import failed"
            import_failed += 1
    downloads["stored"] = stored
    downloads["conflicts"] = conflicted
    downloads["import_failed"] = import_failed


def _record_qobuz_attempt(
    db: Session,
    item: PlaylistItem,
    entry: dict,
    job_id: int | None,
) -> None:
    lookup_key = provider_lookup_key(item)
    attempt = db.scalar(
        select(ProviderAttempt).where(
            ProviderAttempt.provider == "qobuz",
            ProviderAttempt.lookup_key == lookup_key,
        )
    )
    if attempt is None:
        attempt = ProviderAttempt(provider="qobuz", lookup_key=lookup_key)
        db.add(attempt)
    attempt.playlist_item_id = item.id
    attempt.job_id = job_id
    attempt.status = str(entry.get("status") or "failed")
    attempt.provider_item_id = entry.get("qobuz_track_id")
    attempt.selection_method = entry.get("selection")
    attempt.error_code = entry.get("error")
    db.flush()


def record_qobuz_download_attempts(
    db: Session,
    downloads: dict,
    playlist_items: dict[int, PlaylistItem],
    job_id: int | None,
) -> None:
    """Persist final stored/conflict outcomes after the import phase."""

    for entry in downloads.get("items", []):
        item = playlist_items.get(int(entry["item_id"]))
        if item is not None:
            _record_qobuz_attempt(db, item, entry, job_id)


def fetch_missing_tracks(
    db: Session,
    playlist: Playlist,
    client: Any,
    progress_callback: Callable[[dict], None] | None = None,
    job_id: int | None = None,
) -> tuple[dict, list[Path]]:
    all_missing, items = missing_provider_items(db, playlist, "qobuz")
    batch_size = settings.qobuz_max_tracks_per_run
    batch_count = (len(items) + batch_size - 1) // batch_size
    entries: list[dict[str, Any]] = [
        {
            "item_id": item.id,
            "artist": item.artist_raw,
            "title": item.title_raw,
            "status": "queued",
        }
        for item in items
    ]
    summary: dict[str, Any] = {
        "playlist_id": playlist.id,
        "total_missing": len(all_missing),
        "eligible_total": len(items),
        "skipped_same_source": len(all_missing) - len(items),
        "batch_size": batch_size,
        "batch_count": batch_count,
        "current_batch": 0,
        "current_batch_size": 0,
        "batch_processed": 0,
        "batch_pause_seconds": 0,
        "processed": 0,
        "attempted": 0,
        "downloaded": 0,
        "not_found": 0,
        "ambiguous": 0,
        "failed": 0,
        "items": entries,
    }
    collected: list[Path] = []
    if progress_callback is not None:
        progress_callback(summary)
    for batch_index, batch_start in enumerate(range(0, len(items), batch_size)):
        batch_items = items[batch_start : batch_start + batch_size]
        batch_entries = entries[batch_start : batch_start + batch_size]
        summary["current_batch"] = batch_index + 1
        summary["current_batch_size"] = len(batch_items)
        summary["batch_processed"] = 0
        summary["batch_pause_seconds"] = 0
        summary["batch_state"] = "running"
        if progress_callback is not None:
            progress_callback(summary)
        for item, entry in zip(batch_items, batch_entries, strict=True):
            entry["status"] = "searching"
            if progress_callback is not None:
                progress_callback(summary)
            query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
            try:
                if not query:
                    raise QobuzProviderError("Playlist item has no artist/title query")
                summary["attempted"] += 1
                candidates = search_tracks(client, query, limit=10)
                best, method = choose_track_candidate(
                    artist_raw=item.artist_raw,
                    title_raw=item.title_raw,
                    album_raw=item.album_raw,
                    isrc=item.isrc,
                    duration_ms=item.duration_ms,
                    candidates=candidates,
                )
                if best is None:
                    status = "ambiguous" if method == "ambiguous" else "not_found"
                    summary[status] += 1
                    entry["status"] = status
                    entry["selection"] = method
                else:
                    entry["qobuz_track_id"] = best.qobuz_id
                    entry["selection"] = method
                    entry["status"] = "downloading"
                    if progress_callback is not None:
                        progress_callback(summary)
                    files = download_track_to_staging(
                        client,
                        best.qobuz_id,
                        settings.qobuz_staging_path,
                        settings.qobuz_quality,
                        settings.qobuz_embed_art,
                    )
                    if files:
                        collected.extend(files)
                        summary["downloaded"] += 1
                        entry["status"] = "downloaded"
                        entry["files"] = [str(path) for path in files]
                    else:
                        summary["failed"] += 1
                        entry["status"] = "failed"
                        entry["error"] = "download produced no verified audio"
            except QobuzServiceError as exc:
                summary["failed"] += 1
                entry["status"] = "failed"
                entry["error"] = type(exc).__name__
            if entry["status"] != "downloaded":
                _record_qobuz_attempt(db, item, entry, job_id)
            summary["processed"] += 1
            summary["batch_processed"] += 1
            if progress_callback is not None:
                progress_callback(summary)
            if settings.qobuz_request_delay_seconds:
                time.sleep(settings.qobuz_request_delay_seconds)
        if batch_index + 1 < batch_count:
            summary["batch_state"] = "paused"
            summary["batch_pause_seconds"] = settings.qobuz_batch_delay_seconds
            if progress_callback is not None:
                progress_callback(summary)
            if settings.qobuz_batch_delay_seconds:
                time.sleep(settings.qobuz_batch_delay_seconds)
    return summary, collected
