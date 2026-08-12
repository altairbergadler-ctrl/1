"""Lossless expand/backfill/contract migration and rollback tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, text


def _run_backend(database_url: str, *command: str) -> subprocess.CompletedProcess:
    backend = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, *command],
        cwd=backend,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _run_backend_unchecked(
    database_url: str, *command: str
) -> subprocess.CompletedProcess:
    backend = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    environment["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, *command],
        cwd=backend,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


def _create_legacy_schema(engine) -> None:
    statements = [
        "CREATE TABLE alembic_version (version_num VARCHAR(64) NOT NULL PRIMARY KEY)",
        "INSERT INTO alembic_version (version_num) VALUES ('0007_manual_playlist_source')",
        "CREATE TABLE users (id INTEGER PRIMARY KEY, login VARCHAR(128) NOT NULL, "
        "token VARCHAR(256) NOT NULL, CONSTRAINT uq_users_login UNIQUE (login))",
        "CREATE TABLE playlist_sources (id INTEGER PRIMARY KEY, service VARCHAR(16) NOT NULL, "
        "access_token TEXT, refresh_token TEXT, expires_at DATETIME, "
        "CONSTRAINT uq_playlist_sources_service UNIQUE (service))",
        "CREATE TABLE playlists (id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, "
        "external_id VARCHAR(256) NOT NULL, name VARCHAR(512) NOT NULL, "
        "snapshot_hash VARCHAR(256), track_count INTEGER, updated_at DATETIME, "
        "CONSTRAINT uq_playlists_source_external UNIQUE (source_id, external_id), "
        "FOREIGN KEY(source_id) REFERENCES playlist_sources(id))",
        "CREATE TABLE playlist_items (id INTEGER PRIMARY KEY, playlist_id INTEGER NOT NULL, "
        "position INTEGER NOT NULL, artist_raw VARCHAR(512), title_raw VARCHAR(512), "
        "CONSTRAINT uq_playlist_items_playlist_position UNIQUE (playlist_id, position), "
        "FOREIGN KEY(playlist_id) REFERENCES playlists(id))",
        "CREATE TABLE jobs (id INTEGER PRIMARY KEY, type VARCHAR(64) NOT NULL, "
        "source_id INTEGER, status VARCHAR(16), payload TEXT, error TEXT, "
        "created_at DATETIME, heartbeat_at DATETIME NOT NULL, lock_owner VARCHAR(64), "
        "finished_at DATETIME, FOREIGN KEY(source_id) REFERENCES playlist_sources(id))",
        "CREATE UNIQUE INDEX uq_jobs_active_matching ON jobs(type) "
        "WHERE type = 'run_matching' AND status IN ('pending', 'running')",
        "CREATE TABLE provider_credentials (id INTEGER PRIMARY KEY, provider VARCHAR(32) NOT NULL, "
        "ciphertext TEXT NOT NULL, nonce VARCHAR(64) NOT NULL, key_id VARCHAR(64) NOT NULL, "
        "version INTEGER NOT NULL, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, "
        "validated_at DATETIME NOT NULL, CONSTRAINT uq_provider_credentials_provider UNIQUE(provider))",
        "CREATE TABLE storage_secrets (id INTEGER PRIMARY KEY)",
        "CREATE TABLE storage_oauth_states (id INTEGER PRIMARY KEY, state_hash VARCHAR(64) NOT NULL, "
        "secret_id INTEGER NOT NULL, expires_at DATETIME NOT NULL, consumed_at DATETIME, "
        "created_at DATETIME NOT NULL, CONSTRAINT uq_storage_oauth_states_state_hash UNIQUE(state_hash), "
        "FOREIGN KEY(secret_id) REFERENCES storage_secrets(id))",
        "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, sha1 VARCHAR(40) NOT NULL UNIQUE)",
    ]
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def test_expand_backfill_contract_is_idempotent_and_losslessly_reversible(tmp_path):
    # Fixed IDs and SHA-1 values prove that ownership backfill changes rights,
    # not the existing catalog rows or physical-file identity.
    database = (tmp_path / "legacy.sqlite3").resolve()
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    engine = create_engine(database_url)
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO playlist_sources "
                "(id, service, access_token, refresh_token) "
                "VALUES (11, 'spotify', 'synthetic-access', 'synthetic-refresh')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO playlists "
                "(id, source_id, external_id, name, track_count) "
                "VALUES (21, 11, 'legacy', 'Legacy', 1)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO playlist_items "
                "(id, playlist_id, position, artist_raw, title_raw) "
                "VALUES (31, 21, 0, 'Artist', 'Track')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO files "
                "(id, path, sha1) "
                "VALUES (44, '/library/legacy.flac', :sha1)"
            ),
            {"sha1": "a" * 40},
        )
        connection.execute(
                text(
                    "INSERT INTO jobs "
                    "(id, type, source_id, status, payload, heartbeat_at) "
                    "VALUES (51, 'import_playlists', 11, 'done', "
                    ":payload, CURRENT_TIMESTAMP)"
                ),
                {"payload": '{"source_id":11,"playlist_id":21}'},
            )

    first = _run_backend(database_url, "-m", "app.commands.migrate_database")
    second = _run_backend(database_url, "-m", "app.commands.migrate_database")
    assert '"status":"migrated"' in first.stdout
    assert '"status":"already_current"' in second.stdout

    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0010_open_subsonic_players"
        assert connection.scalar(text("SELECT count(*) FROM player_credentials")) == 0
        assert connection.scalar(
            text("SELECT count(*) FROM playlists WHERE opensubsonic_id IS NULL")
        ) == 0
        owner_id = connection.scalar(text("SELECT id FROM users WHERE is_bootstrap_owner = 1"))
        assert owner_id is not None
        assert connection.scalar(text("SELECT user_id FROM playlist_sources WHERE id = 11")) == owner_id
        assert connection.scalar(text("SELECT user_id FROM playlists WHERE id = 21")) == owner_id
        assert connection.scalar(text("SELECT user_id FROM jobs WHERE id = 51")) == owner_id
        assert connection.scalar(text("SELECT scope FROM jobs WHERE id = 51")) == "user"
        assert connection.scalar(text("SELECT count(*) FROM user_provider_credentials")) == 1
        assert connection.scalar(text("SELECT count(*) FROM provider_credentials WHERE provider = 'spotify'")) == 0
        assert connection.scalar(text("SELECT count(*) FROM multitenancy_migration_credentials")) == 1
        assert connection.scalar(text("SELECT sha1 FROM files WHERE id = 44")) == "a" * 40

    _run_backend(
        database_url,
        "-m",
        "alembic",
        "downgrade",
        "0007_manual_playlist_source",
    )
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0007_manual_playlist_source"
        assert connection.scalar(text("SELECT count(*) FROM users")) == 0
        assert connection.scalar(text("SELECT count(*) FROM playlist_sources WHERE id = 11")) == 1
        assert connection.scalar(text("SELECT count(*) FROM playlists WHERE id = 21")) == 1
        assert connection.scalar(text("SELECT count(*) FROM playlist_items WHERE id = 31")) == 1
        assert connection.scalar(text("SELECT count(*) FROM jobs WHERE id = 51")) == 1
        assert connection.scalar(text("SELECT count(*) FROM provider_credentials WHERE provider = 'spotify'")) == 1
        assert connection.scalar(text("SELECT sha1 FROM files WHERE id = 44")) == "a" * 40
    engine.dispose()


def test_direct_contract_refuses_to_drop_legacy_source_credentials(tmp_path):
    database = (tmp_path / "direct-contract.sqlite3").resolve()
    database_url = f"sqlite+pysqlite:///{database.as_posix()}"
    engine = create_engine(database_url)
    _create_legacy_schema(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO playlist_sources "
                "(id, service, access_token, refresh_token) "
                "VALUES (11, 'spotify', 'synthetic-access', 'synthetic-refresh')"
            )
        )

    _run_backend(database_url, "-m", "alembic", "upgrade", "0008_google_user_auth_expand")
    contract = _run_backend_unchecked(
        database_url,
        "-m",
        "alembic",
        "upgrade",
        "0009_google_user_auth_contract",
    )

    assert contract.returncode != 0
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "0008_google_user_auth_expand"
        assert connection.scalar(
            text("SELECT count(*) FROM playlist_sources WHERE access_token IS NOT NULL")
        ) == 1
    engine.dispose()
