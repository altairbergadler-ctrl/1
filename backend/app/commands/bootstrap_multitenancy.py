"""Idempotent, fail-closed ownership and credential backfill."""

from __future__ import annotations

import json

from sqlalchemy import func, inspect, select, text

from app.db import SessionLocal
from app.models import (
    Job,
    JobScope,
    Playlist,
    PlaylistSource,
    ProviderCredential,
    StorageOAuthState,
    User,
    UserRole,
    UserState,
    utcnow,
)
from app.services.credentials import (
    credential_envelope,
    decrypt_envelope,
)
from app.services.user_credentials import (
    get_user_credential_payload,
    get_user_credential_record,
    save_user_credential,
)

SYSTEM_JOB_TYPES = frozenset(
    {
        "provider_health_check",
        "storage_health_check",
        "storage_migration",
        "scan_library",
    }
)
USER_JOB_TYPES = frozenset(
    {
        "import_playlists",
        "run_matching",
        "qobuz_download",
        "yandex_download",
    }
)


def _create_owner(db) -> User:
    columns = {item["name"] for item in inspect(db.get_bind()).get_columns("users")}
    now = utcnow()
    if {"login", "token"}.issubset(columns):
        result = db.execute(
            text(
                "INSERT INTO users "
                "(login, token, email, email_key, google_sub, display_name, role, "
                "state, is_bootstrap_owner, created_at) "
                "VALUES (:login, :token, NULL, NULL, NULL, NULL, :role, :state, "
                ":bootstrap, :created_at) RETURNING id"
            ),
            {
                "login": "bootstrap-owner-disabled",
                "token": "disabled-not-an-authentication-token",
                "role": UserRole.owner.value,
                "state": UserState.pending.value,
                "bootstrap": True,
                "created_at": now,
            },
        )
        owner_id = int(result.scalar_one())
        db.flush()
        return db.get(User, owner_id)
    owner = User(
        role=UserRole.owner,
        state=UserState.pending,
        is_bootstrap_owner=True,
        created_at=now,
    )
    db.add(owner)
    db.flush()
    return owner


def _owner(db) -> User:
    users = list(db.scalars(select(User).order_by(User.id)))
    bootstrap = [user for user in users if user.is_bootstrap_owner]
    if not users:
        return _create_owner(db)
    if len(users) != 1 or len(bootstrap) != 1:
        raise RuntimeError("Refusing ambiguous bootstrap user migration")
    owner = bootstrap[0]
    if owner.role != UserRole.owner:
        raise RuntimeError("Bootstrap user is not an owner")
    return owner


def _payload_playlist_id(payload: str | None) -> int | None:
    try:
        parsed = json.loads(payload or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get("playlist_id")
    if value is None and isinstance(parsed.get("downloads"), dict):
        value = parsed["downloads"].get("playlist_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _migrate_user_credential(db, owner: User, provider: str, *, remove_global: bool) -> None:
    global_record = db.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == provider)
    )
    user_record = get_user_credential_record(db, owner.id, provider)
    if global_record is None:
        if user_record is None:
            return
        get_user_credential_payload(db, owner.id, provider)
        return
    payload = decrypt_envelope(
        credential_envelope(global_record), expected_provider=provider
    )
    if remove_global:
        backup = db.execute(
            text(
                "SELECT provider, ciphertext, nonce, key_id, version, created_at, "
                "updated_at, validated_at FROM multitenancy_migration_credentials "
                "WHERE provider = :provider"
            ),
            {"provider": provider},
        ).mappings().one_or_none()
        expected = {
            "provider": global_record.provider,
            "ciphertext": global_record.ciphertext,
            "nonce": global_record.nonce,
            "key_id": global_record.key_id,
            "version": global_record.version,
            "created_at": global_record.created_at,
            "updated_at": global_record.updated_at,
            "validated_at": global_record.validated_at,
        }
        if backup is None:
            db.execute(
                text(
                    "INSERT INTO multitenancy_migration_credentials "
                    "(provider, ciphertext, nonce, key_id, version, created_at, "
                    "updated_at, validated_at) VALUES "
                    "(:provider, :ciphertext, :nonce, :key_id, :version, :created_at, "
                    ":updated_at, :validated_at)"
                ),
                expected,
            )
        elif any(
            backup[key] != expected[key]
            for key in ("provider", "ciphertext", "nonce", "key_id", "version")
        ):
            raise RuntimeError(f"Refusing conflicting {provider} rollback backup")
    if user_record is None:
        save_user_credential(
            db,
            owner.id,
            provider,
            payload,
            validated_at=global_record.validated_at,
        )
    elif get_user_credential_payload(db, owner.id, provider) != payload:
        raise RuntimeError(f"Refusing conflicting {provider} credential migration")
    if remove_global:
        db.delete(global_record)


def migrate() -> dict[str, int]:
    db = SessionLocal()
    try:
        if db.get_bind().dialect.name == "postgresql":
            db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": 2026081201})
        owner = _owner(db)

        source_columns = {
            item["name"]
            for item in inspect(db.get_bind()).get_columns("playlist_sources")
        }
        if {"access_token", "refresh_token"}.issubset(source_columns):
            legacy = db.scalar(
                text(
                    "SELECT count(*) FROM playlist_sources "
                    "WHERE access_token IS NOT NULL OR refresh_token IS NOT NULL"
                )
            )
            if legacy:
                raise RuntimeError("Legacy source credentials must be migrated first")

        sources = list(db.scalars(select(PlaylistSource)))
        for source in sources:
            if source.user_id not in (None, owner.id):
                raise RuntimeError("Playlist source already belongs to another user")
            source.user_id = owner.id

        playlists = list(db.scalars(select(Playlist)))
        for playlist in playlists:
            if playlist.user_id not in (None, owner.id):
                raise RuntimeError("Playlist already belongs to another user")
            playlist.user_id = owner.id

        jobs = list(db.scalars(select(Job)))
        for job in jobs:
            if job.type in SYSTEM_JOB_TYPES:
                job.scope = JobScope.system
                job.user_id = None
            elif job.type in USER_JOB_TYPES:
                if job.user_id not in (None, owner.id):
                    raise RuntimeError("Job already belongs to another user")
                job.scope = JobScope.user
                job.user_id = owner.id
                playlist_id = _payload_playlist_id(job.payload)
                if playlist_id is not None:
                    playlist = db.get(Playlist, playlist_id)
                    if playlist is not None and playlist.user_id == owner.id:
                        job.playlist_id = playlist_id
            else:
                raise RuntimeError(f"Unknown job type: {job.type}")

        for oauth_state in db.scalars(select(StorageOAuthState)):
            oauth_state.initiated_by_user_id = owner.id

        _migrate_user_credential(db, owner, "spotify", remove_global=True)
        _migrate_user_credential(db, owner, "yandex", remove_global=False)
        db.flush()

        if any(source.user_id is None for source in sources):
            raise RuntimeError("Playlist source ownership backfill is incomplete")
        if any(playlist.user_id is None for playlist in playlists):
            raise RuntimeError("Playlist ownership backfill is incomplete")
        if any(job.scope is None for job in jobs):
            raise RuntimeError("Job ownership backfill is incomplete")
        db.commit()
        return {
            "users": int(db.scalar(select(func.count(User.id))) or 0),
            "sources": len(sources),
            "playlists": len(playlists),
            "jobs": len(jobs),
            "user_credentials": 2
            - int(get_user_credential_record(db, owner.id, "spotify") is None)
            - int(get_user_credential_record(db, owner.id, "yandex") is None),
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps(migrate(), separators=(",", ":"), sort_keys=True))
