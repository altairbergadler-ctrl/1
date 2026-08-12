"""expand schema for Google users and private ownership

Revision ID: 0008_google_user_auth_expand
Revises: 0007_manual_playlist_source
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "0008_google_user_auth_expand"
down_revision = "0007_manual_playlist_source"
branch_labels = None
depends_on = None


def _enum(name: str, *values: str):
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        postgresql.ENUM(*values, name=name).create(bind, checkfirst=True)
        return postgresql.ENUM(*values, name=name, create_type=False)
    return sa.Enum(*values, name=name)


def upgrade() -> None:
    # This is deliberately an expand-only step. Ownership columns stay nullable
    # until the bootstrap command assigns every legacy row to the single owner.
    user_role = _enum("user_role", "owner", "user")
    user_state = _enum("user_state", "pending", "active", "disabled")
    session_kind = _enum("session_kind", "google", "recovery")
    job_scope = _enum("job_scope", "user", "system")
    user_provider = _enum("user_provider", "spotify", "yandex")

    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(length=320)))
        batch_op.add_column(sa.Column("email_key", sa.String(length=320)))
        batch_op.add_column(sa.Column("google_sub", sa.String(length=255)))
        batch_op.add_column(sa.Column("display_name", sa.String(length=255)))
        batch_op.add_column(sa.Column("role", user_role, nullable=True))
        batch_op.add_column(sa.Column("state", user_state, nullable=True))
        batch_op.add_column(
            sa.Column(
                "is_bootstrap_owner",
                sa.Boolean(),
                nullable=True,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column("created_at", sa.DateTime()))
        batch_op.add_column(sa.Column("activated_at", sa.DateTime()))
        batch_op.add_column(sa.Column("last_login_at", sa.DateTime()))

    op.create_table(
        "user_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", session_kind, nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("csrf_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.UniqueConstraint("token_hash", name="uq_user_sessions_token_hash"),
    )
    op.create_index(
        "ix_user_sessions_user_active",
        "user_sessions",
        ["user_id", "revoked_at"],
    )
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    op.create_table(
        "google_login_attempts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("state_hash", sa.LargeBinary(), nullable=False),
        sa.Column("browser_binding_hash", sa.LargeBinary(), nullable=False),
        sa.Column("nonce_hash", sa.LargeBinary(), nullable=False),
        sa.Column("pkce_ciphertext", sa.Text(), nullable=False),
        sa.Column("pkce_nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.UniqueConstraint(
            "state_hash", name="uq_google_login_attempts_state_hash"
        ),
    )
    op.create_index(
        "ix_google_login_attempts_expires_at",
        "google_login_attempts",
        ["expires_at"],
    )

    op.create_table(
        "user_provider_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", user_provider, nullable=False),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("validated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_provider_credentials_user_provider",
        ),
    )

    # Preserve the exact encrypted global Spotify envelope until production
    # acceptance.  The backfill command moves its decrypted value into the
    # user-scoped vault, while this copy permits a lossless schema rollback.
    op.create_table(
        "multitenancy_migration_credentials",
        sa.Column("provider", sa.String(length=32), primary_key=True),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("validated_at", sa.DateTime(), nullable=False),
    )

    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_playlist_sources_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_playlists_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("playlist_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("user_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("scope", job_scope, nullable=True))
        batch_op.create_foreign_key(
            "fk_jobs_playlist_id_playlists",
            "playlists",
            ["playlist_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch_op.create_foreign_key(
            "fk_jobs_user_id_users",
            "users",
            ["user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    with op.batch_alter_table("storage_oauth_states") as batch_op:
        batch_op.add_column(
            sa.Column("initiated_by_user_id", sa.Integer(), nullable=True)
        )
        batch_op.create_foreign_key(
            "fk_storage_oauth_states_user_id_users",
            "users",
            ["initiated_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    bind = op.get_bind()
    # Restore the encrypted legacy envelope before removing the user-scoped
    # vault so a pre-multitenancy release can still use the original credential.
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
        batch_op.drop_constraint(
            "fk_storage_oauth_states_user_id_users", type_="foreignkey"
        )
        batch_op.drop_column("initiated_by_user_id")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint("fk_jobs_user_id_users", type_="foreignkey")
        batch_op.drop_constraint(
            "fk_jobs_playlist_id_playlists", type_="foreignkey"
        )
        batch_op.drop_column("scope")
        batch_op.drop_column("user_id")
        batch_op.drop_column("playlist_id")
    with op.batch_alter_table("playlists") as batch_op:
        batch_op.drop_constraint("fk_playlists_user_id_users", type_="foreignkey")
        batch_op.drop_column("user_id")
    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.drop_constraint(
            "fk_playlist_sources_user_id_users", type_="foreignkey"
        )
        batch_op.drop_column("user_id")
    op.drop_table("user_provider_credentials")
    op.drop_table("multitenancy_migration_credentials")
    op.drop_index(
        "ix_google_login_attempts_expires_at", table_name="google_login_attempts"
    )
    op.drop_table("google_login_attempts")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_active", table_name="user_sessions")
    op.drop_table("user_sessions")
    op.execute(
        "DELETE FROM users WHERE is_bootstrap_owner = true "
        "AND email IS NULL AND google_sub IS NULL AND state = 'pending' "
        "AND login = 'bootstrap-owner-disabled' "
        "AND token = 'disabled-not-an-authentication-token'"
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("last_login_at")
        batch_op.drop_column("activated_at")
        batch_op.drop_column("created_at")
        batch_op.drop_column("is_bootstrap_owner")
        batch_op.drop_column("state")
        batch_op.drop_column("role")
        batch_op.drop_column("display_name")
        batch_op.drop_column("google_sub")
        batch_op.drop_column("email_key")
        batch_op.drop_column("email")
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for name in ("user_provider", "job_scope", "session_kind", "user_state", "user_role"):
            postgresql.ENUM(name=name).drop(bind, checkfirst=True)
