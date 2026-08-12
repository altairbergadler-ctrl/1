import enum
import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.db import Base


def enum_values(enum_class):
    return [member.value for member in enum_class]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def new_public_id() -> str:
    return str(uuid.uuid4())


class ServiceEnum(str, enum.Enum):
    spotify = "spotify"
    yandex = "yandex"
    manual = "manual"


class MatchStatus(str, enum.Enum):
    ready = "READY"
    needs_review = "NEEDS_REVIEW"
    missing = "MISSING"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class UserRole(str, enum.Enum):
    owner = "owner"
    user = "user"


class UserState(str, enum.Enum):
    pending = "pending"
    active = "active"
    disabled = "disabled"


class SessionKind(str, enum.Enum):
    google = "google"
    recovery = "recovery"


class JobScope(str, enum.Enum):
    user = "user"
    system = "system"


class UserProvider(str, enum.Enum):
    spotify = "spotify"
    yandex = "yandex"


class ProviderHealthState(str, enum.Enum):
    healthy = "healthy"
    expired = "expired"
    rate_limited = "rate_limited"
    provider_down = "provider_down"
    not_configured = "not_configured"


class ProviderHealthComponent(str, enum.Enum):
    account = "account"
    provider_api = "provider_api"
    sidecar = "sidecar"
    worker = "worker"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(320))
    email_key = Column(String(320))
    google_sub = Column(String(255), unique=True)
    display_name = Column(String(255))
    role = Column(
        Enum(UserRole, values_callable=enum_values, name="user_role"),
        nullable=False,
        default=UserRole.user,
    )
    state = Column(
        Enum(UserState, values_callable=enum_values, name="user_state"),
        nullable=False,
        default=UserState.pending,
    )
    is_bootstrap_owner = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    activated_at = Column(DateTime)
    last_login_at = Column(DateTime)
    sessions = relationship(
        "UserSession", back_populates="user", cascade="all, delete-orphan"
    )
    player_credentials = relationship(
        "PlayerCredential", back_populates="user", cascade="all, delete-orphan"
    )
    provider_credentials = relationship(
        "UserProviderCredential", back_populates="user", cascade="all, delete-orphan"
    )
    sources = relationship("PlaylistSource", back_populates="user")
    playlists = relationship(
        "Playlist", back_populates="user", overlaps="playlists,source"
    )
    __table_args__ = (
        Index(
            "uq_users_email_key",
            "email_key",
            unique=True,
            postgresql_where=text("email_key IS NOT NULL"),
            sqlite_where=text("email_key IS NOT NULL"),
        ),
        Index(
            "uq_users_single_bootstrap_owner",
            "is_bootstrap_owner",
            unique=True,
            postgresql_where=text("is_bootstrap_owner"),
            sqlite_where=text("is_bootstrap_owner = 1"),
        ),
        CheckConstraint(
            "(email IS NULL AND email_key IS NULL) OR "
            "(email IS NOT NULL AND email_key IS NOT NULL)",
            name="ck_users_email_pair",
        ),
        CheckConstraint(
            "email IS NOT NULL OR "
            "(is_bootstrap_owner AND state = 'pending' AND google_sub IS NULL)",
            name="ck_users_bootstrap_email",
        ),
    )


