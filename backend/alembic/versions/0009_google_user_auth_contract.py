"""contract schema after multi-user ownership backfill

Revision ID: 0009_google_user_auth_contract
Revises: 0008_google_user_auth_expand
Create Date: 2026-08-12
"""

from __future__ import annotations

import json
from datetime import datetime

from alembic import op
import sqlalchemy as sa


revision = "0009_google_user_auth_contract"
down_revision = "0008_google_user_auth_expand"
branch_labels = None
depends_on = None

SYSTEM_JOB_TYPES = {
    "provider_health_check",
    "storage_health_check",
    "storage_migration",
    "scan_library",
}
USER_JOB_TYPES = {
    "import_playlists",
    "run_matching",
    "qobuz_download",
    "yandex_download",
}


def _playlist_id(payload: str | None) -> int | None:
    try:
        value = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    item = value.get("playlist_id")
    if item is None and isinstance(value.get("downloads"), dict):
        item = value["downloads"].get("playlist_id")
    return item if isinstance(item, int) and not isinstance(item, bool) else None


def _ensure_backfill() -> int:
    bind = op.get_bind()
    legacy_source_credentials = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM playlist_sources "
                "WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL"
            )
        ).scalar_one()
    )
    if legacy_source_credentials:
        raise RuntimeError(
            "Run app.commands.migrate_database before the contract migration"
        )
    users = list(bind.execute(sa.text("SELECT id, is_bootstrap_owner FROM users")))
    if not users:
        owner_id = int(
            bind.execute(
                sa.text(
                    "INSERT INTO users "
                    "(login, token, role, state, is_bootstrap_owner, created_at) "
                    "VALUES (:login, :token, 'owner', 'pending', true, :created_at) "
                    "RETURNING id"
                ),
                {
                    "login": "bootstrap-owner-disabled",
                    "token": "disabled-not-an-authentication-token",
                    "created_at": datetime.utcnow(),
                },
            ).scalar_one()
        )
    elif len(users) == 1 and bool(users[0].is_bootstrap_owner):
        owner_id = int(users[0].id)
    else:
        raise RuntimeError("Refusing ambiguous bootstrap user migration")

    bind.execute(
        sa.text(
            "UPDATE users SET role = COALESCE(role, 'owner'), "
            "state = COALESCE(state, 'pending'), "
            "is_bootstrap_owner = COALESCE(is_bootstrap_owner, true), "
            "created_at = COALESCE(created_at, :created_at) WHERE id = :owner_id"
        ),
        {"owner_id": owner_id, "created_at": datetime.utcnow()},
    )
    bind.execute(
        sa.text("UPDATE playlist_sources SET user_id = :owner_id WHERE user_id IS NULL"),
        {"owner_id": owner_id},
    )
    bind.execute(
        sa.text("UPDATE playlists SET user_id = :owner_id WHERE user_id IS NULL"),
        {"owner_id": owner_id},
    )
    bind.execute(
        sa.text(
            "UPDATE storage_oauth_states SET initiated_by_user_id = :owner_id "
            "WHERE initiated_by_user_id IS NULL"
        ),
        {"owner_id": owner_id},
    )
    rows = list(bind.execute(sa.text("SELECT id, type, payload, user_id, scope FROM jobs")))
    for row in rows:
        if row.type in SYSTEM_JOB_TYPES:
            bind.execute(
                sa.text(
                    "UPDATE jobs SET scope = 'system', user_id = NULL WHERE id = :id"
                ),
                {"id": row.id},
            )
        elif row.type in USER_JOB_TYPES:
            bind.execute(
                sa.text(
                    "UPDATE jobs SET scope = 'user', user_id = :owner_id, "
                    "playlist_id = COALESCE(playlist_id, :playlist_id) WHERE id = :id"
                ),
                {
                    "id": row.id,
                    "owner_id": owner_id,
                    "playlist_id": _playlist_id(row.payload),
                },
            )
        else:
            raise RuntimeError(f"Unknown job type: {row.type}")

    spotify_global = bind.execute(
        sa.text("SELECT count(*) FROM provider_credentials WHERE provider = 'spotify'")
    ).scalar_one()
    if spotify_global:
        raise RuntimeError(
            "Run app.commands.bootstrap_multitenancy before the contract migration"
        )
    yandex_global = bind.execute(
        sa.text("SELECT count(*) FROM provider_credentials WHERE provider = 'yandex'")
    ).scalar_one()
    yandex_source = bind.execute(
        sa.text("SELECT count(*) FROM playlist_sources WHERE service = 'yandex'")
    ).scalar_one()
    yandex_user = bind.execute(
        sa.text(
            "SELECT count(*) FROM user_provider_credentials "
            "WHERE user_id = :owner_id AND provider = 'yandex'"
        ),
        {"owner_id": owner_id},
    ).scalar_one()
    if yandex_global and yandex_source and not yandex_user:
        raise RuntimeError(
            "Run app.commands.bootstrap_multitenancy before the contract migration"
        )
    return owner_id


