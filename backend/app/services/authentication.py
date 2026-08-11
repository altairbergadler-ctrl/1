"""Server-side sessions and authentication cryptographic primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import unicodedata
import uuid
from datetime import timedelta

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import SessionKind, User, UserSession, UserState, utcnow
from app.services.credentials import CredentialKeyError, key_id, load_key


class AuthenticationError(RuntimeError):
    pass


def lock_identity_mutation(db: Session) -> None:
    """Serialize invitation and identity binding without logging PII conflicts."""

    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": 2026081202},
        )


def _auth_key() -> bytes:
    try:
        return load_key(settings.auth_key_file)
    except Exception as exc:
        raise CredentialKeyError("Authentication key is unavailable") from exc


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    try:
        raw = value.encode()
        return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except Exception as exc:
        raise AuthenticationError("Authentication envelope is invalid") from exc


def keyed_digest(purpose: str, value: str) -> bytes:
    return hmac.new(
        _auth_key(),
        purpose.encode() + b"\0" + value.encode(),
        hashlib.sha256,
    ).digest()


def token_hash(token: str) -> bytes:
    return keyed_digest("session-token-v1", token)


def csrf_token_for_session(token: str) -> str:
    return _b64(keyed_digest("csrf-token-v1", token))


def csrf_hash(token: str) -> bytes:
    return keyed_digest("csrf-hash-v1", token)


def normalize_email(value: str) -> tuple[str, str]:
    display = unicodedata.normalize("NFC", str(value or "").strip())
    if (
        not display
        or len(display) > 320
        or display.count("@") != 1
        or any(character.isspace() for character in display)
    ):
        raise AuthenticationError("Email address is invalid")
    local, domain = display.rsplit("@", 1)
    if not local or not domain or "." not in domain:
        raise AuthenticationError("Email address is invalid")
    key = unicodedata.normalize("NFC", display).casefold()
    return display, key


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def create_session(
    db: Session,
    user: User,
    *,
    kind: SessionKind = SessionKind.google,
) -> tuple[str, str, UserSession]:
    raw = new_opaque_token()
    csrf = csrf_token_for_session(raw)
    now = utcnow()
    ttl = (
        settings.auth_recovery_max_age_seconds
        if kind == SessionKind.recovery
        else settings.auth_cookie_max_age_seconds
    )
    record = UserSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        kind=kind,
        token_hash=token_hash(raw),
        csrf_hash=csrf_hash(csrf),
        created_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=ttl),
    )
    db.add(record)
    db.flush()
    return raw, csrf, record


def session_for_token(
    db: Session,
    raw_token: str,
    *,
    kind: SessionKind,
    touch: bool = True,
) -> UserSession | None:
    if not raw_token or len(raw_token) > 256:
        return None
    record = db.scalar(
        select(UserSession)
        .options(joinedload(UserSession.user))
        .where(
            UserSession.token_hash == token_hash(raw_token),
            UserSession.kind == kind,
        )
    )
    if record is None:
        return None
    now = utcnow()
    idle_seconds = (
        settings.auth_recovery_idle_seconds
        if kind == SessionKind.recovery
        else settings.auth_session_idle_seconds
    )
    if (
        record.revoked_at is not None
        or record.expires_at <= now
        or record.last_seen_at + timedelta(seconds=idle_seconds) <= now
        or (record.user.state != UserState.active and kind == SessionKind.google)
    ):
        return None
    if touch and record.last_seen_at + timedelta(
        seconds=settings.auth_session_touch_interval_seconds
    ) <= now:
        record.last_seen_at = now
        db.commit()
    return record


def revoke_session(db: Session, record: UserSession) -> None:
    if record.revoked_at is None:
        record.revoked_at = utcnow()
        db.flush()


def revoke_user_sessions(db: Session, user_id: int) -> int:
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


def seal_login_value(attempt_id: str, value: str) -> tuple[str, str, str]:
    key = _auth_key()
    nonce = os.urandom(12)
    ciphertext = AESGCM(key).encrypt(
        nonce,
        value.encode(),
        f"music-service:google-login-attempt:v1:{attempt_id}".encode(),
    )
    return _b64(ciphertext), _b64(nonce), key_id(key)


def open_login_value(
    attempt_id: str,
    ciphertext: str,
    nonce: str,
    expected_key_id: str,
) -> str:
    key = _auth_key()
    if key_id(key) != expected_key_id:
        raise AuthenticationError("Authentication state key changed")
    try:
        plaintext = AESGCM(key).decrypt(
            _unb64(nonce),
            _unb64(ciphertext),
            f"music-service:google-login-attempt:v1:{attempt_id}".encode(),
        )
        return plaintext.decode()
    except Exception as exc:
        raise AuthenticationError("Authentication state is invalid") from exc