class UserSession(Base):
    __tablename__ = "user_sessions"
    id = Column(String(36), primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    kind = Column(
        Enum(SessionKind, values_callable=enum_values, name="session_kind"),
        nullable=False,
    )
    token_hash = Column(LargeBinary, nullable=False, unique=True)
    csrf_hash = Column(LargeBinary, nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    last_seen_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False)
    revoked_at = Column(DateTime)
    user = relationship("User", back_populates="sessions")
    __table_args__ = (
        Index("ix_user_sessions_user_active", "user_id", "revoked_at"),
        Index("ix_user_sessions_expires_at", "expires_at"),
    )


class PlayerCredential(Base):
    """One revocable OpenSubsonic API key; plaintext is never persisted."""

    __tablename__ = "player_credentials"
    id = Column(String(36), primary_key=True, default=new_public_id)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    label = Column(String(128), nullable=False)
    public_handle = Column(String(32), nullable=False, unique=True)
    secret_hash = Column(LargeBinary(32), nullable=False)
    auth_scheme = Column(String(32), nullable=False, default="api_key_v1")
    key_id = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    last_used_at = Column(DateTime)
    expires_at = Column(DateTime)
    revoked_at = Column(DateTime)
    user = relationship("User", back_populates="player_credentials")
    __table_args__ = (
        Index("ix_player_credentials_user_active", "user_id", "revoked_at"),
        Index("ix_player_credentials_expires_at", "expires_at"),
    )


class GoogleLoginAttempt(Base):
    __tablename__ = "google_login_attempts"
    id = Column(String(36), primary_key=True)
    state_hash = Column(LargeBinary, nullable=False, unique=True)
    browser_binding_hash = Column(LargeBinary, nullable=False)
    nonce_hash = Column(LargeBinary, nullable=False)
    pkce_ciphertext = Column(Text, nullable=False)
    pkce_nonce = Column(String(64), nullable=False)
    key_id = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime)
    __table_args__ = (Index("ix_google_login_attempts_expires_at", "expires_at"),)


class PlaylistSource(Base):
    __tablename__ = "playlist_sources"
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    service = Column(
        Enum(ServiceEnum, values_callable=enum_values, name="service_enum"),
        nullable=False,
    )
    expires_at = Column(DateTime)
    user = relationship("User", back_populates="sources")
    playlists = relationship(
        "Playlist",
        back_populates="source",
        cascade="all, delete-orphan",
        overlaps="playlists,user",
    )
    __table_args__ = (
        UniqueConstraint("user_id", "service", name="uq_playlist_sources_user_service"),
        UniqueConstraint("id", "user_id", name="uq_playlist_sources_id_user"),
    )


class Playlist(Base):
    __tablename__ = "playlists"
    id = Column(Integer, primary_key=True)
    source_id = Column(Integer, nullable=False)
    user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    external_id = Column(String(256), nullable=False)
    name = Column(String(512), nullable=False)
    snapshot_hash = Column(String(256))
    track_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    created_at = Column(DateTime, nullable=False, default=utcnow)
    opensubsonic_id = Column(String(36), nullable=False, unique=True, default=new_public_id)
    sync_revision = Column(BigInteger, nullable=False, default=1)
    sync_changed_at = Column(DateTime, nullable=False, default=utcnow)
    sync_fingerprint = Column(LargeBinary(32), nullable=False, default=b"")
    source = relationship(
        "PlaylistSource", back_populates="playlists", overlaps="playlists,user"
    )
    user = relationship(
        "User", back_populates="playlists", overlaps="playlists,source"
    )
    items = relationship(
        "PlaylistItem", back_populates="playlist", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("source_id", "external_id"),
        UniqueConstraint("id", "user_id", name="uq_playlists_id_user"),
        ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["playlist_sources.id", "playlist_sources.user_id"],
            name="fk_playlists_source_user",
            ondelete="RESTRICT",
        ),
    )


class PlaylistItem(Base):
    __tablename__ = "playlist_items"
    id = Column(Integer, primary_key=True)
    playlist_id = Column(ForeignKey("playlists.id"), nullable=False)
    position = Column(Integer, nullable=False)
    artist_raw = Column(String(512))
    title_raw = Column(String(512))
    album_raw = Column(String(512))
    artist_norm = Column(String(512), index=True)
    title_norm = Column(String(512), index=True)
    album_norm = Column(String(512))
    isrc = Column(String(32), index=True)
    duration_ms = Column(Integer)
    external_track_id = Column(String(256))
    playlist = relationship("Playlist", back_populates="items")
    match = relationship(
        "Match",
        back_populates="playlist_item",
        uselist=False,
        cascade="all, delete-orphan",
        single_parent=True,
    )
    __table_args__ = (
        UniqueConstraint(
            "playlist_id", "position", name="uq_playlist_items_playlist_position"
        ),
    )


