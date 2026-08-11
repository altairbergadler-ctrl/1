"""Deterministic playlist-to-library matching cascade for the MVP."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Sequence

from rapidfuzz.fuzz import token_set_ratio
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.models import (
    Album,
    File as LibraryFile,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    Track,
)
from app.services.normalize import normalize_artist, normalize_title

EXACT_DURATION_TOLERANCE_MS = 2_000
FUZZY_DURATION_TOLERANCE_MS = 5_000
FUZZY_THRESHOLD = 85.0
FUZZY_AUTO_READY_CONFIDENCE = 0.9
FUZZY_AMBIGUITY_GAP = 0.05

_ISRC_RE = re.compile(r"^[A-Z0-9]{12}$")
_VERSION_MARKERS = {
    "live": re.compile(r"(?<!\w)live(?!\w)", re.IGNORECASE),
    "remix": re.compile(r"(?<!\w)remix(?:ed)?(?!\w)", re.IGNORECASE),
    "cover": re.compile(r"(?<!\w)cover(?!\w)", re.IGNORECASE),
}


class MatchMethod(str, Enum):
    isrc = "isrc"
    exact = "exact"
    fuzzy = "fuzzy"
    manual = "manual"
    manual_missing = "manual_missing"
    none = "none"


@dataclass(frozen=True, slots=True)
class CatalogTrack:
    track_id: int
    artist: str
    artist_norm: str
    title: str
    title_norm: str
    album: str
    album_norm: str
    duration_ms: int | None
    isrc: str | None
    bit_depth: int | None
    sample_rate: int | None
    format: str | None
    quality_rank: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class MatchCandidate:
    track_id: int
    artist: str
    title: str
    album: str
    duration_ms: int | None
    isrc: str | None
    confidence: float
    bit_depth: int | None
    sample_rate: int | None
    format: str | None


@dataclass(frozen=True, slots=True)
class MatchDecision:
    track_id: int | None
    confidence: float
    method: MatchMethod
    status: MatchStatus


@dataclass(slots=True)
class MatchingSummary:
    total: int = 0
    ready: int = 0
    needs_review: int = 0
    missing: int = 0

    def record(self, status: MatchStatus) -> None:
        self.total += 1
        if status == MatchStatus.ready:
            self.ready += 1
        elif status == MatchStatus.needs_review:
            self.needs_review += 1
        else:
            self.missing += 1

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


def normalize_isrc(value: str | None) -> str | None:
    if not value:
        return None
    candidate = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    return candidate if _ISRC_RE.fullmatch(candidate) else None


def _file_quality(file: LibraryFile) -> tuple[int, int, int, int]:
    """Rank existing copies without inventing a release-2 quality policy."""

    return (
        int(file.bit_depth or 0),
        int(file.sample_rate or 0),
        int(file.size_bytes or 0),
        -int(file.id or 0),
    )


def load_catalog(db: Session) -> list[CatalogTrack]:
    """Load playable tracks once so a matching run does not query per item."""

    tracks = db.scalars(
        select(Track)
        .options(
            joinedload(Track.album).joinedload(Album.artist),
            selectinload(Track.files),
        )
        .where(Track.files.any())
        .order_by(Track.id)
    ).unique()
    catalog: list[CatalogTrack] = []
    for track in tracks:
        best_file = max(track.files, key=_file_quality)
        album = track.album
        artist = album.artist
        catalog.append(
            CatalogTrack(
                track_id=track.id,
                artist=artist.name,
                artist_norm=artist.name_norm or normalize_artist(artist.name),
                title=track.title,
                title_norm=track.title_norm or normalize_title(track.title),
                album=album.title,
                album_norm=album.title_norm,
                duration_ms=track.duration_ms,
                isrc=normalize_isrc(track.isrc),
                bit_depth=best_file.bit_depth,
                sample_rate=best_file.sample_rate,
                format=best_file.format,
                quality_rank=_file_quality(best_file),
            )
        )
    return catalog


def _version_markers(value: str | None) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", value or "")
    return frozenset(
        marker for marker, pattern in _VERSION_MARKERS.items() if pattern.search(normalized)
    )


def _item_version_markers(item: PlaylistItem) -> frozenset[str]:
    return _version_markers(item.title_raw) | _version_markers(item.album_raw)


def _catalog_version_markers(track: CatalogTrack) -> frozenset[str]:
    return _version_markers(track.title) | _version_markers(track.album)


def _duration_delta(
    playlist_duration_ms: int | None,
    catalog_duration_ms: int | None,
) -> int | None:
    if playlist_duration_ms is None or catalog_duration_ms is None:
        return None
    return abs(playlist_duration_ms - catalog_duration_ms)


def _within_duration(
    playlist_duration_ms: int | None,
    catalog_duration_ms: int | None,
    tolerance_ms: int,
) -> bool:
    delta = _duration_delta(playlist_duration_ms, catalog_duration_ms)
    return delta is None or delta <= tolerance_ms


def _candidate_order(candidate: tuple[CatalogTrack, float]):
    track, confidence = candidate
    return (
        -confidence,
        tuple(-value for value in track.quality_rank),
        track.track_id,
    )


def _best_quality(candidates: Sequence[CatalogTrack]) -> CatalogTrack:
    return sorted(
        candidates,
        key=lambda track: (
            tuple(-value for value in track.quality_rank),
            track.track_id,
        ),
    )[0]


def _exact_confidence(item: PlaylistItem, candidate: CatalogTrack) -> float:
    delta = _duration_delta(item.duration_ms, candidate.duration_ms)
    if delta is None:
        return 0.92
    # Preserve a conservative 0.94+ exact confidence at the allowed boundary.
    return round(0.98 - (0.04 * delta / EXACT_DURATION_TOLERANCE_MS), 4)


def _fuzzy_candidates(
    item: PlaylistItem,
    catalog: Sequence[CatalogTrack],
) -> list[tuple[CatalogTrack, float]]:
    item_artist = item.artist_norm or normalize_artist(item.artist_raw)
    item_title = item.title_norm or normalize_title(item.title_raw)
    if not item_artist or not item_title:
        return []

    item_key = f"{item_artist} {item_title}"
    item_markers = _item_version_markers(item)
    candidates: list[tuple[CatalogTrack, float]] = []
    for candidate in catalog:
        if not _within_duration(
            item.duration_ms,
            candidate.duration_ms,
            FUZZY_DURATION_TOLERANCE_MS,
        ):
            continue
        artist_score = float(token_set_ratio(item_artist, candidate.artist_norm))
        title_score = float(token_set_ratio(item_title, candidate.title_norm))
        combined_score = float(
            token_set_ratio(
                item_key,
                f"{candidate.artist_norm} {candidate.title_norm}",
            )
        )
        # A combined token score alone can hide a completely wrong artist or
        # title, so both components retain a modest independent floor.
        score = min(combined_score, (artist_score * 0.4) + (title_score * 0.6))
        if item_markers != _catalog_version_markers(candidate):
            score -= 15.0
        if score < FUZZY_THRESHOLD:
            continue
        confidence = 0.7 + ((score - FUZZY_THRESHOLD) / (100 - FUZZY_THRESHOLD)) * 0.2
        candidates.append((candidate, round(min(0.9, confidence), 4)))
    return sorted(candidates, key=_candidate_order)


def match_playlist_item(
    item: PlaylistItem,
    catalog: Sequence[CatalogTrack],
) -> MatchDecision:
    """Apply the fixed MVP cascade to one imported playlist item."""

    item_isrc = normalize_isrc(item.isrc)
    if item_isrc:
        isrc_matches = [track for track in catalog if track.isrc == item_isrc]
        if isrc_matches:
            selected = _best_quality(isrc_matches)
            return MatchDecision(
                track_id=selected.track_id,
                confidence=1.0,
                method=MatchMethod.isrc,
                status=MatchStatus.ready,
            )

    item_artist = item.artist_norm or normalize_artist(item.artist_raw)
    item_title = item.title_norm or normalize_title(item.title_raw)
    item_album = item.album_norm or ""
    if item_artist and item_title and item_album:
        exact_matches = [
            track
            for track in catalog
            if track.artist_norm == item_artist
            and track.title_norm == item_title
            and track.album_norm == item_album
            and _item_version_markers(item) == _catalog_version_markers(track)
            and _within_duration(
                item.duration_ms,
                track.duration_ms,
                EXACT_DURATION_TOLERANCE_MS,
            )
        ]
        if exact_matches:
            selected = sorted(
                exact_matches,
                key=lambda track: (
                    -_exact_confidence(item, track),
                    tuple(-value for value in track.quality_rank),
                    track.track_id,
                ),
            )[0]
            return MatchDecision(
                track_id=selected.track_id,
                confidence=_exact_confidence(item, selected),
                method=MatchMethod.exact,
                status=MatchStatus.ready,
            )

    fuzzy = _fuzzy_candidates(item, catalog)
    if fuzzy:
        selected, confidence = fuzzy[0]
        ambiguous = (
            len(fuzzy) > 1
            and confidence - fuzzy[1][1] < FUZZY_AMBIGUITY_GAP
        )
        ready = confidence >= FUZZY_AUTO_READY_CONFIDENCE and not ambiguous
        return MatchDecision(
            track_id=selected.track_id,
            confidence=min(confidence, 0.89) if ambiguous else confidence,
            method=MatchMethod.fuzzy,
            status=MatchStatus.ready if ready else MatchStatus.needs_review,
        )

    return MatchDecision(
        track_id=None,
        confidence=0.0,
        method=MatchMethod.none,
        status=MatchStatus.missing,
    )


def get_review_candidates(
    db: Session,
    item: PlaylistItem,
    *,
    limit: int = 5,
    catalog: Sequence[CatalogTrack] | None = None,
) -> list[MatchCandidate]:
    candidates = _fuzzy_candidates(item, catalog or load_catalog(db))[:limit]
    return [
        MatchCandidate(
            track_id=track.track_id,
            artist=track.artist,
            title=track.title,
            album=track.album,
            duration_ms=track.duration_ms,
            isrc=track.isrc,
            confidence=confidence,
            bit_depth=track.bit_depth,
            sample_rate=track.sample_rate,
            format=track.format,
        )
        for track, confidence in candidates
    ]


def _existing_manual_decision(
    match: Match | None,
    playable_track_ids: frozenset[int],
) -> MatchDecision | None:
    if match is None or match.method not in {
        MatchMethod.manual.value,
        MatchMethod.manual_missing.value,
    }:
        return None
    if (
        match.method == MatchMethod.manual.value
        and match.track_id not in playable_track_ids
    ):
        return None
    return MatchDecision(
        track_id=match.track_id,
        confidence=float(match.confidence or 0.0),
        method=MatchMethod(match.method),
        status=match.status,
    )


def run_matching(
    db: Session,
    user_id: int,
    playlist_id: int | None = None,
    *,
    progress_callback: Callable[[MatchingSummary], None] | None = None,
) -> MatchingSummary:
    """Upsert one match per playlist item and commit atomically."""

    statement = (
        select(PlaylistItem)
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(Playlist.user_id == user_id)
        .order_by(
        PlaylistItem.playlist_id,
        PlaylistItem.position,
        PlaylistItem.id,
        )
    )
    if playlist_id is not None:
        statement = statement.where(PlaylistItem.playlist_id == playlist_id)
    items = list(db.scalars(statement))
    catalog = load_catalog(db)
    playable_track_ids = frozenset(track.track_id for track in catalog)
    summary = MatchingSummary()

    try:
        for item in items:
            decision = _existing_manual_decision(item.match, playable_track_ids)
            if decision is None:
                decision = match_playlist_item(item, catalog)
                match = item.match
                if match is None:
                    match = Match(playlist_item=item)
                    db.add(match)
                match.track_id = decision.track_id
                match.confidence = decision.confidence
                match.method = decision.method.value
                match.status = decision.status
            summary.record(decision.status)
            if progress_callback is not None:
                progress_callback(summary)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return summary
