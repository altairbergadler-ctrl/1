"""Persist provider-specific track lookup outcomes.

Revision ID: 0004_provider_attempts
Revises: 0003_stage4_matching
Create Date: 2026-08-10
"""

import hashlib
import json
import re

from alembic import op
import sqlalchemy as sa


revision = "0004_provider_attempts"
down_revision = "0003_stage4_matching"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "provider_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("lookup_key", sa.String(length=64), nullable=False),
        sa.Column("playlist_item_id", sa.Integer(), nullable=True),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("provider_item_id", sa.String(length=128), nullable=True),
        sa.Column("selection_method", sa.String(length=32), nullable=True),
        sa.Column("error_code", sa.String(length=128), nullable=True),
        sa.Column("attempted_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["playlist_item_id"], ["playlist_items.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "provider",
            "lookup_key",
            name="uq_provider_attempts_provider_lookup",
        ),
    )
    op.create_index(
        "ix_provider_attempts_playlist_item",
        "provider_attempts",
        ["playlist_item_id"],
        unique=False,
    )
    _backfill_qobuz_attempts()


def _lookup_key(item: dict) -> str:
    candidate = re.sub(r"[^A-Za-z0-9]", "", item.get("isrc") or "").upper()
    isrc = candidate if len(candidate) == 12 else ""
    if isrc:
        identity = ("isrc", isrc)
    else:
        identity = (
            "metadata",
            item.get("artist_norm") or "",
            item.get("title_norm") or "",
            item.get("album_norm") or "",
            str(item.get("duration_ms") or ""),
        )
    return hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()


def _backfill_qobuz_attempts() -> None:
    bind = op.get_bind()
    jobs = bind.execute(
        sa.text(
            "SELECT id, status, payload, created_at, finished_at "
            "FROM jobs WHERE type = 'qobuz_download' "
            "ORDER BY created_at DESC, id DESC"
        )
    ).mappings()
    seen: set[str] = set()
    terminal = {"downloaded", "stored", "conflict", "not_found", "ambiguous", "failed"}
    for job in jobs:
        try:
            payload = json.loads(job["payload"] or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        downloads = payload.get("downloads") if isinstance(payload, dict) else None
        if not isinstance(downloads, dict):
            continue
        for entry in downloads.get("items") or []:
            if not isinstance(entry, dict) or entry.get("status") not in terminal:
                continue
            item_id = entry.get("item_id")
            item = bind.execute(
                sa.text(
                    "SELECT id, artist_norm, title_norm, album_norm, isrc, duration_ms "
                    "FROM playlist_items WHERE id = :item_id"
                ),
                {"item_id": item_id},
            ).mappings().first()
            if item is None:
                continue
            lookup_key = _lookup_key(dict(item))
            if lookup_key in seen:
                continue
            seen.add(lookup_key)
            outcome = entry.get("status")
            if outcome == "downloaded" and job["status"] == "done":
                outcome = "stored"
            attempted_at = job["finished_at"] or job["created_at"]
            bind.execute(
                sa.text(
                    "INSERT INTO provider_attempts "
                    "(provider, lookup_key, playlist_item_id, job_id, status, "
                    "provider_item_id, selection_method, error_code, attempted_at, updated_at) "
                    "VALUES (:provider, :lookup_key, :playlist_item_id, :job_id, :status, "
                    ":provider_item_id, :selection_method, :error_code, :attempted_at, :updated_at)"
                ),
                {
                    "provider": "qobuz",
                    "lookup_key": lookup_key,
                    "playlist_item_id": item["id"],
                    "job_id": job["id"],
                    "status": outcome,
                    "provider_item_id": entry.get("qobuz_track_id"),
                    "selection_method": entry.get("selection"),
                    "error_code": entry.get("error"),
                    "attempted_at": attempted_at,
                    "updated_at": attempted_at,
                },
            )


def downgrade() -> None:
    op.drop_index(
        "ix_provider_attempts_playlist_item", table_name="provider_attempts"
    )
    op.drop_table("provider_attempts")
