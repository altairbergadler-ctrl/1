import enum
from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.db import Base


def enum_values(enum_class):
    return [member.value for member in enum_class]


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class ServiceEnum(str, enum.Enum):
    spotify = "spotify"
    yandex = "yandex"


class MatchStatus(str, enum.Enum):
    ready = "READY"
    needs_review = "NEEDS_REVIEW"
    missing = "MISSING"


class JobStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    done = "done"
    failed = "failed"


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    login = Column(String(128), unique=True, nullable=False)
    token = Column(String(256), nullable=False)


class PlaylistSource(Base):
    __tablename__ = "playlist_sources"
    id = Column(Integer, primary_key=True)
    service = Column(
        Enum(ServiceEnum, values_callable=enum_values, name="service_enum"),
        nullable=False,
    )
    access_token = Column(Text)
    refresh_token = Column(Text)
    expires_at = Column(DateTime)
    playlists = relationship("Playlist", back_populates="source")


class Playlist(Base):
    __tablename__ = "playlists"
    id = Column(Integer, primary_key=True)
    source_id = Column(ForeignKey("playlist_sources.id"), nullable=False)
    external_id = Column(String(256), nullable=False)
    name = Column(String(512), nullable=False)
    snapshot_hash = Column(String(256))
    track_count = Column(Integer, default=0)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
    source = relationship("PlaylistSource", back_populates="playlists")
    items = relationship(
        "PlaylistItem", back_populates="playlist", cascade="all, delete-orphan"
    )
    __table_args__ = (UniqueConstraint("source_id", "external_id"),)


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
    match = relationship("Match", back_populates="playlist_item", uselist=False)


class Artist(Base):
    __tablename__ = "artists"
    id = Column(Integer, primary_key=True)
    name = Column(String(512), nullable=False)
    name_norm = Column(String(512), unique=True, nullable=False)
    mbid = Column(String(64))
    albums = relationship("Album", back_populates="artist")


class Album(Base):
    __tablename__ = "albums"
    id = Column(Integer, primary_key=True)
    artist_id = Column(ForeignKey("artists.id"), nullable=False)
    title = Column(String(512), nullable=False)
    title_norm = Column(String(512), nullable=False)
    year = Column(Integer)
    mbid = Column(String(64))
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
    path = Column(Text, unique=True, nullable=False)
    format = Column(String(16))
    bit_depth = Column(Integer)
    sample_rate = Column(Integer)
    size_bytes = Column(BigInteger)
    sha1 = Column(String(40), unique=True, nullable=False)
    scanned_at = Column(DateTime, default=utcnow)
    track = relationship("Track", back_populates="files")


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
