"""Qobuz downloads behind the RESTRICT rules of docs/qobuz-dl-assessment.md.

The qobuz-dl package is imported lazily inside factory functions so the app
and tests start without the optional dependency. Secrets, passwords and auth
tokens are never logged, stored in the database, or included in exceptions.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable

from mutagen import File as MutagenFile
from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Match, MatchStatus, Playlist, PlaylistItem

AUDIO_EXTENSIONS = frozenset({".flac", ".mp3"})
_BUNDLE_CACHE_KEY = "qobuz:bundle:v1"
_BUNDLE_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60
_SEARCH_THRESHOLD = 85.0
_DURATION_TOLERANCE_MS = 5_000
_SUPPORTED_URL_TYPES = frozenset({"album", "track"})


class QobuzServiceError(RuntimeError):
    """Base error for Qobuz integration failures."""


class QobuzConfigurationError(QobuzServiceError):
    """Qobuz is disabled, credentials are missing, or the package is absent."""


class QobuzAuthError(QobuzServiceError):
    """Qobuz rejected the configured account credentials."""


class QobuzProviderError(QobuzServiceError):
    """Qobuz could not complete a remote operation."""


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


def is_qobuz_configured(config: Any = settings) -> bool:
    return bool(
        config.qobuz_enabled
        and str(config.qobuz_email or "").strip()
        and config.qobuz_password
    )


def _qobuz_modules() -> SimpleNamespace:
    """Import qobuz-dl lazily; the app must boot without the package."""

    try:
        from qobuz_dl import bundle as bundle_module
        from qobuz_dl import downloader as downloader_module
        from qobuz_dl import qopy as qopy_module
        from qobuz_dl.exceptions import (
            AuthenticationError,
            IneligibleError,
            InvalidAppIdError,
            InvalidAppSecretError,
            InvalidQuality,
            NonStreamable,
        )
        from qobuz_dl.utils import get_url_info
    except ImportError as exc:
        raise QobuzConfigurationError(
            "The qobuz-dl package is not installed in this environment"
        ) from exc
    return SimpleNamespace(
        bundle=bundle_module,
        downloader=downloader_module,
        qopy=qopy_module,
        get_url_info=get_url_info,
        AuthenticationError=AuthenticationError,
        IneligibleError=IneligibleError,
        InvalidAppIdError=InvalidAppIdError,
        InvalidAppSecretError=InvalidAppSecretError,
        InvalidQuality=InvalidQuality,
        NonStreamable=NonStreamable,
    )


def _load_cached_bundle() -> tuple[str, list[str]] | None:
    """Read app_id/secrets from Redis; cache failures are non-fatal."""

    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        raw = client.get(_BUNDLE_CACHE_KEY)
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
        app_id = str(data["app_id"])
        secrets = [str(secret) for secret in data["secrets"]]
    except (KeyError, TypeError, ValueError):
        return None
    if not app_id or not secrets:
        return None
    return app_id, secrets


def _store_bundle_cache(app_id: str, secrets: Iterable[str]) -> None:
    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.set(
            _BUNDLE_CACHE_KEY,
            json.dumps({"app_id": app_id, "secrets": list(secrets)}),
            ex=_BUNDLE_CACHE_TTL_SECONDS,
        )
    except Exception:
        pass


def _drop_bundle_cache() -> None:
    try:
        from redis import Redis

        client = Redis.from_url(settings.redis_url, decode_responses=True)
        client.delete(_BUNDLE_CACHE_KEY)
    except Exception:
        pass


def _fetch_bundle() -> tuple[str, list[str]]:
    modules = _qobuz_modules()
    try:
        bundle = modules.bundle.Bundle()
        app_id = str(bundle.get_app_id())
        secrets = [str(secret) for secret in bundle.get_secrets().values()]
    except Exception as exc:
        raise QobuzProviderError(
            "Qobuz app bundle could not be extracted from play.qobuz.com"
        ) from exc
    if not app_id or not secrets:
        raise QobuzProviderError("Qobuz app bundle extraction returned no secrets")
    _store_bundle_cache(app_id, secrets)
    return app_id, secrets


def _get_bundle(*, force_refresh: bool = False) -> tuple[str, list[str]]:
    if not force_refresh:
        cached = _load_cached_bundle()
        if cached is not None:
            return cached
    return _fetch_bundle()


def create_qobuz_client():
    """Build an authenticated qopy.Client from env settings only.

    The password leaves the process exclusively as an MD5 hex digest, exactly
    like the upstream CLI does before initializing qopy.Client.
    """

    modules = _qobuz_modules()
    if not is_qobuz_configured():
        raise QobuzConfigurationError(
            "QOBUZ_ENABLED, QOBUZ_EMAIL and QOBUZ_PASSWORD must be configured"
        )
    password_md5 = hashlib.md5(settings.qobuz_password.encode("utf-8")).hexdigest()
    for attempt in range(2):
        app_id, secrets = _get_bundle(force_refresh=attempt == 1)
        try:
            return modules.qopy.Client(
                settings.qobuz_email.strip(),
                password_md5,
                app_id,
                secrets,
            )
        except modules.InvalidAppSecretError as exc:
            if attempt == 0:
                _drop_bundle_cache()
                continue
            raise QobuzProviderError(
                "Qobuz app secret is invalid even after a bundle refresh"
            ) from exc
        except (modules.AuthenticationError, modules.IneligibleError) as exc:
            raise QobuzAuthError(
                "Qobuz rejected the configured credentials"
            ) from exc
        except modules.InvalidAppIdError as exc:
            raise QobuzAuthError("Qobuz rejected the extracted app id") from exc
        except QobuzServiceError:
            raise
        except Exception as exc:
            raise QobuzProviderError("Qobuz client initialization failed") from exc
    raise QobuzProviderError("Qobuz client initialization failed")


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
        isrc=str(raw.get("isrc") or "").strip() or None,
        hires=bool(raw.get("hires_streamable")),
        url=f"https://play.qobuz.com/track/{qobuz_id}",
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
    )


def _search_items(response: Any, key: str) -> list[Any]:
    if not isinstance(response, dict):
        return []
    container = response.get(key)
    if not isinstance(container, dict):
        return []
    items = container.get("items")
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


def select_best_track_candidate(
    *,
    artist_raw: str | None,
    title_raw: str | None,
    duration_ms: int | None,
    candidates: Iterable[QobuzSearchCandidate],
) -> QobuzSearchCandidate | None:
    """Pick the best fuzzy match like the library matcher does (85 / ±5 s)."""

    item_key = f"{artist_raw or ''} {title_raw or ''}".strip()
    if not item_key:
        return None
    best: QobuzSearchCandidate | None = None
    best_score = 0.0
    for candidate in candidates:
        if (
            duration_ms is not None
            and candidate.duration_ms is not None
            and abs(duration_ms - candidate.duration_ms) > _DURATION_TOLERANCE_MS
        ):
            continue
        score = float(
            token_set_ratio(item_key, f"{candidate.artist} {candidate.title}")
        )
        if score < _SEARCH_THRESHOLD or score <= best_score:
            continue
        best = candidate
        best_score = score
    return best


def _staging_snapshot(staging: Path) -> set[Path]:
    if not staging.exists():
        return set()
    return {
        path.resolve()
        for path in staging.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def verify_staging_files(files: Iterable[Path]) -> tuple[list[Path], list[dict]]:
    """Keep only parseable audio; everything else stays for the report."""

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


def _new_verified_files(staging: Path, before: set[Path]) -> tuple[list[Path], list[dict]]:
    new_files = sorted(_staging_snapshot(staging) - before)
    return verify_staging_files(new_files)


def _map_download_error(modules: SimpleNamespace, exc: Exception) -> QobuzServiceError:
    if isinstance(exc, QobuzServiceError):
        return exc
    if isinstance(exc, (modules.AuthenticationError, modules.IneligibleError)):
        return QobuzAuthError("Qobuz rejected the configured credentials")
    if isinstance(exc, modules.InvalidAppSecretError):
        return QobuzProviderError("Qobuz app secret was rejected during download")
    if isinstance(exc, modules.InvalidQuality):
        return QobuzConfigurationError(
            "QOBUZ_QUALITY must be one of 5, 6, 7 or 27"
        )
    if isinstance(exc, modules.NonStreamable):
        return QobuzProviderError("Qobuz item is not streamable")
    return QobuzProviderError("Qobuz download failed")


def download_track_to_staging(
    client: Any,
    track_id: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    modules = _qobuz_modules()
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    before = _staging_snapshot(staging)
    try:
        modules.downloader.Download(
            client,
            str(track_id),
            str(staging),
            int(quality),
            embed_art=embed_art,
            downgrade_quality=True,
        ).download_id_by_type(track=True)
    except Exception as exc:
        raise _map_download_error(modules, exc) from exc
    verified, _rejected = _new_verified_files(staging, before)
    return verified


def download_url_to_staging(
    client: Any,
    url: str,
    staging_dir: str | Path,
    quality: int,
    embed_art: bool,
) -> list[Path]:
    modules = _qobuz_modules()
    info = modules.get_url_info(str(url or ""))
    if not info:
        raise QobuzProviderError("URL is not a recognized Qobuz link")
    kind, item_id = info
    if kind not in _SUPPORTED_URL_TYPES:
        raise QobuzProviderError(
            f"Qobuz '{kind}' URLs are not supported in the MVP; "
            "only album and track URLs can be downloaded"
        )
    staging = Path(staging_dir)
    staging.mkdir(parents=True, exist_ok=True)
    before = _staging_snapshot(staging)
    try:
        modules.downloader.Download(
            client,
            str(item_id),
            str(staging),
            int(quality),
            embed_art=embed_art,
            downgrade_quality=True,
        ).download_id_by_type(track=kind == "track")
    except Exception as exc:
        raise _map_download_error(modules, exc) from exc
    verified, _rejected = _new_verified_files(staging, before)
    return verified


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
    """Move verified audio into the library without overwriting anything."""

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


def _missing_items(db: Session, playlist: Playlist, limit: int) -> list[PlaylistItem]:
    statement = (
        select(PlaylistItem)
        .join(Match, Match.playlist_item_id == PlaylistItem.id)
        .where(
            PlaylistItem.playlist_id == playlist.id,
            Match.status == MatchStatus.missing,
        )
        .order_by(PlaylistItem.position, PlaylistItem.id)
        .limit(limit)
    )
    return list(db.scalars(statement))


def fetch_missing_tracks(
    db: Session,
    playlist: Playlist,
    client: Any,
    progress_callback: Callable[[dict], None] | None = None,
) -> tuple[dict, list[Path]]:
    """Download MISSING playlist items into the staging area, one by one."""

    max_tracks = settings.qobuz_max_tracks_per_run
    items = _missing_items(db, playlist, max_tracks)
    summary: dict[str, Any] = {
        "playlist_id": playlist.id,
        "total_missing": len(items),
        "attempted": 0,
        "downloaded": 0,
        "not_found": 0,
        "failed": 0,
        "items": [],
    }
    collected: list[Path] = []
    for item in items:
        entry: dict[str, Any] = {
            "item_id": item.id,
            "artist": item.artist_raw,
            "title": item.title_raw,
        }
        query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
        try:
            if not query:
                raise QobuzProviderError("Playlist item has no artist/title query")
            summary["attempted"] += 1
            candidates = search_tracks(client, query, limit=5)
            best = select_best_track_candidate(
                artist_raw=item.artist_raw,
                title_raw=item.title_raw,
                duration_ms=item.duration_ms,
                candidates=candidates,
            )
            if best is None:
                summary["not_found"] += 1
                entry["status"] = "not_found"
            else:
                entry["qobuz_track_id"] = best.qobuz_id
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
                else:
                    summary["failed"] += 1
                    entry["status"] = "failed"
                    entry["error"] = "download produced no verified audio"
            time.sleep(settings.qobuz_request_delay_seconds)
        except QobuzServiceError as exc:
            summary["failed"] += 1
            entry["status"] = "failed"
            entry["error"] = type(exc).__name__
        summary["items"].append(entry)
        if progress_callback is not None:
            progress_callback(
                {key: value for key, value in summary.items() if key != "items"}
            )
    return summary, collected
