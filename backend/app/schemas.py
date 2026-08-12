import json
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator


class HealthOut(BaseModel):
    status: str = "ok"
    release_sha: str


class LoginIn(BaseModel):
    token: SecretStr = Field(min_length=16, max_length=4096)


class LoginOut(BaseModel):
    authenticated: bool = True
    csrf_token: str | None = None


class CurrentUserOut(BaseModel):
    id: int
    email: str
    display_name: str | None = None
    role: Literal["owner", "user"]
    csrf_token: str


class UserAdminOut(BaseModel):
    id: int
    email: str | None
    display_name: str | None = None
    role: Literal["owner", "user"]
    state: Literal["pending", "active", "disabled"]
    is_bootstrap_owner: bool
    created_at: datetime
    activated_at: datetime | None = None
    last_login_at: datetime | None = None
    active_sessions: int = 0


class UserAdminListOut(BaseModel):
    items: list[UserAdminOut]


class UserRoleUpdateIn(BaseModel):
    role: Literal["owner", "user"]


class RecoveryOwnerBindingIn(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    confirm: Literal["RESET BOOTSTRAP OWNER"]


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
        if value is None:
            return None
        if isinstance(value, dict):
            parsed = value
        else:
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                return None
        if not isinstance(parsed, dict):
            return None
        allowed = {
            "source_id",
            "playlist_id",
            "mode",
            "phase",
            "provider",
            "account_id",
            "discovered",
            "created",
            "updated",
            "unchanged",
            "failed",
            "tracks_imported",
            "skipped_items",
            "matched",
            "missing",
            "review",
            "processed",
            "imported",
            "skipped",
            "bytes",
            "files",
            "phase",
            "progress",
            "scan",
            "musicbrainz",
            "downloads",
            "import",
            "matching",
            "storage",
            "items",
            "status",
            "reason",
            "total",
            "added",
            "changed",
            "moved",
            "removed",
            "duplicate_content",
            "enriched",
            "not_found",
            "needs_review",
            "ready",
            "batch_total",
            "batch_count",
            "batch_size",
            "current_batch",
            "eligible_total",
            "total_missing",
            "already_checked",
            "downloaded",
            "stored",
            "item_id",
            "selection",
            "codec",
            "source_codec",
            "remuxed",
            "uploaded",
            "reused",
            "evicted",
            "deferred",
            "result_status",
            "service",
        }

        def sanitized(mapping: dict, depth: int = 0) -> dict:
            if depth > 4:
                return {}
            result = {}
            for key, item in mapping.items():
                if key not in allowed:
                    continue
                if isinstance(item, (int, float, bool, type(None))):
                    result[key] = item
                elif isinstance(item, str):
                    result[key] = item[:256]
                elif isinstance(item, dict):
                    result[key] = sanitized(item, depth + 1)
                elif key == "items" and isinstance(item, list):
                    result[key] = [
                        sanitized(entry, depth + 1)
                        for entry in item[:500]
                        if isinstance(entry, dict)
                    ]
            return result

        return sanitized(parsed)

    @field_validator("error", mode="before")
    @classmethod
    def redact_error(cls, value):
        return "Job failed" if value else None


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


class PlaylistUrlImportIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class PlaylistContentImportIn(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    content: str = Field(min_length=1, max_length=2_000_000)
    format: Literal["auto", "csv", "m3u", "text"] = "auto"


class PlaylistContentImportOut(BaseModel):
    playlist_id: int
    imported: int
    skipped: int
    format: str
    matching_job: JobOut


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


# --- Схемы интеграции Qobuz (RESTRICT, docs/qobuz-dl-assessment.md) ---------
#
# В Out-схемах никогда не бывает секретов: только нечувствительные флаги,
# лимиты, тариф (label) и публичные поля каталога Qobuz. In-схемы принимают
# минимум данных (url / playlist_id) с жёсткой валидацией длин и диапазонов.


class QobuzStatusOut(BaseModel):
    enabled: bool
    configured: bool
    quality: int
    max_tracks_per_run: int
    batch_delay_seconds: float


class QobuzDownloadEligibilityOut(BaseModel):
    total_missing: int
    eligible: int
    already_checked: int


class QobuzConnectOut(BaseModel):
    connected: bool
    label: str | None = None


class QobuzSearchItemOut(BaseModel):
    kind: str  # track | album
    qobuz_id: str
    artist: str
    title: str
    album: str | None = None
    duration_ms: int | None = None
    isrc: str | None = None
    hires: bool
    url: str


class QobuzSearchOut(BaseModel):
    items: list[QobuzSearchItemOut]


class QobuzDownloadUrlIn(BaseModel):
    url: str = Field(min_length=1, max_length=2048)


class QobuzFetchMissingIn(BaseModel):
    playlist_id: int = Field(gt=0)


class YandexDownloadStatusOut(BaseModel):
    enabled: bool
    configured: bool
    supported_codecs: list[str]
    lossless_supported: bool
    max_tracks_per_run: int
    batch_delay_seconds: float


class YandexDownloadEligibilityOut(BaseModel):
    total_missing: int
    eligible: int
    already_checked: int


class YandexFetchMissingIn(BaseModel):
    playlist_id: int = Field(gt=0)


class ProviderHealthComponentOut(BaseModel):
    state: str
    detail_code: str | None = None
    checked_at: datetime | None = None
    latency_ms: int | None = None
    credential_version: int | None = None
    retry_at: datetime | None = None
    stale: bool = False


class ProviderHealthOut(BaseModel):
    provider: str
    configured: bool
    credential_version: int | None = None
    credential_updated_at: datetime | None = None
    account: ProviderHealthComponentOut
    provider_api: ProviderHealthComponentOut
    sidecar: ProviderHealthComponentOut
    worker: ProviderHealthComponentOut


class ProviderHealthListOut(BaseModel):
    items: list[ProviderHealthOut]


class QobuzCredentialIn(BaseModel):
    token: SecretStr = Field(min_length=8, max_length=4096)
    user_id: SecretStr = Field(min_length=1, max_length=128)


class YandexCredentialIn(BaseModel):
    token: SecretStr = Field(min_length=8, max_length=4096)


class ProviderCredentialOut(BaseModel):
    provider: str
    configured: bool = True
    version: int
    updated_at: datetime
    label: str | None = None


class GoogleOAuthConfigIn(BaseModel):
    client_id: str = Field(min_length=20, max_length=512)
    client_secret: SecretStr = Field(min_length=8, max_length=4096)


class GoogleOAuthConfigOut(BaseModel):
    configured: bool
    version: int | None = None
    updated_at: datetime | None = None
    pending: bool = False


class GoogleOAuthStartOut(BaseModel):
    authorization_url: str


class StorageAccountOut(BaseModel):
    id: int
    provider: str
    email: str
    label: str | None = None
    enabled: bool
    priority: int
    state: str
    detail_code: str | None = None
    quota_limit_bytes: int | None = None
    quota_usage_bytes: int | None = None
    quota_trash_bytes: int | None = None
    free_bytes: int | None = None
    credential_version: int
    last_checked_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class StorageOverviewOut(BaseModel):
    primary_backend: str
    configured: bool
    oauth: GoogleOAuthConfigOut
    accounts: list[StorageAccountOut]
    total_limit_bytes: int | None = None
    total_usage_bytes: int
    total_free_bytes: int | None = None


class StorageAccountUpdateIn(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=-1000, le=1000)


class PlayerCredentialCreateIn(BaseModel):
    label: str = Field(min_length=1, max_length=128)


class PlayerCredentialOut(BaseModel):
    id: str
    label: str
    auth_scheme: str
    created_at: datetime
    last_used_at: datetime | None = None
    expires_at: datetime | None = None
    revoked_at: datetime | None = None


class PlayerCredentialListOut(BaseModel):
    items: list[PlayerCredentialOut]


class PlayerCredentialCreatedOut(PlayerCredentialOut):
    server_url: str
    api_key: str
