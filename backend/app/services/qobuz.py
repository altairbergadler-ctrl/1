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
_COVER_ART_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
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
                f"{noïmm¢G§²ÚîÆ­yÒ&V6öâÒ&×WFvVâ6÷VÆBæ÷B'6RF†Rf–ÆR ¢VÆ–bæ÷B—6–ç7Fæ6R†ÆVæwF‚Â†–çBÂfÆöB’’÷"ÆVæwF‚ÃÒ ¢&V6öâÒ&VF–ò†2æòGW&F–öâ ¢–b&V6öâ—2æöæS ¢fW&–f–VBæVæB‡F‚¢VÇ6S ¢&V¦V7FVBæVæB‡²'F‚#¢7G"‡F‚’Â'&V6öâ#¢&V6öçÒ¢&WGW&âfW&–f–VBÂ&V¦V7FV@  ¦FVb÷6–FV6%÷F‡2‡&VÆF—fU÷F‡3¢—FW&&ÆU·7G%ÒÂ7Fv–æuöF—#¢7G"ÂF‚’ÓâÆ—7EµF…Ó ¢7Fv–ærÒF‚‡7Fv–æuöF—"’æW‡æGW6W"‚’ç&W6öÇfR‚¢F‡3¢Æ—7EµF…ÒÒµÐ¢f÷"&VÆF—fU÷F‚–â&VÆF—fU÷F‡3 ¢6æF–FFRÒ‡7Fv–ærò&VÆF—fU÷F‚’ç&W6öÇfR‚¢–bæ÷B6æF–FFRæ—5÷&VÆF—fU÷Fò‡7Fv–ær“ ¢&—6Rö'W¥&÷f–FW$W'&÷"‚%F†Rö'W¢6–FV6"&WGW&æVBâVç6fR7Fv–ærF‚"¢F‡2æVæB†6æF–FFR¢fW&–f–VBÂ&V¦V7FVBÒfW&–g•÷7Fv–æuöf–ÆW2‡F‡2¢2F†R6–FV6"Ö’&WGW&âF÷væÆöFVB6÷fW"'BÆöæw6–FRF†R&WVW7FV@¢2VF–òâ6÷fW'2&RFV×÷&'’7V—6—F–öâ'F–f7G3¢¶VWVæ¶æ÷vâ&V¦V7FV@¢2f–ÆW2f÷"–ç7V7F–öâÂ'WB&VÖ÷fR¶æ÷vâ–ÖvRf÷&ÖG2–ç6–FR7Fv–ærà¢f÷"VçG'’–â&V¦V7FVC ¢'F–f7BÒF‚‡7G"†VçG'’ævWB‚'F‚"’÷"""’’æW‡æGW6W"‚’ç&W6öÇfR‚¢–b€¢'F–f7Bæ—5÷&VÆF—fU÷Fò‡7Fv–ær¢æB'F–f7Bç7Vff—‚æ66VföÆB‚’–âô4õdU%ô%EôU…DTå4”ôå0¢“ ¢G'“ ¢'F–f7BçVæÆ–æ²†Ö—76–æuöö³ÕG'VR¢W†6WBõ4W'&÷# ¢70¢ö6ÆVçWöV×G•÷7Fv–æuöF—'2‡7Fv–ær¢&WGW&âfW&–f–V@  ¦FVbF÷væÆöE÷G&6µ÷Fõ÷7Fv–ær€¢6Æ–VçC¢ç’À¢G&6µö–C¢7G"À¢7Fv–æuöF—#¢7G"ÂF‚À¢VÆ—G“¢–çBÀ¢VÖ&VEö'C¢&ööÂÀ¢’ÓâÆ—7EµF…Ó ¢G'“ ¢&VÆF—fU÷F‡2Ò6Æ–VçBæF÷væÆöE÷G&6²‡7G"‡G&6µö–B’Â–çB‡VÆ—G’’ÂVÖ&VEö'B¢W†6WBö'W¥6W'f–6TW'&÷# ¢&—6P¢W†6WBW†6WF–öâ2W†3 ¢&—6Rö'W¥&÷f–FW$W'&÷"‚%ö'W¢G&6²F÷væÆöBf–ÆVB"’g&öÒW†0¢&WGW&â÷6–FV6%÷F‡2‡&VÆF—fU÷F‡2Â7Fv–æuöF—"  ¦FVbF÷væÆöE÷W&Å÷Fõ÷7Fv–ær€¢6Æ–VçC¢ç’À¢W&Ã¢7G"À¢7Fv–æuöF—#¢7G"ÂF‚À¢VÆ—G“¢–çBÀ¢VÖ&VEö'C¢&ööÂÀ¢’ÓâÆ—7EµF…Ó ¢G'“ ¢&VÆF—fU÷F‡2Ò6Æ–VçBæF÷væÆöE÷W&Â‡7G"‡W&Â’Â–çB‡VÆ—G’’ÂVÖ&VEö'B¢W†6WBö'W¥6W'f–6TW'&÷# ¢&—6P¢W†6WBW†6WF–öâ2W†3 ¢&—6Rö'W¥&÷f–FW$W'&÷"‚%ö'W¢U$ÂF÷væÆöBf–ÆVB"’g&öÒW†0¢&WGW&â÷6–FV6%÷F‡2‡&VÆF—fU÷F‡2Â7Fv–æuöF—"  ¦FVbö6ÆVçWöV×G•÷7Fv–æuöF—'2‡7Fv–æs¢F‚’ÓâæöæS ¢–bæ÷B7Fv–æræW†—7G2‚“ ¢&WGW&à¢F—&V7F÷&–W2Ò6÷'FVB€¢‡F‚f÷"F‚–â7Fv–ærç&vÆö"‚"¢"’–bF‚æ—5öF—"‚’’À¢¶W“ÖÆÖ&FFƒ¢ÆVâ‡F‚ç'G2’À¢&WfW'6SÕG'VRÀ¢¢f÷"F—&V7F÷'’–âF—&V7F÷&–W3 ¢G'“ ¢F—&V7F÷'’ç&ÖF—"‚¢W†6WBõ4W'&÷# ¢70  ¦FVb–×÷'Eöf–ÆW5÷FõöÆ–'&'’€¢f–ÆW3¢—FW&&ÆUµF…ÒÀ¢7Fv–æuöF—#¢7G"ÂF‚À¢Æ–'&'•÷Fƒ¢7G"ÂF‚À¢’ÓâF–7C ¢7Fv–æu÷&ö÷BÒF‚‡7Fv–æuöF—"’æW‡æGW6W"‚’ç&W6öÇfR‚¢Æ–'&'•÷&ö÷BÒF‚†Æ–'&'•÷F‚’æW‡æGW6W"‚’ç&W6öÇfR‚¢Æ–'&'•÷&ö÷BæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢&W÷'C¢F–7E·7G"ÂÆ—7EÒÒ²&–×÷'FVB#¢µÒÂ&6öæfÆ–7G2#¢µÒÂ'&V¦V7FVB#¢µ×Ð¢f÷"f–ÆR–âf–ÆW3 ¢6÷W&6RÒF‚†f–ÆR’æW‡æGW6W"‚’ç&W6öÇfR‚¢G'“ ¢&VÆF—fRÒ6÷W&6Rç&VÆF—fU÷Fò‡7Fv–æu÷&ö÷B¢W†6WBfÇVTW'&÷# ¢&W÷'E²'&V¦V7FVB%ÒæVæB€¢²'F‚#¢7G"†f–ÆR’Â'&V6öâ#¢&f–ÆR—2÷WG6–FRF†R7Fv–ær&V'Ð¢¢6öçF–çVP¢F&vWBÒ†Æ–'&'•÷&ö÷Bò&VÆF—fR’ç&W6öÇfR‚¢–bæ÷BF&vWBæ—5÷&VÆF—fU÷Fò†Æ–'&'•÷&ö÷B“ ¢&W÷'E²'&V¦V7FVB%ÒæVæB€¢²'F‚#¢7G"†f–ÆR’Â'&V6öâ#¢'F&vWBW66W2F†RÆ–'&'’&ö÷B'Ð¢¢6öçF–çVP¢–bF&vWBæW†—7G2‚“ ¢&W÷'E²&6öæfÆ–7G2%ÒæVæB€¢²'F‚#¢7G"†f–ÆR’Â'F&vWB#¢7G"‡F&vWB’Â'&V6öâ#¢&Ç&VG’W†—7G2'Ð¢¢6öçF–çVP¢F&vWBç&VçBæÖ¶F—"‡&VçG3ÕG'VRÂW†—7Eöö³ÕG'VR¢6‡WF–ÂæÖ÷fR‡7G"‡6÷W&6R’Â7G"‡F&vWB’¢&W÷'E²&–×÷'FVB%ÒæVæB‡7G"‡F&vWB’¢ö6ÆVçWöV×G•÷7Fv–æuöF—'2‡7Fv–æu÷&ö÷B¢&WGW&â&W÷'@  ¦FVb&÷f–FW%öÆöö·Wö¶W’†—FVÓ¢Æ–Æ—7D—FVÒ’Óâ7G# ¢""%7F&ÆR–FVçF—G’6†&VB'’WVÂÆ–Æ—7B&÷w2æBgWGW&R&÷f–FW'2â""  ¢—7&2Òæ÷&ÖÆ—¦Uö—7&2†—FVÒæ—7&2¢–b—7&3 ¢–FVçF—G’Ò‚&—7&2"Â—7&2¢VÇ6S ¢–FVçF—G’Ò€¢&ÖWFFF"À¢—FVÒæ'F—7Eöæ÷&Ò÷"æ÷&ÖÆ—¦Uö'F—7B†—FVÒæ'F—7E÷&r’À¢—FVÒçF—FÆUöæ÷&Ò÷"æ÷&ÖÆ—¦U÷F—FÆR†—FVÒçF—FÆU÷&r’À¢—FVÒæÆ'VÕöæ÷&Ò÷"æ÷&ÖÆ—¦UöÆ'VÒ†—FVÒæÆ'VÕ÷&r’À¢7G"†—FVÒæGW&F–öåö×2÷"""’À¢¢&WGW&â†6†Æ–"ç6†#Sb‚%Çƒb"æ¦ö–â†–FVçF—G’’æVæ6öFR‚'WFbÓ‚"’’æ†W†F–vW7B‚  ¦FVbÖ—76–æu÷&÷f–FW%ö—FV×2€¢F#¢6W76–öâÂÆ–Æ—7C¢Æ–Æ—7BÂ&÷f–FW#¢7G ¢’ÓâGWÆU¶Æ—7EµÆ–Æ—7D—FVÕÒÂÆ—7EµÆ–Æ—7D—FVÕÕÓ ¢7FFVÖVçBÒ€¢6VÆV7B…Æ–Æ—7D—FVÒ¢æ¦ö–â„ÖF6‚ÂÖF6‚çÆ–Æ—7Eö—FVÕö–BÓÒÆ–Æ—7D—FVÒæ–B¢çv†W&R€¢Æ–Æ—7D—FVÒçÆ–Æ—7Eö–BÓÒÆ–Æ—7Bæ–BÀ¢ÖF6‚ç7FGW2ÓÒÖF6…7FGW2æÖ—76–ærÀ¢¢æ÷&FW%ö'’…Æ–Æ—7D—FVÒç÷6—F–öâÂÆ–Æ—7D—FVÒæ–B¢¢ÆÅö—FV×2ÒÆ—7B†F"ç66Æ'2‡7FFVÖVçB’¢Æöö·Wö¶W—2Ò·&÷f–FW%öÆöö·Wö¶W’†—FVÒ’f÷"—FVÒ–âÆÅö—FV×7Ð¢GFV×FVEö¶W—2Ò6WB‚¢–bÆöö·Wö¶W—3 ¢GFV×G2ÒF"æW†V7WFR€¢6VÆV7B€¢&÷f–FW$GFV×Bç&÷f–FW"À¢&÷f–FW$GFV×BæÆöö·Wö¶W’À¢&÷f–FW$GFV×Bç7FGW2À¢’çv†W&R…&÷f–FW$GFV×BæÆöö·Wö¶W’æ–åò†Æöö·Wö¶W—2’¢¢GFV×FVEö¶W—2Ò°¢Æöö·Wö¶W¢f÷"&÷f–FW%öæÖRÂÆöö·Wö¶W’Â7FGW2–âGFV×G0¢–b&÷f–FW%öæÖRÓÒ&÷f–FW"÷"7FGW2–â²'7F÷&VB"Â&6öæfÆ–7B'Ð¢Ð¢6VVåö¶W—2Ò6WB†GFV×FVEö¶W—2¢VÆ–v–&ÆS¢Æ—7EµÆ–Æ—7D—FVÕÒÒµÐ¢f÷"—FVÒ–âÆÅö—FV×3 ¢Æöö·Wö¶W’Ò&÷f–FW%öÆöö·Wö¶W’†—FVÒ¢–bÆöö·Wö¶W’–â6VVåö¶W—3 ¢6öçF–çVP¢6VVåö¶W—2æFB†Æöö·Wö¶W’¢VÆ–v–&ÆRæVæB†—FVÒ¢&WGW&âÆÅö—FV×2ÂVÆ–v–&ÆP  ¦FVbö'W¥öF÷væÆöEöVÆ–v–&–Æ—G’†F#¢6W76–öâÂÆ–Æ—7C¢Æ–Æ—7B’ÓâF–7E·7G"Â–çEÓ ¢ÆÅöÖ—76–ærÂVÆ–v–&ÆRÒÖ—76–æu÷&÷f–FW%ö—FV×2†F"ÂÆ–Æ—7BÂ'ö'W¢"¢&WGW&â°¢'F÷FÅöÖ—76–ær#¢ÆVâ†ÆÅöÖ—76–ær’À¢&VÆ–v–&ÆR#¢ÆVâ†VÆ–v–&ÆR’À¢&Ç&VG•ö6†V6¶VB#¢ÆVâ†ÆÅöÖ—76–ær’ÒÆVâ†VÆ–v–&ÆR’À¢Ð  ¦FVbÖ&µöF÷væÆöG5÷7F÷&VB€¢F÷væÆöG3¢F–7BÀ¢–×÷'E÷&W÷'C¢F–7BÀ¢7Fv–æuöF—#¢7G"ÂF‚À¢Æ–'&'•÷Fƒ¢7G"ÂF‚À¢’ÓâæöæS ¢""%&öÖ÷FRW"×G&6²F÷væÆöB7FFW2gFW"f–ÆW2Ö÷fR–çFòF†RÆ–'&'’â""  ¢7Fv–æu÷&ö÷BÒF‚‡7Fv–æuöF—"’æW‡æGW6W"‚’ç&W6öÇfR‚¢Æ–'&'•÷&ö÷BÒF‚†Æ–'&'•÷F‚’æW‡æGW6W"‚’ç&W6öÇfR‚¢–×÷'FVBÒ°¢7G"…F‚‡F‚’æW‡æGW6W"‚’ç&W6öÇfR‚’’f÷"F‚–â–×÷'E÷&W÷'E²&–×÷'FVB%Ð¢Ð¢6öæfÆ–7G2Ò°¢7G"…F‚†VçG'•²'F&vWB%Ò’æW‡æGW6W"‚’ç&W6öÇfR‚’¢f÷"VçG'’–â–×÷'E÷&W÷'E²&6öæfÆ–7G2%Ð¢Ð¢7F÷&VBÒ ¢6öæfÆ–7FVBÒ ¢–×÷'Eöf–ÆVBÒ ¢f÷"VçG'’–âF÷væÆöG2ævWB‚&—FV×2"ÂµÒ“ ¢f–ÆW2ÒµF‚‡F‚’æW‡æGW6W"‚’ç&W6öÇfR‚’f÷"F‚–âVçG'’ç÷‚&f–ÆW2"ÂµÒ•Ð¢–bf–ÆW3 ¢VçG'•²&f–ÆUö6÷VçB%ÒÒÆVâ†f–ÆW2¢–bVçG'’ævWB‚'7FGW2"’Ò&F÷væÆöFVB# ¢6öçF–çVP¢F&vWG3¢Æ—7E·7G%ÒÒµÐ¢G'“ ¢F&vWG2Ò°¢7G"‚†Æ–'&'•÷&ö÷Bò6÷W&6Rç&VÆF—fU÷Fò‡7Fv–æu÷&ö÷B’’ç&W6öÇfR‚’¢f÷"6÷W&6R–âf–ÆW0¢Ð¢W†6WBfÇVTW'&÷# ¢F&vWG2ÒµÐ¢–bF&vWG2æBÆÂ‡F&vWB–â–×÷'FVBf÷"F&vWB–âF&vWG2“ ¢VçG'•²'7FGW2%ÒÒ'7F÷&VB ¢7F÷&VB³Ò¢VÆ–bF&vWG2æBç’‡F&vWB–â6öæfÆ–7G2f÷"F&vWB–âF&vWG2“ ¢VçG'•²'7FGW2%ÒÒ&6öæfÆ–7B ¢6öæfÆ–7FVB³Ò¢VÇ6S ¢VçG'•²'7FGW2%ÒÒ&f–ÆVB ¢VçG'•²&W'&÷"%ÒÒ&Æ–'&'’–×÷'Bf–ÆVB ¢–×÷'Eöf–ÆVB³Ò¢VçG&–W2ÒF÷væÆöG2ævWB‚&—FV×2"ÂµÒ¢F÷væÆöG5²'7F÷&VB%ÒÒ7VÒ†VçG'’ævWB‚'7FGW2"’ÓÒ'7F÷&VB"f÷"VçG'’–âVçG&–W2¢F÷væÆöG5²&6öæfÆ–7G2%ÒÒ7VÒ€¢VçG'’ævWB‚'7FGW2"’ÓÒ&6öæfÆ–7B"f÷"VçG'’–âVçG&–W0¢¢F÷væÆöG5²&–×÷'Eöf–ÆVB%ÒÒ7VÒ€¢VçG'’ævWB‚&W'&÷""’ÓÒ&Æ–'&'’–×÷'Bf–ÆVB"f÷"VçG'’–âVçG&–W0¢  ¦FVb÷&V6÷&E÷ö'W¥öGFV×B€¢F#¢6W76–öâÀ¢—FVÓ¢Æ–Æ—7D—FVÒÀ¢VçG'“¢F–7BÀ¢¦ö%ö–C¢–çBÂæöæRÀ¢’ÓâæöæS ¢Æöö·Wö¶W’Ò&÷f–FW%öÆöö·Wö¶W’†—FVÒ¢GFV×BÒF"ç66Æ"€¢6VÆV7B…&÷f–FW$GFV×B’çv†W&R€¢&÷f–FW$GFV×Bç&÷f–FW"ÓÒ'ö'W¢"À¢&÷f–FW$GFV×BæÆöö·Wö¶W’ÓÒÆöö·Wö¶W’À¢¢¢–bGFV×B—2æöæS ¢GFV×BÒ&÷f–FW$GFV×B‡&÷f–FW#Ò'ö'W¢"ÂÆöö·Wö¶W“ÖÆöö·Wö¶W’¢F"æFB†GFV×B¢GFV×BçÆ–Æ—7Eö—FVÕö–BÒ—FVÒæ–@¢GFV×Bæ¦ö%ö–BÒ¦ö%ö–@¢GFV×Bç7FGW2Ò7G"†VçG'’ævWB‚'7FGW2"’÷"&f–ÆVB"¢GFV×Bç&÷f–FW%ö—FVÕö–BÒVçG'’ævWB‚'ö'W¥÷G&6µö–B"¢GFV×Bç6VÆV7F–öåöÖWF†öBÒVçG'’ævWB‚'6VÆV7F–öâ"¢GFV×BæW'&÷%ö6öFRÒVçG'’ævWB‚&W'&÷""¢F"æfÇW6‚‚  ¦FVb&V6÷&E÷ö'W¥öF÷væÆöEöGFV×G2€¢F#¢6W76–öâÀ¢F÷væÆöG3¢F–7BÀ¢Æ–Æ—7Eö—FV×3¢F–7E¶–çBÂÆ–Æ—7D—FVÕÒÀ¢¦ö%ö–C¢–çBÂæöæRÀ¢’ÓâæöæS ¢""%W'6—7Bf–æÂ7F÷&VBö6öæfÆ–7B÷WF6öÖW2gFW"F†R–×÷'B†6Râ""  ¢f÷"VçG'’–âF÷væÆöG2ævWB‚&—FV×2"ÂµÒ“ ¢–bVçG'’ævWB‚'7FGW2"’–â²'VWVVB"Â'6V&6†–ær"Â&F÷væÆöF–ær'Ó ¢6öçF–çVP¢—FVÒÒÆ–Æ—7Eö—FV×2ævWB†–çB†VçG'•²&—FVÕö–B%Ò’¢–b—FVÒ—2æ÷BæöæS ¢÷&V6÷&E÷ö'W¥öGFV×B†F"Â—FVÒÂVçG'’Â¦ö%ö–B  ¦FVbfWF6…öÖ—76–æu÷G&6·2€¢F#¢6W76–öâÀ¢Æ–Æ—7C¢Æ–Æ—7BÀ¢6Æ–VçC¢ç’À¢&öw&W75ö6ÆÆ&6³¢6ÆÆ&ÆUµ¶F–7EÒÂæöæUÒÂæöæRÒæöæRÀ¢¦ö%ö–C¢–çBÂæöæRÒæöæRÀ¢&F6…ö6ö×ÆWFUö6ÆÆ&6³¢6ÆÆ&ÆUµ¶F–7BÂÆ—7EµF…ÕÒÂæöæUÒÂæöæRÒæöæRÀ¢’ÓâGWÆU¶F–7BÂÆ—7EµF…ÕÓ ¢ÆÅöÖ—76–ærÂ—FV×2ÒÖ—76–æu÷&÷f–FW%ö—FV×2†F"ÂÆ–Æ—7BÂ'ö'W¢"¢&F6…÷6—¦RÒ6WGF–æw2çö'W¥öÖ…÷G&6·5÷W%÷'Và¢&F6…ö6÷VçBÒ†ÆVâ†—FV×2’²&F6…÷6—¦RÒ’òò&F6…÷6—¦P¢VçG&–W3¢Æ—7E¶F–7E·7G"Âç•ÕÒÒ°¢°¢&—FVÕö–B#¢—FVÒæ–BÀ¢&'F—7B#¢—FVÒæ'F—7E÷&rÀ¢'F—FÆR#¢—FVÒçF—FÆU÷&rÀ¢'7FGW2#¢'VWVVB"À¢Ð¢f÷"—FVÒ–â—FV×0¢Ð¢7VÖÖ'“¢F–7E·7G"Âç•ÒÒ°¢'Æ–Æ—7Eö–B#¢Æ–Æ—7Bæ–BÀ¢'F÷FÅöÖ—76–ær#¢ÆVâ†ÆÅöÖ—76–ær’À¢&VÆ–v–&ÆU÷F÷FÂ#¢ÆVâ†—FV×2’À¢'6¶—VE÷6ÖU÷6÷W&6R#¢ÆVâ†ÆÅöÖ—76–ær’ÒÆVâ†—FV×2’À¢&&F6…÷6—¦R#¢&F6…÷6—¦RÀ¢&&F6…ö6÷VçB#¢&F6…ö6÷VçBÀ¢&7W'&VçEö&F6‚#¢À¢&7W'&VçEö&F6…÷6—¦R#¢À¢&&F6…÷&ö6W76VB#¢À¢&&F6…÷W6U÷6V6öæG2#¢À¢'&ö6W76VB#¢À¢&GFV×FVB#¢À¢&F÷væÆöFVB#¢À¢&æ÷Eöf÷VæB#¢À¢&Ö&–wV÷W2#¢À¢&f–ÆVB#¢À¢&—FV×2#¢VçG&–W2À¢Ð¢6öÆÆV7FVC¢Æ—7EµF…ÒÒµÐ¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢f÷"&F6…ö–æFW‚Â&F6…÷7F'B–âVçVÖW&FR‡&ævRƒÂÆVâ†—FV×2’Â&F6…÷6—¦R’“ ¢6öÆÆV7FVEö&Vf÷&Uö&F6‚ÒÆVâ†6öÆÆV7FVB¢&F6…ö—FV×2Ò—FV×5¶&F6…÷7F'B¢&F6…÷7F'B²&F6…÷6—¦UÐ¢&F6…öVçG&–W2ÒVçG&–W5¶&F6…÷7F'B¢&F6…÷7F'B²&F6…÷6—¦UÐ¢7VÖÖ'•²&7W'&VçEö&F6‚%ÒÒ&F6…ö–æFW‚²¢7VÖÖ'•²&7W'&VçEö&F6…÷6—¦R%ÒÒÆVâ†&F6…ö—FV×2¢7VÖÖ'•²&&F6…÷&ö6W76VB%ÒÒ ¢7VÖÖ'•²&&F6…÷W6U÷6V6öæG2%ÒÒ ¢7VÖÖ'•²&&F6…÷7FFR%ÒÒ''Vææ–ær ¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢f÷"—FVÒÂVçG'’–â¦—†&F6…ö—FV×2Â&F6…öVçG&–W2Â7G&–7CÕG'VR“ ¢VçG'•²'7FGW2%ÒÒ'6V&6†–ær ¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢VW'’Òb'¶—FVÒæ'F—7E÷&r÷"rwÒ¶—FVÒçF—FÆU÷&r÷"rwÒ"ç7G&—‚¢G'“ ¢–bæ÷BVW'“ ¢&—6Rö'W¥&÷f–FW$W'&÷"‚%Æ–Æ—7B—FVÒ†2æò'F—7B÷F—FÆRVW'’"¢7VÖÖ'•²&GFV×FVB%Ò³Ò¢6æF–FFW2Ò6V&6…÷G&6·2†6Æ–VçBÂVW'’ÂÆ–Ö—CÓ¢&W7BÂÖWF†öBÒ6†ö÷6U÷G&6µö6æF–FFR€¢'F—7E÷&sÖ—FVÒæ'F—7E÷&rÀ¢F—FÆU÷&sÖ—FVÒçF—FÆU÷&rÀ¢Æ'VÕ÷&sÖ—FVÒæÆ'VÕ÷&rÀ¢—7&3Ö—FVÒæ—7&2À¢GW&F–öåö×3Ö—FVÒæGW&F–öåö×2À¢6æF–FFW3Ö6æF–FFW2À¢¢–b&W7B—2æöæS ¢7FGW2Ò&Ö&–wV÷W2"–bÖWF†öBÓÒ&Ö&–wV÷W2"VÇ6R&æ÷Eöf÷VæB ¢7VÖÖ'•·7FGW5Ò³Ò¢VçG'•²'7FGW2%ÒÒ7FGW0¢VçG'•²'6VÆV7F–öâ%ÒÒÖWF†ö@¢VÇ6S ¢VçG'•²'ö'W¥÷G&6µö–B%ÒÒ&W7Bçö'W¥ö–@¢VçG'•²'6VÆV7F–öâ%ÒÒÖWF†ö@¢VçG'•²'7FGW2%ÒÒ&F÷væÆöF–ær ¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢f–ÆW2ÒF÷væÆöE÷G&6µ÷Fõ÷7Fv–ær€¢6Æ–VçBÀ¢&W7Bçö'W¥ö–BÀ¢6WGF–æw2çö'W¥÷7Fv–æu÷F‚À¢6WGF–æw2çö'W¥÷VÆ—G’À¢6WGF–æw2çö'W¥öVÖ&VEö'BÀ¢¢–bf–ÆW3 ¢6öÆÆV7FVBæW‡FVæB†f–ÆW2¢7VÖÖ'•²&F÷væÆöFVB%Ò³Ò¢VçG'•²'7FGW2%ÒÒ&F÷væÆöFVB ¢VçG'•²&f–ÆW2%ÒÒ·7G"‡F‚’f÷"F‚–âf–ÆW5Ð¢VÇ6S ¢7VÖÖ'•²&f–ÆVB%Ò³Ò¢VçG'•²'7FGW2%ÒÒ&f–ÆVB ¢VçG'•²&W'&÷"%ÒÒ&F÷væÆöB&öGV6VBæòfW&–f–VBVF–ò ¢W†6WBö'W¥6W'f–6TW'&÷"2W†3 ¢7VÖÖ'•²&f–ÆVB%Ò³Ò¢VçG'•²'7FGW2%ÒÒ&f–ÆVB ¢VçG'•²&W'&÷"%ÒÒG—R†W†2’åõöæÖUõð¢–bVçG'•²'7FGW2%ÒÒ&F÷væÆöFVB# ¢÷&V6÷&E÷ö'W¥öGFV×B†F"Â—FVÒÂVçG'’Â¦ö%ö–B¢7VÖÖ'•²'&ö6W76VB%Ò³Ò¢7VÖÖ'•²&&F6…÷&ö6W76VB%Ò³Ò¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢–b6WGF–æw2çö'W¥÷&WVW7EöFVÆ•÷6V6öæG3 ¢F–ÖRç6ÆVW‡6WGF–æw2çö'W¥÷&WVW7EöFVÆ•÷6V6öæG2¢–b&F6…ö6ö×ÆWFUö6ÆÆ&6²—2æ÷BæöæS ¢7VÖÖ'•²&&F6…÷7FFR%ÒÒ&G&–æ–ær ¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢&F6…ö6ö×ÆWFUö6ÆÆ&6²€¢7VÖÖ'’À¢6öÆÆV7FVE¶6öÆÆV7FVEö&Vf÷&Uö&F6ƒ¥ÒÀ¢¢–b&F6…ö–æFW‚²Â&F6…ö6÷VçC ¢7VÖÖ'•²&&F6…÷7FFR%ÒÒ'W6VB ¢7VÖÖ'•²&&F6…÷W6U÷6V6öæG2%ÒÒ6WGF–æw2çö'W¥ö&F6…öFVÆ•÷6V6öæG0¢–b&öw&W75ö6ÆÆ&6²—2æ÷BæöæS ¢&öw&W75ö6ÆÆ&6²‡7VÖÖ'’¢–b6WGF–æw2çö'W¥ö&F6…öFVÆ•÷6V6öæG3 ¢F–ÖRç6ÆVW‡6WGF–æw2çö'W¥ö&F6…öFVÆ•÷6V6öæG2¢&WGW&â7VÖÖ'’Â6öÆÆV7FV@