class Artist(Base):
    __tablename__ = "artists"
    id = Column(Integer, primary_key=True)
    name = Column(String(512), nullable=False)
    name_norm = Column(String(512), unique=True, nullable=False)
    mbid = Column(String(64))
    opensubsonic_id = Column(String(36), nullable=False, unique=True, default=new_public_id)
    albums = relationship("Album", back_populates="artist")


class Album(Base):
    __tablename__ = "albums"
    id = Column(Integer, primary_key=True)
    artist_id = Column(ForeignKey("artists.id"), nullable=False)
    title = Column(String(512), nullable=False)
    title_norm = Column(String(512), nullable=False)
    year = Column(Integer)
    mbid = Column(String(64))
    opensubsonic_id = Column(String(36), nullable=False, unique=True, default=new_public_id)
    artist = relationship("Artist", back_populates="albums")
    tracks = relationship("Track", back_populates="album")
    __table_args__ = (
        UniqueConstraint(
            "artist_id", "title_norm", "year", name="uq_albums_artist_title_year"
        ),
    )


class Track(Base):
    __tablename__ = "tracks"
    id = Column(Integer, primary_key=True)
    album_id = Column(ForeignKey("albums.id"), nullable=False)
    title = Column(String(512), nullable=False)
    title_norm = Column(String(512), nullable=False, index=True)
    track_no = Column(Integer)
    disc_no = Column(Integer)
    duration_ms = Column(Integer)
    isrc = Column(String(32), index=True)
    mbid = Column(String(64))
    opensubsonic_id = Column(String(36), nullable=False, unique=True, default=new_public_id)
    album = relationship("Album", back_populates="tracks")
    files = relationship("File", back_populates="track")
    __table_args__ = (
        UniqueConstraint(
            "album_id",
            "disc_no",
            "track_no",
            "title_norm",
            name="uq_tracks_album_position_title",
        ),
    )


class File(Base):
    __tablename__ = "files"
    id = Column(Integer, primary_key=True)
    track_id = Column(ForeignKey("tracks.id"), nullable=False)
    # A local path is a temporary upload source. Once a verified remote
    # location is durable, it is cleared and the catalog row remains stable.
    path = Column(Text, unique=True, nullable=True)
    format = Column(String(16))
    bit_depth = Column(Integer)
    sample_rate = Column(Integer)
    size_bytes = Column(BigInteger)
    sha1 = Column(String(40), unique=True, nullable=False)
    scanned_at = Column(DateTime, default=utcnow)
    track = relationship("Track", back_populates="files")
    drive_locations = relationship(
        "DriveFileLocation",
        back_populates="file",
        cascade="all, delete-orphan",
    )


class Match(Base):
    __tablename__ = "matches"
    id = Column(Integer, primary_key=True)
    playlist_item_id = Column(ForeignKey("playlist_items.id"), unique=True)
    track_id = Column(ForeignKey("tracks.id"))
    confidence = Column(Float)
    method = Column(String(32))  # isrc | exact | fuzzy | manual
    status = Column(
        Enum(MatchStatus, values_callable=enum_values, name="match_status"),
        default=MatchStatus.missing,
    )
    playlist_item = relationship("PlaylistItem", back_populates="match")
    track = relationship("Track")


