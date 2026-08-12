"""add OpenSubsonic player credentials and stable sync metadata

Revision ID: 0010_open_subsonic_players
Revises: 0009_google_user_auth_contract
Create Date: 2026-08-12
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "0010_open_subsonic_players"
down_revision = "0009_google_user_auth_contract"
branch_labels = None
depends_on = None

PUBLIC_ID_NAMESPACE = uuid.UUID("3e3c7428-f4da-4e65-9e95-5d20422eec4c")


def _public_id(kind: str, row_id: int) -> str:
    return str(uuid.uuid5(PUBLIC_ID_NAMESPACE, f"audiofeel:{kind}:{row_id}"))


def _playlist_fingerprint(bind, playlist_id: int, name: str) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"audiofeel-playlist-sync-v1\0")
    digest.update((name or "").encode("utf-8"))
    inspector = sa.inspect(bind)
    if not all(inspector.has_table(table) for table in ("playlist_items", "matches", "tracks", "files")):
        return digest.digest()
    playable = "f.path IS NOT NULL"
    if all(
        inspector.has_table(table)
        for table in ("drive_file_locations", "storage_accounts")
    ):
        playable += (
            " OR EXISTS (SELECT 1 FROM drive_file_locations d "
            "JOIN storage_accounts sa ON sa.id = d.account_id "
            "WHERE d.file_id = f.id AND d.state = 'healthy' "
            "AND sa.enabled = true AND sa.state = 'healthy')"
        )
    rows = bind.execute(
        sa.text(
            "SELECT pi.position, t.id AS track_id, t.title, f.sha1, f.format, f.size_bytes "
            "FROM playlist_items pi "
            "JOIN matches m ON m.playlist_item_id = pi.id AND m.status = 'READY' "
            "JOIN tracks t ON t.id = m.track_id "
            "JOIN files f ON f.track_id = t.id "
            f"WHERE pi.playlist_id = :playlist_id AND ({playable}) "
            "ORDER BY pi.position, f.bit_depth DESC, f.sample_rate DESC, "
            "f.size_bytes DESC, f.id ASC"
        ),
        {"playlist_id": playlist_id},
    )
    seen: set[int] = set()
    for row in rows:
        if row.position in seen:
            continue
        seen.add(row.position)
        digest.update(
            (
                f"\0{row.position}\0{_public_id('song', row.track_id)}\0"
                f"{row.title or ''}\0{row.sha1 or ''}\0{row.format or ''}\0"
                f"{row.size_bytes or 0}"
            ).encode("utf-8")
        )
    return digest.digest()


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "player_credentials",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=128), nullable=False),
        sa.Column("public_handle", sa.String(length=32), nullable=False, unique=True),
        sa.Column("secret_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("auth_scheme", sa.String(length=32), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime()),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("revoked_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_player_credentials_user_active",
        "player_credentials",
        ["user_id", "revoked_at"],
    )
    op.create_index(
        "ix_player_credentials_expires_at", "player_credentials", ["expires_at"]
    )

    stable_tables = [
        table
        for table in ("artists", "albums", "tracks", "playlists")
        if sa.inspect(bind).has_table(table)
    ]
    for table in stable_tables:
        with op.batch_alter_table(table) as batch_op:
            batch_op.add_column(sa.Column("opensubsonic_id", sa.String(length=36)))

    playlist_now: dict[int, datetime] = {}
    for table, kind in (
        ("artists", "artist"),
        ("albums", "album"),
        ("tracks", "song"),
        ("playlists", "playlist"),
    ):
        if table not in stable_tables:
            continue
        for row in bind.execute(sa.text(f"SELECT id FROM {table}")):
            bind.execute(
                sa.text(
                    f"UPDATE {table} SET opensubsonic_id = :public_id WHERE id = :id"
                ),
                {"id": row.id, "public_id": _public_id(kind, row.id)},
            )

    with op.batch_alter_table("playlists") as batch_op:
        batch_op.add_column(sa.Column("created_at", sa.DateTime()))
        batch_op.add_column(sa.Column("sync_revision", sa.BigInteger()))
        batch_op.add_column(sa.Column("sync_changed_at", sa.DateTime()))
        batch_op.add_column(sa.Column("sync_fingerprint", sa.LargeBinary(length=32)))

    playlists = list(bind.execute(sa.text("SELECT id, name, updated_at FROM playlists")))
    for row in playlists:
        changed_at = row.updated_at or datetime.utcnow()
        playlist_now[row.id] = changed_at
        bind.execute(
            sa.text(
                "UPDATE playlists SET created_at = :changed_at, sync_revision = 1, "
                "sync_changed_at = :changed_at, sync_fingerprint = :fingerprint "
                "WHERE id = :id"
            ),
            {
                "id": row.id,
                "changed_at": changed_at,
                "fingerprint": _playlist_fingerprint(bind, row.id, row.name),
            },
        )

    for table in stable_tables:
        with op.batch_alter_table(table) as batch_op:
            batch_op.alter_column(
                "opensubsonic_id", existing_type=sa.String(length=36), nullable=False
            )
            batch_op.create_unique_constraint(
                f"uq_{table}_opensubsonic_id", ["opensubsonic_id"]
            )
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column("sync_revision", existing_type=sa.BigInteger(), nullable=False)
        batch_op.alter_column("sync_changed_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.alter_column(
            "sync_fingerprint", existing_type=sa.LargeBinary(length=32), nullable=False
        )


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_index("ix_player_credentials_expires_at", table_name="player_credentials")
    op.drop_index("ix_player_credentials_user_active", table_name="player_credentials")
    op.drop_table("player_credentials")
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.drop_column("sync_fingerprint")
        batch_op.drop_column("sync_changed_at")
        batch_op.drop_column("sync_revision")
        batch_op.drop_column("created_at")
    for table in ("playlists", "tracks", "albums", "artists"):
        if not sa.inspect(bind).has_table(table):
            continue
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_constraint(f"uq_{table}_opensubsonic_id", type_="unique")
            batch_op.drop_column("opensubsonic_id")