def upgrade() -> None:
    _ensure_backfill()
    bind = op.get_bind()

    op.create_index(
        "uq_users_email_key",
        "users",
        ["email_key"],
        unique=True,
        postgresql_where=sa.text("email_key IS NOT NULL"),
        sqlite_where=sa.text("email_key IS NOT NULL"),
    )
    op.create_index(
        "uq_users_single_bootstrap_owner",
        "users",
        ["is_bootstrap_owner"],
        unique=True,
        postgresql_where=sa.text("is_bootstrap_owner"),
        sqlite_where=sa.text("is_bootstrap_owner = 1"),
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("role", existing_type=sa.Enum(name="user_role"), nullable=False)
        batch_op.alter_column("state", existing_type=sa.Enum(name="user_state"), nullable=False)
        batch_op.alter_column("is_bootstrap_owner", existing_type=sa.Boolean(), nullable=False, server_default=None)
        batch_op.alter_column("created_at", existing_type=sa.DateTime(), nullable=False)
        batch_op.create_check_constraint(
            "ck_users_email_pair",
            "(email IS NULL AND email_key IS NULL) OR "
            "(email IS NOT NULL AND email_key IS NOT NULL)",
        )
        batch_op.create_check_constraint(
            "ck_users_bootstrap_email",
            "email IS NOT NULL OR "
            "(is_bootstrap_owner AND state = 'pending' AND google_sub IS NULL)",
        )
        batch_op.drop_column("token")
        batch_op.drop_column("login")

    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.drop_constraint("uq_playlist_sources_service", type_="unique")
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint(
            "uq_playlist_sources_user_service", ["user_id", "service"]
        )
        batch_op.create_unique_constraint(
            "uq_playlist_sources_id_user", ["id", "user_id"]
        )
        batch_op.drop_column("refresh_token")
        batch_op.drop_column("access_token")

    with op.batch_alter_table("playlists") as batch_op:
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
        batch_op.create_unique_constraint("uq_playlists_id_user", ["id", "user_id"])
        batch_op.create_foreign_key(
            "fk_playlists_source_user",
            "playlist_sources",
            ["source_id", "user_id"],
            ["id", "user_id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("jobs") as batch_op:
        batch_op.alter_column("scope", existing_type=sa.Enum(name="job_scope"), nullable=False)
        batch_op.create_check_constraint(
            "ck_jobs_scope_user",
            "(scope = 'user' AND user_id IS NOT NULL) OR "
            "(scope = 'system' AND user_id IS NULL)",
        )
        batch_op.create_foreign_key(
            "fk_jobs_source_user",
            "playlist_sources",
            ["source_id", "user_id"],
            ["id", "user_id"],
        )
        batch_op.create_foreign_key(
            "fk_jobs_playlist_user",
            "playlists",
            ["playlist_id", "user_id"],
            ["id", "user_id"],
        )
    op.create_index("ix_jobs_user_created", "jobs", ["user_id", "created_at"])
    op.drop_index("uq_jobs_active_matching", table_name="jobs")
    op.create_index(
        "uq_jobs_active_matching_user",
        "jobs",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'run_matching' AND status IN ('pending', 'running')"
        ),
        sqlite_where=sa.text(
            "type = 'run_matching' AND status IN ('pending', 'running')"
        ),
    )

    with op.batch_alter_table("storage_oauth_states") as batch_op:
        batch_op.alter_column(
            "initiated_by_user_id", existing_type=sa.Integer(), nullable=False
        )

    if bind.dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_provider_credentials_system_provider",
            "provider_credentials",
            "provider IN ('qobuz', 'yandex')",
        )


def downgrade() -> None:
    bind = op.get_bind()
    user_count = int(bind.execute(sa.text("SELECT count(*) FROM users")).scalar_one())
    duplicate_sources = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM (SELECT service FROM playlist_sources "
                "GROUP BY service HAVING count(*) > 1) AS duplicates"
            )
        ).scalar_one()
    )
    identity_rows = int(
        bind.execute(
            sa.text(
                "SELECT count(*) FROM users WHERE email IS NOT NULL "
                "OR google_sub IS NOT NULL OR state <> 'pending'"
            )
        ).scalar_one()
    )
    if user_count > 1 or duplicate_sources or identity_rows:
        raise RuntimeError("Lossless downgrade is impossible after multi-user activation")

    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "ck_provider_credentials_system_provider",
            "provider_credentials",
            type_="check",
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            "INSERT INTO provider_credentials "
            "(provider, ciphertext, nonce, key_id, version, created_at, updated_at, validated_at) "
            "SELECT provider, ciphertext, nonce, key_id, version, created_at, updated_at, validated_at "
            "FROM multitenancy_migration_credentials WHERE provider = 'spotify' "
            "ON CONFLICT (provider) DO NOTHING"
        )
    else:
        op.execute(
            "INSERT OR IGNORE INTO provider_credentials "
            "(provider, ciphertext, nonce, key_id, version, created_at, updated_at, validated_at) "
            "SELECT provider, ciphertext, nonce, key_id, version, created_at, updated_at, validated_at "
            "FROM multitenancy_migration_credentials WHERE provider = 'spotify'"
        )

    with op.batch_alter_table("storage_oauth_states") as batch_op:
        batch_op.alter_column(
            "initiated_by_user_id", existing_type=sa.Integer(), nullable=True
        )
    op.drop_index("uq_jobs_active_matching_user", table_name="jobs")
    op.create_index(
        "uq_jobs_active_matching",
        "jobs",
        ["type"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'run_matching' AND status IN ('pending', 'running')"
        ),
        sqlite_where=sa.text(
            "type = 'run_matching' AND status IN ('pending', 'running')"
        ),
    )
    op.drop_index("ix_jobs_user_created", table_name="jobs")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint("fk_jobs_playlist_user", type_="foreignkey")
        batch_op.drop_constraint("fk_jobs_source_user", type_="foreignkey")
        batch_op.drop_constraint("ck_jobs_scope_user", type_="check")
        batch_op.alter_column("scope", existing_type=sa.Enum(name="job_scope"), nullable=True)
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.drop_constraint("fk_playlists_source_user", type_="foreignkey")
        batch_op.drop_constraint("uq_playlists_id_user", type_="unique")
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.add_column(sa.Column("access_token", sa.Text()))
        batch_op.add_column(sa.Column("refresh_token", sa.Text()))
        batch_op.drop_constraint("uq_playlist_sources_id_user", type_="unique")
        batch_op.drop_constraint("uq_playlist_sources_user_service", type_="unique")
        batch_op.create_unique_constraint("uq_playlist_sources_service", ["service"])
        batch_op.alter_column("user_id", existing_type=sa.Integer(), nullable=True)
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("login", sa.String(length=128)))
        batch_op.add_column(sa.Column("token", sa.String(length=256)))
        batch_op.drop_constraint("ck_users_bootstrap_email", type_="check")
        batch_op.drop_constraint("ck_users_email_pair", type_="check")
        batch_op.alter_column("created_at", existing_type=sa.DateTime(), nullable=True)
        batch_op.alter_column("is_bootstrap_owner", existing_type=sa.Boolean(), nullable=True)
        batch_op.alter_column("state", existing_type=sa.Enum(name="user_state"), nullable=True)
        batch_op.alter_column("role", existing_type=sa.Enum(name="user_role"), nullable=True)
    bind.execute(
        sa.text(
            "UPDATE users SET login = 'bootstrap-owner-disabled', "
            "token = 'disabled-not-an-authentication-token'"
        )
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("login", existing_type=sa.String(length=128), nullable=False)
        batch_op.alter_column("token", existing_type=sa.String(length=256), nullable=False)
        batch_op.create_unique_constraint("uq_users_login", ["login"])
    op.drop_index("uq_users_single_bootstrap_owner", table_name="users")
    op.drop_index("uq_users_email_key", table_name="users")
