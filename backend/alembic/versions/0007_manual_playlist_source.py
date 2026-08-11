"""manual playlist source

Revision ID: 0007_manual_playlist_source
Revises: 0006_google_drive_storage
Create Date: 2026-08-11
"""

from alembic import op


revision = "0007_manual_playlist_source"
down_revision = "0006_google_drive_storage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE service_enum ADD VALUE IF NOT EXISTS 'manual'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be removed safely while rows may still use
    # them. Keeping the value is harmless if this application revision rolls
    # back; SQLite test databases build the enum from model metadata.
    pass
