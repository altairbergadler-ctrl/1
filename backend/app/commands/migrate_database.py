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


def _config() -> Config:
    return Config("alembic.ini")


def _current_revision() -> str | None:
    with engine.connect() as connection:
        return MigrationContext.configure(connection).get_current_revision()


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

    if current != EXPAND_REVISION:
        command.upgrade(config, EXPAND_REVISION)
    if _current_revision() != EXPAND_REVISION:
        raise RuntimeError("Database did not stop at the ownership expand revision")

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
