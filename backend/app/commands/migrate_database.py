"""Run the expand/backfill/contract database migration safely and idempotently."""

from __future__ import annotations

import json

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.commands import bootstrap_multitenancy, migrate_credentials
from app.db import engine

EXPAND_REVISION = "0008_google_user_auth_expand"
CONTRACT_REVISION = "0009_google_user_auth_contract"


def _config() -> Config:
    return Config("alembic.ini")


def _current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


def _descends_from(
    scripts: ScriptDirectory, revision: str, ancestor: str
) -> bool:
    """Return whether revision is at or after ancestor in the revision graph."""
    pending = [revision]
    visited: set[str] = set()
    while pending:
        candidate = pending.pop()
        if candidate == ancestor:
            return True
        if candidate in visited:
            continue
        visited.add(candidate)
        script = scripts.get_revision(candidate)
        if script is None:
            continue
        down_revisions = script.down_revision
        if isinstance(down_revisions, tuple):
            pending.extend(down_revisions)
        elif down_revisions is not None:
            pending.append(down_revisions)
    return False


def migrate() -> dict[str, object]:
    config = _config()
    scripts = ScriptDirectory.from_config(config)
    expected_head = scripts.get_current_head()
    current = _current_revision()
    if current == expected_head:
        return {"revision": current, "status": "already_current"}

    known_revisions = {revision.revision for revision in scripts.walk_revisions()}
    if current is not None and current not in known_revisions:
        raise RuntimeError("Database revision is not part of this release")

    # Ownership backfill is complete once the contract revision has been
    # reached. Later additive releases must upgrade directly from that point;
    # attempting to downgrade back to the expand phase is both invalid and
    # unnecessary.
    if current is not None and _descends_from(
        scripts, current, CONTRACT_REVISION
    ):
        command.upgrade(config, "head")
        final_revision = _current_revision()
        if final_revision != expected_head:
            raise RuntimeError("Database did not reach the expected Alembic head")
        return {
            "revision": final_revision,
            "status": "migrated",
            "legacy_credentials": 0,
            "backfill": {"status": "not_required"},
        }

    if current != EXPAND_REVISION:
        command.upgrade(config, EXPAND_REVISION)
    if _current_revision() != EXPAND_REVISION:
        raise RuntimeError("Database did not stop at the ownership expand revision")

    # Keep the three phases explicit: schema expansion, data/credential
    # backfill, then NOT NULL constraints and removal of legacy columns.
    legacy_count = migrate_credentials.migrate()
    backfill = bootstrap_multitenancy.migrate()
    command.upgrade(config, "head")
    final_revision = _current_revision()
    if final_revision != expected_head:
        raise RuntimeError("Database did not reach the expected Alembic head")
    return {
        "revision": final_revision,
        "status": "migrated",
        "legacy_credentials": legacy_count,
        "backfill": backfill,
    }


if __name__ == "__main__":
    print(json.dumps(migrate(), separators=(",", ":"), sort_keys=True))