class Job(Base):
    __tablename__ = "jobs"
    id = Column(Integer, primary_key=True)
    type = Column(String(64), nullable=False)
    source_id = Column(
        ForeignKey("playlist_sources.id", ondelete="SET NULL"), nullable=True
    )
    playlist_id = Column(
        ForeignKey("playlists.id", ondelete="SET NULL"), nullable=True
    )
    user_id = Column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=True)
    scope = Column(
        Enum(JobScope, values_callable=enum_values, name="job_scope"),
        nullable=False,
        default=JobScope.user,
    )
    status = Column(
        Enum(JobStatus, values_callable=enum_values, name="job_status"),
        default=JobStatus.pending,
    )
    payload = Column(Text)
    error = Column(Text)
    created_at = Column(DateTime, default=utcnow)
    heartbeat_at = Column(DateTime, default=utcnow, nullable=False)
    lock_owner = Column(String(64))
    finished_at = Column(DateTime)
    __table_args__ = (
        CheckConstraint(
            "(scope = 'user' AND user_id IS NOT NULL) OR "
            "(scope = 'system' AND user_id IS NULL)",
            name="ck_jobs_scope_user",
        ),
        ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["playlist_sources.id", "playlist_sources.user_id"],
            name="fk_jobs_source_user",
        ),
        ForeignKeyConstraint(
            ["playlist_id", "user_id"],
            ["playlists.id", "playlists.user_id"],
            name="fk_jobs_playlist_user",
        ),
        Index("ix_jobs_user_created", "user_id", "created_at"),
        Index(
            "uq_jobs_active_import_source",
            "source_id",
            unique=True,
            postgresql_where=text(
                "type = 'import_playlists' AND status IN ('pending', 'running')"
            ),
            sqlite_where=text(
                "type = 'import_playlists' AND status IN ('pending', 'running')"
            ),
        ),
        Index(
            "uq_jobs_active_matching_user",
            "user_id",
            unique=True,
            postgresql_where=text(
                "type = 'run_matching' AND status IN ('pending', 'running')"
            ),
            sqlite_where=text(
                "type = 'run_matching' AND status IN ('pending', 'running')"
            ),
        ),
        Index(
            "uq_jobs_active_storage_migration",
            "type",
            unique=True,
            postgresql_where=text(
                "type = 'storage_migration' AND status IN ('pending', 'running')"
            ),
            sqlite_where=text(
                "type = 'storage_migration' AND status IN ('pending', 'running')"
            ),
        ),
    )


class ProviderAttempt(Base):
    """One terminal lookup outcome per provider and stable track identity."""

    __tablename__ = "provider_attempts"
    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False)
    lookup_key = Column(String(64), nullable=False)
    playlist_item_id = Column(
        ForeignKey("playlist_items.id", ondelete="SET NULL"), nullable=True
    )
    job_id = Column(ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True)
    status = Column(String(32), nullable=False)
    provider_item_id = Column(String(128))
    selection_method = Column(String(32))
    error_code = Column(String(128))
    attempted_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "provider", "lookup_key", name="uq_provider_attempts_provider_lookup"
        ),
        Index("ix_provider_attempts_playlist_item", "playlist_item_id"),
    )


class ProviderCredential(Base):
    """Authenticated provider material encrypted with an external AEAD key."""

    __tablename__ = "provider_credentials"
    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False, unique=True)
    ciphertext = Column(Text, nullable=False)
    nonce = Column(String(64), nullable=False)
    key_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    validated_at = Column(DateTime, nullable=False)


class UserProviderCredential(Base):
    """Encrypted playlist-provider material scoped to one user."""

    __tablename__ = "user_provider_credentials"
    id = Column(Integer, primary_key=True)
    user_id = Column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    provider = Column(
        Enum(UserProvider, values_callable=enum_values, name="user_provider"),
        nullable=False,
    )
    ciphertext = Column(Text, nullable=False)
    nonce = Column(String(64), nullable=False)
    key_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    expires_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    validated_at = Column(DateTime, nullable=False)
    user = relationship("User", back_populates="provider_credentials")
    __table_args__ = (
        UniqueConstraint(
            "user_id", "provider", name="uq_user_provider_credentials_user_provider"
        ),
    )


