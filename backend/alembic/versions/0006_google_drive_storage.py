"""encrypted multi-account Google Drive storage

Revision ID: 0006_google_drive_storage
Revises: 0005_provider_health_credentials
Create Date: 2026-08-11
"""

from alembic import op
import sqlalchemy as sa


revision = "0006_google_drive_storage"
down_revision = "0005_provider_health_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("files", "path", existing_type=sa.Text(), nullable=True)
    op.create_index(
        "uq_jobs_active_storage_migration",
        "jobs",
        ["type"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'storage_migration' AND status IN ('pending', 'running')"
        ),
    )

    op.create_table(
        "storage_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("label", sa.String(length=512)),
        sa.Column("root_folder_id", sa.String(length=256), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("detail_code", sa.String(length=64)),
        sa.Column("quota_limit_bytes", sa.BigInteger()),
        sa.Column("quota_usage_bytes", sa.BigInteger()),
        sa.Column("quota_trash_bytes", sa.BigInteger()),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("last_checked_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("email", name="uq_storage_accounts_email"),
    )
    op.create_index(
        "ix_storage_accounts_enabled_priority",
        "storage_accounts",
        ["enabled", "priority"],
    )

    op.create_table(
        "storage_secrets",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("storage_accounts.id", ondelete="CASCADE"),
        ),
        sa.Column("ciphertext", sa.Text(), nullable=False),
        sa.Column("nonce", sa.String(length=64), nullable=False),
        sa.Column("key_id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("validated_at", sa.DateTime()),
        sa.UniqueConstraint("name", name="uq_storage_secrets_name"),
    )
    op.create_index(
        "ix_storage_secrets_account_id", "storage_secrets", ["account_id"]
    )

    op.create_table(
        "storage_oauth_states",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "secret_id",
            sa.Integer(),
            sa.ForeignKey("storage_secrets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("consumed_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("state_hash", name="uq_storage_oauth_states_state_hash"),
    )
    op.create_index(
        "ix_storage_oauth_states_expires_at",
        "storage_oauth_states",
        ["expires_at"],
    )

    op.create_table(
        "drive_file_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "file_id",
            sa.Integer(),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.Integer(),
            sa.ForeignKey("storage_accounts.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("remote_file_id", sa.String(length=256), nullable=False),
        sa.Column("remote_name", sa.String(length=1024), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha1", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(
            "account_id",
            "remote_file_id",
            name="uq_drive_location_account_remote",
        ),
        sa.UniqueConstraint(
            "file_id", "account_id", name="uq_drive_location_file_account"
        ),
    )
    op.create_index(
        "ix_drive_file_locations_file_id", "drive_file_locations", ["file_id"]
    )
    op.create_index(
        "ix_drive_file_locations_account_id",
        "drive_file_locations",
        ["account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_drive_file_locations_account_id", table_name="drive_file_locations"
    )
    op.drop_index("ix_drive_file_locations_file_id", table_name="drive_file_locations")
    op.drop_table("drive_file_locations")
    op.drop_index("ix_storage_oauth_states_expires_at", table_name="storage_oauth_states")
    op.drop_table("storage_oauth_states")
    op.drop_index("ix_storage_secrets_account_id", table_name="storage_secrets")
    op.drop_table("storage_secrets")
    op.drop_index(
        "ix_storage_accounts_enabled_priority", table_name="storage_accounts"
    )
    op.drop_table("storage_accounts")
    op.drop_index("uq_jobs_active_storage_migration", table_name="jobs")
    op.alter_column("files", "path", existing_type=sa.Text(), nullable=False)
