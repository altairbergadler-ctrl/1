"""Encrypted storage credentials using the existing external provider key."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import StorageSecret, utcnow
from app.services.credentials import key_id, load_key

_SECRET_NAME = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,255}$")


class StorageSecretError(RuntimeError):
    pass


class StorageSecretIntegrityError(StorageSecretError):
    pass


def _validate_name(name: str) -> str:
    normalized = str(name or "").strip().casefold()
    if not _SECRET_NAME.fullmatch(normalized):
        raise StorageSecretError("Invalid storage secret name")
    return normalized


def _aad(name: str, version: int) -> bytes:
    return f"music-service:storage-secret:v1:{name}:{version}".encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    raw = str(value or "").encode()
    try:
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise StorageSecretIntegrityError("Storage secret envelope is invalid") from exc


def get_storage_secret(db: Session, name: str) -> StorageSecret | None:
    name = _validate_name(name)
    return db.scalar(select(StorageSecret).where(StorageSecret.name == name))


def has_storage_secret(db: Session, name: str) -> bool:
    return get_storage_secret(db, name) is not None


def save_storage_secret(
    db: Session,
    name: str,
    payload: Mapping[str, Any],
    *,
    account_id: int | None = None,
    validated_at=None,
) -> StorageSecret:
    name = _validate_name(name)
    record = get_storage_secret(db, name)
    version = int(record.version) + 1 if record is not None else 1
    key = load_key(settings.provider_credential_key_file)
    nonce = os.urandom(12)
    plaintext = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(name, version))
    now = utcnow()
    if record is None:
        record = StorageSecret(name=name, created_at=now)
        db.add(record)
    record.account_id = account_id
    record.ciphertext = _encode(ciphertext)
    record.nonce = _encode(nonce)
    record.key_id = key_id(key)
    record.version = version
    record.updated_at = now
    record.validated_at = validated_at
    db.flush()
    return record


def read_storage_secret(db: Session, name: str) -> dict[str, Any]:
    record = get_storage_secret(db, name)
    if record is None:
        raise StorageSecretError("Storage secret is not configured")
    key = load_key(settings.provider_credential_key_file)
    if record.key_id != key_id(key):
        raise StorageSecretIntegrityError("Storage secret uses another key")
    try:
        plaintext = AESGCM(key).decrypt(
            _decode(record.nonce),
            _decode(record.ciphertext),
            _aad(record.name, int(record.version)),
        )
        payload = json.loads(plaintext)
    except StorageSecretIntegrityError:
        raise
    except Exception as exc:
        raise StorageSecretIntegrityError(
            "Storage secret integrity validation failed"
        ) from exc
    if not isinstance(payload, dict):
        raise StorageSecretIntegrityError("Storage secret payload is invalid")
    return payload


def delete_storage_secret(db: Session, name: str) -> None:
    record = get_storage_secret(db, name)
    if record is not None:
        db.delete(record)
        db.flush()
