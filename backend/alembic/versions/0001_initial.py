"""Create the MVP schema.

Revision ID: 0001_initial
Revises:
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa


revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


service_enum = sa.Enum("spotify", "yandex", name="service_enum")
match_status = sa.Enum("READY", "NEEDS_REVIEW", "MISSING", name="match_status")
job_status = sa.Enum("pending", "running", "done", "failed", name="job_status")


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("login", sa.String(length=128), nullable=False),
        sa.Column("token", sa.String(length=256), nullable=False),
        sa.UniqueConstraint("login"),
    )
    op.create_table(
        "playlist_sources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("service", service_enum, nullable=False),
        sa.Column("access_token", sa.Text()),
        sa.Column("refresh_token", sa.Text()),
        sa.Column("expires_at", sa.DateTime()),
    )
    op.create_table(
        "artists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("name_norm", sa.String(length=512), nullable=False),
        sa.Column("mbid", sa.String(length=64)),
        sa.UniqueConstraint("name_norm"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("type", sa.String(length=64), nullable=False),
        sa.Column("status", job_status),
        sa.Column("payload", sa.Text()),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime()),
        sa.Column("heartbeat_at", sa.DateTime(), nullable=False),
        sa.Column("lock_owner", sa.String(length=64)),
        sa.Column("finished_at", sa.DateTime()),
    )
    op.create_table(
        "playlists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "source_id",
            sa.Integer(),
            sa.ForeignKey("playlist_sources.id"),
            nullable=False,
        ),
        sa.Column("external_id", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=512), nullable=False),
        sa.Column("snapshot_hash", sa.String(length=256)),
        sa.Column("track_count", sa.Integer()),
        sa.Column("updated_at", sa.DateTime()),
        sa.UniqueConstraint("source_id", "external_id"),
    )
    op.create_table(
        "albums",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "artist_id", sa.Integer(), sa.ForeignKey("artists.id"), nullable=False
        ),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("title_norm", sa.String(length=512), nullable=False),
        sa.Column("year", sa.Integer()),
        sa.Column("mbid", sa.String(length=64)),
        sa.UniqueConstraint(
            "artist_id",
            "title_norm",
            "year",
            name="uq_albums_artist_title_year",
        ),
    )
    op.create_table(
        "playlist_items",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "playlist_id", sa.Integer(), sa.ForeignKey("playlists.id"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("artist_raw", sa.String(length=512)),
        sa.Column("title_raw", sa.String(length=512)),
        sa.Column("album_raw", sa.String(length=512)),
        sa.Column("artist_norm", sa.String(length=512)),
        sa.Column("title_norm", sa.String(length=512)),
        sa.Column("album_norm", sa.String(length=512)),
        sa.Column("isrc", sa.String(length=32)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("external_track_id", sa.String(length=256)),
    )
    op.create_index("ix_playlist_items_artist_norm", "playlist_items", ["artist_norm"])
    op.create_index("ix_playlist_items_title_norm", "playlist_items", ["title_norm"])
    op.create_index("ix_playlist_items_isrc", "playlist_items", ["isrc"])
    op.create_table(
        "tracks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("album_id", sa.Integer(), sa.ForeignKey("albums.id"), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("title_norm", sa.String(length=512), nullable=False),
        sa.Column("track_no", sa.Integer()),
        sa.Column("disc_no", sa.Integer()),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("isrc", sa.String(length=32)),
        sa.Column("mbid", sa.String(length=64)),
        sa.UniqueConstraint(
            "album_id",
            "disc_no",
            "track_no",
            "title_norm",
            name="uq_tracks_album_position_title",
        ),
    )
    op.create_index("ix_tracks_title_norm", "tracks", ["title_norm"])
    op.create_index("ix_tracks_isrc", "tracks", ["isrc"])
    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("format", sa.String(length=16)),
        sa.Column("bit_depth", sa.Integer()),
        sa.Column("sample_rate", sa.Integer()),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("sha1", sa.String(length=40), nullable=False),
        sa.Column("scanned_at", sa.DateTime()),
        sa.UniqueConstraint("path"),
        sa.UniqueConstraint("sha1"),
    )
    op.create_table(
        "matches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "playlist_item_id",
            sa.Integer(),
            sa.ForeignKey("playlist_items.id"),
            unique=True,
        ),
        sa.Column("track_id", sa.Integer(), sa.ForeignKey("tracks.id")),
        sa.Column("confidence", sa.Float()),
        sa.Column("method", sa.String(length=32)),
        sa.Column("status", match_status),
    )


def downgrade() -> None:
    op.drop_table("matches")
    op.drop_table("files")
    op.drop_index("ix_tracks_isrc", table_name="tracks")
    op.drop_index("ix_tracks_title_norm", table_name="tracks")
    op.drop_table("tracks")
    op.drop_index("ix_playlist_items_isrc", table_name="playlist_items")
    op.drop_index("ix_playlist_items_title_norm", table_name="playlist_items")
    op.drop_index("ix_playlist_items_artist_norm", table_name="playlist_items")
    op.drop_table("playlist_items")
    op.drop_table("albums")
    op.drop_table("playlists")
    op.drop_table("jobs")
    op.drop_table("artists")
    op.drop_table("playlist_sources")
    op.drop_table("users")
    match_status.drop(op.get_bind(), checkfirst=True)
    job_status.drop(op.get_bind(), checkfirst=True)
    service_enum.drop(op.get_bind(), checkfirst=True)
