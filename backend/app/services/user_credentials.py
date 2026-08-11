"""Encrypted playlist-provider credentials scoped to one application user."""

from __future__ import annotations

import base64
import json
import os
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import UserProvider, UserProviderCredential, utcnow
from app.services.credentials import (
    CredentialError,
    CredentialIntegrityError,
    CredentialKeyError,
    key_id,
    load_key,
)

SUPPORTED_USER_PROVIDERS = frozenset({"spotify", "yandex"})


def _provider(value: str | UserProvider) -> str:
    normalized = value.value if isinstance(value, UserProvider) else str(value or "")
    normalized = normalized.strip().casefold()
    if normalized not in SUPPORTED_USER_PROVIDERS:
        raise CredentialError("Unsupported user credential provider")
    return normalized


def _aad(user_id: int, provider: str, version: int) -> bytes:
    return (
        f"music-service:user-provider-credential:v1:{user_id}:{provider}:{version}"
    ).encode()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    try:
        raw = value.encode()
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception as exc:
        raise CredentialIntegrityError("Credential envelope is invalid") from exc


def get_user_credential_record(
    db: Session, user_id: int, provider: str | UserProvider
) -> UserProviderCredential | None:
    provider_name = _provider(provider)
    return db.scalar(
        select(UserProviderCredential).where(
            UserProviderCredential.user_id == user_id,
            UserProviderCredential.provider == UserProvider(provider_name),
        )
    )


def has_user_credential(
    db: Session, user_id: int, provider: str | UserProvider
) -> bool:
    return get_user_credential_record(db, user_id, provider) is not None


def get_user_credential_payload(
    db: Session, user_id: int, provider: str | UserProvider
) -> dict[str, Any]:
    record = get_user_credential_record(db, user_id, provider)
    if record is None:
        raise CredentialError("User provider credential is not configured")
    provider_name = _provider(provider)
    key = load_key(settings.provider_credential_key_file)
    if record.key_id != key_id(key):
        raise CredentialKeyError("Credential was encrypted with another key")
    try:
        plaintext = AESGCM(key).decrypt(
            _decode(record.nonce),
            _decode(record.ciphertext),
            _aad(user_id, provider_name, int(record.version)),
        )
        payload = json.loads(plaintext)
    except CredentialError:
        raise
    except Exception as exc:
        raise CredentialIntegrityError("Credential integrity validation failed") from exc
    if not isinstance(payload, dict):
        raise CredentialIntegrityError("Credential payload is invalid")
    return payload


def save_user_credential(
    db: Session,
    user_id: int,
    provider: str | UserProvider,
    payload: Mapping[str, Any],
    *,
    expires_at=None,
    validated_at=None,
) -> UserProviderCredential:
    provider_name = _provider(provider)
    record = get_user_credential_record(db, user_id, provider_name)
    version = int(record.version) + 1 if record is not None else 1
    key = load_key(settings.provider_credential_key_file)
    nonce = os.urandom(12)
    plaintext = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    ciphertext = AESGCM(key).encrypt(
        nonce,
        plaintext,
        _aad(user_id, provider_name, version),
    )
    now = validated_at or utcnow()
    if record is None:
        record = UserProviderCredential(
            user_id=user_id,
            provider=UserProvider(provider_name),
            created_at=now,
        )
        db.add(record)
    record.ciphertext = _encode(ciphertext)
    record.nonce = _encode(nonce)
    record.key_id = key_id(key)
    record.version = version
    record.expires_at = expires_at
    record.updated_at = now
    record.validated_at = now
    db.flush()
    return record
