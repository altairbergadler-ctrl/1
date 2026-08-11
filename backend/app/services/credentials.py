"""Encrypted provider credential storage.

Provider values are encrypted with AES-256-GCM.  The key is supplied through a
read-only Docker secret file and is never stored in the database.  Qobuz uses a
separate key because its untrusted downloader sidecar must decrypt only Qobuz
material and must not gain access to Yandex or Spotify credentials.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ProviderCredential, utcnow

SUPPORTED_PROVIDERS = frozenset({"qobuz", "yandex", "spotify"})


class CredentialError(RuntimeError):
    """Base class for fail-closed credential storage errors."""


class CredentialKeyError(CredentialError):
    pass


class CredentialIntegrityError(CredentialError):
    pass


def credential_key_path(provider: str) -> str:
    _validate_provider(provider)
    return (
        settings.qobuz_credential_key_file
        if provider == "qobuz"
        else settings.provider_credential_key_file
    )


def _validate_provider(provider: str) -> str:
    normalized = str(provider or "").strip().casefold()
    if normalized not in SUPPORTED_PROVIDERS:
        raise CredentialError("Unsupported credential provider")
    return normalized


def load_key(path: str | Path) -> bytes:
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise CredentialKeyError("Provider credential key is unavailable") from exc
    if len(data) == 32:
        return data
    raw = data.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise CredentialKeyError("Provider credential key is invalid") from exc
    if len(decoded) != 32:
        raise CredentialKeyError("Provider credential key must contain 32 bytes")
    return decoded


def key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def _aad(provider: str, version: int) -> bytes:
    return f"music-service:provider-credential:v1:{provider}:{version}".encode()


def encrypt_payload(
    provider: str,
    payload: Mapping[str, Any],
    version: int,
    *,
    key_path: str | Path | None = None,
) -> dict[str, Any]:
    provider = _validate_provider(provider)
    if version < 1:
        raise CredentialError("Credential version must be positive")
    key = load_key(key_path or credential_key_path(provider))
    nonce = os.urandom(12)
    plaintext = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _aad(provider, version))
    return {
        "schema": 1,
        "provider": provider,
        "version": version,
        "key_id": key_id(key),
        "nonce": base64.urlsafe_b64encode(nonce).decode().rstrip("="),
        "ciphertext": base64.urlsafe_b64encode(ciphertext).decode().rstrip("="),
    }


def decrypt_envelope(
    envelope: Mapping[str, Any],
    *,
    expected_provider: str | None = None,
    key_path: str | Path | None = None,
) -> dict[str, Any]:
    provider = _validate_provider(str(envelope.get("provider") or ""))
    if expected_provider is not None and provider != _validate_provider(expected_provider):
        raise CredentialIntegrityError("Credential provider does not match")
    try:
        schema = int(envelope.get("schema"))
        version = int(envelope.get("version"))
        nonce_text = str(envelope.get("nonce") or "")
        ciphertext_text = str(envelope.get("ciphertext") or "")
        nonce = base64.urlsafe_b64decode(
            nonce_text.encode() + b"=" * (-len(nonce_text) % 4)
        )
        ciphertext = base64.urlsafe_b64decode(
            ciphertext_text.encode() + b"=" * (-len(ciphertext_text) % 4)
        )
    except (binascii.Error, TypeError, ValueError) as exc:
        raise CredentialIntegrityError("Credential envelope is invalid") from exc
    if schema != 1 or version < 1:
        raise CredentialIntegrityError("Credential envelope version is invalid")
    key = load_key(key_path or credential_key_path(provider))
    if envelope.get("key_id") and str(envelope["key_id"]) != key_id(key):
        raise CredentialKeyError("Credential was encrypted with another key")
    try:
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, _aad(provider, version)
        )
        payload = json.loads(plaintext)
    except Exception as exc:
        raise CredentialIntegrityError("Credential integrity validation failed") from exc
    if not isinstance(payload, dict):
        raise CredentialIntegrityError("Credential payload is invalid")
    return payload


def credential_envelope(record: ProviderCredential) -> dict[str, Any]:
    return {
        "schema": 1,
        "provider": record.provider,
        "version": record.version,
        "key_id": record.key_id,
        "nonce": record.nonce,
        "ciphertext": record.ciphertext,
    }


def get_credential_record(db: Session, provider: str) -> ProviderCredential | None:
    provider = _validate_provider(provider)
    return db.scalar(
        select(ProviderCredential).where(ProviderCredential.provider == provider)
    )


def has_credential(db: Session, provider: str) -> bool:
    return get_credential_record(db, provider) is not None


def get_credential_payload(db: Session, provider: str) -> dict[str, Any]:
    record = get_credential_record(db, provider)
    if record is None:
        raise CredentialError("Provider credential is not configured")
    return decrypt_envelope(credential_envelope(record), expected_provider=provider)


def save_credential(
    db: Session,
    provider: str,
    payload: Mapping[str, Any],
    *,
    validated_at=None,
) -> ProviderCredential:
    provider = _validate_provider(provider)
    record = get_credential_record(db, provider)
    version = int(record.version) + 1 if record is not None else 1
    envelope = encrypt_payload(provider, payload, version)
    now = validated_at or utcnow()
    if record is None:
        record = ProviderCredential(
            provider=provider,
            created_at=now,
            validated_at=now,
        )
        db.add(record)
    record.ciphertext = envelope["ciphertext"]
    record.nonce = envelope["nonce"]
    record.key_id = envelope["key_id"]
    record.version = version
    record.updated_at = now
    record.validated_at = now
    db.flush()
    return record
