"""Serialize active Stage 4 matching jobs.

Revision ID: 0003_stage4_matching
Revises: 0002_stage3_playlists
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_stage4_matching"
down_revision = "0002_stage3_playlists"
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index("uq_jobs_active_matching", table_name="jobs")