class ProviderHealth(Base):
    """Latest non-sensitive health result for one provider component."""

    __tablename__ = "provider_health"
    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False)
    component = Column(
        Enum(
            ProviderHealthComponent,
            values_callable=enum_values,
            name="provider_health_component",
        ),
        nullable=False,
    )
    state = Column(
        Enum(
            ProviderHealthState,
            values_callable=enum_values,
            name="provider_health_state",
        ),
        nullable=False,
    )
    detail_code = Column(String(64))
    latency_ms = Column(Integer)
    credential_version = Column(Integer)
    checked_at = Column(DateTime, default=utcnow, nullable=False)
    retry_at = Column(DateTime)
    __table_args__ = (
        UniqueConstraint(
            "provider", "component", name="uq_provider_health_provider_component"
        ),
        Index("ix_provider_health_provider", "provider"),
    )


class StorageAccount(Base):
    """One independently authorized Google Drive quota pool."""

    __tablename__ = "storage_accounts"
    id = Column(Integer, primary_key=True)
    provider = Column(String(32), nullable=False, default="google_drive")
    email = Column(String(320), nullable=False, unique=True)
    label = Column(String(512))
    root_folder_id = Column(String(256), nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    priority = Column(Integer, nullable=False, default=0)
    state = Column(String(32), nullable=False, default="healthy")
    detail_code = Column(String(64))
    quota_limit_bytes = Column(BigInteger)
    quota_usage_bytes = Column(BigInteger)
    quota_trash_bytes = Column(BigInteger)
    credential_version = Column(Integer, nullable=False, default=1)
    last_checked_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    locations = relationship("DriveFileLocation", back_populates="account")
    __table_args__ = (
        Index("ix_storage_accounts_enabled_priority", "enabled", "priority"),
    )


class StorageSecret(Base):
    """AEAD envelope for OAuth application or account material."""

    __tablename__ = "storage_secrets"
    id = Column(Integer, primary_key=True)
    name = Column(String(256), nullable=False, unique=True)
    account_id = Column(
        ForeignKey("storage_accounts.id", ondelete="CASCADE"), nullable=True
    )
    ciphertext = Column(Text, nullable=False)
    nonce = Column(String(64), nullable=False)
    key_id = Column(String(64), nullable=False)
    version = Column(Integer, nullable=False, default=1)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    validated_at = Column(DateTime)
    __table_args__ = (Index("ix_storage_secrets_account_id", "account_id"),)


class StorageOAuthState(Base):
    """Single-use hashed OAuth state with an encrypted PKCE verifier."""

    __tablename__ = "storage_oauth_states"
    id = Column(Integer, primary_key=True)
    state_hash = Column(String(64), nullable=False, unique=True)
    secret_id = Column(
        ForeignKey("storage_secrets.id", ondelete="CASCADE"), nullable=False
    )
    initiated_by_user_id = Column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at = Column(DateTime, nullable=False)
    consumed_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    __table_args__ = (Index("ix_storage_oauth_states_expires_at", "expires_at"),)


class DriveFileLocation(Base):
    """Verified Google Drive object backing one logical catalog file."""

    __tablename__ = "drive_file_locations"
    id = Column(Integer, primary_key=True)
    file_id = Column(ForeignKey("files.id", ondelete="CASCADE"), nullable=False)
    account_id = Column(
        ForeignKey("storage_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    remote_file_id = Column(String(256), nullable=False)
    remote_name = Column(String(1024), nullable=False)
    size_bytes = Column(BigInteger, nullable=False)
    sha1 = Column(String(40), nullable=False)
    state = Column(String(32), nullable=False, default="healthy")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    verified_at = Column(DateTime, default=utcnow, nullable=False)
    file = relationship("File", back_populates="drive_locations")
    account = relationship("StorageAccount", back_populates="locations")
    __table_args__ = (
        UniqueConstraint(
            "account_id", "remote_file_id", name="uq_drive_location_account_remote"
        ),
        UniqueConstraint(
            "file_id", "account_id", name="uq_drive_location_file_account"
        ),
        Index("ix_drive_file_locations_file_id", "file_id"),
        Index("ix_drive_file_locations_account_id", "account_id"),
    )
