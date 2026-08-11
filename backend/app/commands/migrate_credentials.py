"""One-time fail-closed migration of legacy PlaylistSource token columns."""

from __future__ import annotations

import json

from sqlalchemy import inspect, text

from app.db import SessionLocal
from app.models import ProviderCredential, utcnow
from app.services.credentials import encrypt_payload


def migrate() -> int:
    db = SessionLocal()
    migrated = 0
    try:
        columns = {
            item["name"] for item in inspect(db.get_bind()).get_columns("playlist_sources")
        }
        if not {"access_token", "refresh_token"}.issubset(columns):
            return 0
        rows = db.execute(
            text(
                "SELECT id, service, access_token, refresh_token "
                "FROM playlist_sources WHERE access_token IS NOT NULL "
                "OR refresh_token IS NOT NULL"
            )
        ).mappings()
        for row in rows:
            provider = str(row["service"])
            access = str(row["access_token"] or "").strip()
            refresh = str(row["refresh_token"] or "").strip()
            if not access and not refresh:
                continue
            existing = db.query(ProviderCredential).filter_by(provider=provider).one_or_none()
            if existing is not None:
                raise RuntimeError(
                    f"Refusing to overwrite an existing encrypted {provider} credential"
                )
            payload = (
                {"token": access}
                if provider == "yandex"
                else {"access_token": access, "refresh_token": refresh or None}
            )
            envelope = encrypt_payload(provider, payload, 1)
            now = utcnow()
            db.add(
                ProviderCredential(
                    provider=provider,
                    ciphertext=envelope["ciphertext"],
                    nonce=envelope["nonce"],
                    key_id=envelope["key_id"],
                    version=1,
                    created_at=now,
                    updated_at=now,
                    validated_at=now,
                )
            )
            db.execute(
                text(
                    "UPDATE playlist_sources SET access_token = NULL, "
                    "refresh_token = NULL WHERE id = :source_id"
                ),
                {"source_id": row["id"]},
            )
            migrated += 1
        db.commit()
        return migrated
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    # Print only a count. Credential values and encrypted blobs are never emitted.
    print(json.dumps({"migrated": migrate()}, separators=(",", ":")))
