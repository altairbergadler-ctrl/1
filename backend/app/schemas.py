import json
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class HealthOut(BaseModel):
    status: str = "ok"


class LoginIn(BaseModel):
    token: str


class LoginOut(BaseModel):
    authenticated: bool = True


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    type: str
    status: str
    payload: dict | None = None
    error: str | None = None
    created_at: datetime
    heartbeat_at: datetime
    finished_at: datetime | None = None

    @field_validator("payload", mode="before")
    @classmethod
    def parse_payload(cls, value):
        if value is None or isinstance(value, dict):
            return value
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return {"raw": str(value)}
        return parsed if isinstance(parsed, dict) else {"value": parsed}


class FormatStatsOut(BaseModel):
    files: int
    bytes: int


class LibraryStatsOut(BaseModel):
    files: int
    tracks: int
    albums: int
    bytes: int
    formats: dict[str, FormatStatsOut]


class AlbumArtistOut(BaseModel):
    id: int
    name: str
    mbid: str | None = None


class LibraryAlbumOut(BaseModel):
    id: int
    title: str
    year: int | None = None
    mbid: str | None = None
    artist: AlbumArtistOut
    tracks: int
    files: int
    bytes: int


class LibraryAlbumsOut(BaseModel):
    items: list[LibraryAlbumOut]
    total: int


class PlaylistOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    track_count: int


class SourceOut(BaseModel):
    id: int
    service: str
    connected: bool
    expires_at: datetime | None = None


class SourceListOut(BaseModel):
    items: list[SourceOut]


class PlaylistImportIn(BaseModel):
    source_id: int = Field(gt=0)


class PlaylistStatusSummaryOut(BaseModel):
    ready: int
    missing: int
    review: int
    unmatched: int
    collected_percent: float


class PlaylistListItemOut(BaseModel):
    id: int
    source_id: int
    service: str
    external_id: str
    name: str
    snapshot_hash: str | None = None
    track_count: int
    updated_at: datetime
    summary: PlaylistStatusSummaryOut


class PlaylistListOut(BaseModel):
    items: list[PlaylistListItemOut]
    total: int


class PlaylistDetailOut(PlaylistListItemOut):
    pass


class PlaylistItemStatus(str, Enum):
    unmatched = "UNMATCHED"
    ready = "READY"
    needs_review = "NEEDS_REVIEW"
    missing = "MISSING"


class PlaylistItemOut(BaseModel):
    id: int
    position: int
    artist_raw: str | None = None
    title_raw: str | None = None
    album_raw: str | None = None
    artist_norm: str | None = None
    title_norm: str | None = None
    album_norm: str | None = None
    isrc: str | None = None
    duration_ms: int | None = None
    external_track_id: str | None = None
    status: PlaylistItemStatus
    match_id: int | None = None
    track_id: int | None = None
    confidence: float | None = None
    method: str | None = None


class PlaylistItemsOut(BaseModel):
    items: list[PlaylistItemOut]
    total: int


class MatchingRunIn(BaseModel):
    playlist_id: int | None = Field(default=None, gt=0)


class ReviewCandidateOut(BaseModel):
    track_id: int
    artist: str
    title: str
    album: str
    duration_ms: int | None = None
    isrc: str | None = None
    confidence: float
    bit_depth: int | None = None
    sample_rate: int | None = None
    format: str | None = None


class ReviewItemOut(BaseModel):
    match_id: int
    playlist_id: int
    playlist_name: str
    playlist_item_id: int
    position: int
    artist_raw: str | None = None
    title_raw: str | None = None
    album_raw: str | None = None
    duration_ms: int | None = None
    confidence: float
    candidates: list[ReviewCandidateOut]


class ReviewListOut(BaseModel):
    items: list[ReviewItemOut]
    total: int


class MatchResolveIn(BaseModel):
    track_id: int | None = Field(default=None, gt=0)


class MatchResolveOut(BaseModel):
    match_id: int
    playlist_item_id: int
    track_id: int | None = None
    confidence: float
    method: str
    status: PlaylistItemStatus
