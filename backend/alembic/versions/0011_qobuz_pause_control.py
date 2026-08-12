"""add cooperative Qobuz pause control

Revision ID: 0011_qobuz_pause_control
Revises: 0010_open_subsonic_players
Create Date: 2026-08-12
"""

from alembic import op
import sqlalchemy as sa


revision = "0011_qobuz_pause_control"
down_revision = "0010_open_subsonic_players"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("pause_requested_at", sa.DateTime()))
        batch_op.add_column(sa.Column("paused_at", sa.DateTime()))


def downgrade() -> None:
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_column("paused_at")
        batch_op.drop_column("pause_requested_at")