"""add acquisition scheduling and encrypted browser push subscriptions

Revision ID: 0012_web_push_subscriptions
Revises: 0011_qobuz_pause_control
Create Date: 2026-08-13
"""

from alembic import op
import sqlalchemy as sa


revision = "0012_web_push_subscriptions"
down_revision = "0011_qobuz_pause_control"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("next_run_at", sa.DateTime(), nullable=True))
    op.create_table(
        "push_subscriptions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("secret_id", sa.Integer(), nullable=False),
        sa.Column("endpoint_hash", sa.LargeBinary(length=32), nullable=False),
        sa.Column("user_agent_label", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("last_success_at", sa.DateTime()),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime()),
        sa.ForeignKeyConstraint(["secret_id"], ["storage_secrets.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint_hash"),
        sa.UniqueConstraint("secret_id"),
    )
    op.create_index(
        "ix_push_subscriptions_user_active",
        "push_subscriptions",
        ["user_id", "revoked_at"],
    )
    op.create_index(
        "uq_jobs_active_acquisition_playlist",
        "jobs",
        ["playlist_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'acquisition_workflow' AND status IN ('pending', 'running')"
        ),
        sqlite_where=sa.text(
            "type = 'acquisition_workflow' AND status IN ('pending', 'running')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_acquisition_playlist", table_name="jobs")
    op.drop_index(
        "ix_push_subscriptions_user_active",
        table_name="push_subscriptions",
    )
    op.drop_table("push_subscriptions")
    op.drop_column("jobs", "next_run_at")
