"""Add Stage 3 playlist import constraints.

Revision ID: 0002_stage3_playlists
Revises: 0001_initial
Create Date: 2026-08-08
"""

from alembic import op
import sqlalchemy as sa


revision = "0002_stage3_playlists"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.create_unique_constraint(
            "uq_playlist_sources_service",
            ["service"],
        )
    with op.batch_alter_table("playlist_items") as batch_op:
        batch_op.create_unique_constraint(
            "uq_playlist_items_playlist_position",
            ["playlist_id", "position"],
        )
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.add_column(sa.Column("source_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            "fk_jobs_source_id_playlist_sources",
            "playlist_sources",
            ["source_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "uq_jobs_active_import_source",
        "jobs",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text(
            "type = 'import_playlists' AND status IN ('pending', 'running')"
        ),
        sqlite_where=sa.text(
            "type = 'import_playlists' AND status IN ('pending', 'running')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_jobs_active_import_source", table_name="jobs")
    with op.batch_alter_table("jobs") as batch_op:
        batch_op.drop_constraint(
            "fk_jobs_source_id_playlist_sources",
            type_="foreignkey",
        )
        batch_op.drop_column("source_id")
    with op.batch_alter_table("playlist_items") as batch_op:
        batch_op.drop_constraint(
            "uq_playlist_items_playlist_position",
            type_="unique",
        )
    with op.batch_alter_table("playlist_sources") as batch_op:
        batch_op.drop_constraint(
            "uq_playlist_sources_service",
            type_="unique",
        )
