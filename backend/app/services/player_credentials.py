"""Revocable OpenSubsonic API keys backed by the web authentication key."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import uuid
from datetime import timedelta

from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, update
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import PlayerCredential, UserState, utcnow
from app.services.authentication import keyed_digest
from app.services.credentials import key_id, load_key


class PlayerCredentialError(RuntimeError):
    pass


class PlayerRateLimitUnavailable(PlayerCredentialError):
    pass


_redis: Redis | None = None
_test_limits: dict[str, tuple[int, float]] = {}


def _key_id() -> str:
    return key_id(load_key(settings.auth_key_file))


def _digest(raw_key: str) -> bytes:
    return keyed_digest("opensubsonic-api-key-v1", raw_key)


def create_player_credential(
    db: Session, user_id: int, label: str
) -> tuple[str, PlayerCredential]:
    clean_label = " ".join(label.split())
    if not clean_label or len(clean_label) > 128:
        raise PlayerCredentialError("Credential label is invalid")
    handle = secrets.token_urlsafe(12)
    secret = secrets.token_urlsafe(32)
    raw = f"afk1.{handle}.{secret}"
    record = PlayerCredential(
        id=str(uuid.uuid4()),
        user_id=user_id,
        label=clean_label,
        public_handle=handle,
        secret_hash=_digest(raw),
        auth_scheme="api_key_v1",
        key_id=_key_id(),
        created_at=utcnow(),
    )
    db.add(record)
    db.flush()
    return raw, record


def revoke_player_credential(db: Session, credential_id: str, user_id: int) -> bool:
    result = db.execute(
        update(PlayerCredential)
        .where(
            PlayerCredential.id == credential_id,
            PlayerCredential.user_id == user_id,
            PlayerCredential.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return bool(result.rowcount)


def revoke_all_player_credentials(db: Session, user_id: int) -> int:
    result = db.execute(
        update(PlayerCredential)
        .where(
            PlayerCredential.user_id == user_id,
            PlayerCredential.revoked_at.is_(None),
        )
        .values(revoked_at=utcnow())
    )
    return int(result.rowcount or 0)


def _counter(key: str, limit: int, window: int) -> bool:
    global _redis
    if settings.database_url.startswith("sqlite"):
        import time

        now = time.monotonic()
        count, deadline = _test_limits.get(key, (0, now + window))
        if deadline <= now:
            count, deadline = 0, now + window
        count += 1
        _test_limits[key] = (count, deadline)
        return count <= limit
    try:
        _redis = _redis or Redis.from_url(settings.redis_url, decode_responses=True)
        with _redis.pipeline() as pipe:
            pipe.incr(key)
            pipe.expire(key, window, nx=True)
            count, _ = pipe.execute()
        return int(count) <= limit
    except RedisError as exc:
        raise PlayerRateLimitUnavailable("Player authentication is unavailable") from exc


def allow_failed_attempt(client_ip: str, handle: str) -> bool:
    marker = hashlib.sha256(handle.encode()).hexdigest()[:24]
    window = settings.opensubsonic_failed_window_seconds
    limit = settings.opensubsonic_failed_attempts
    return _counter(f"opensubsonic:fail:ip:{client_ip}", limit, window) and _counter(
        f"opensubsonic:fail:key:{marker}", limit, window
    )


def allow_success(credential_id: str) -> bool:
    return _counter(
        f"opensubsonic:ok:{credential_id}",
        settings.opensubsonic_success_requests_per_minute,
        60,
    )


def authenticate_player_key(
    db: Session, raw_key: str, *, client_ip: str
) -> PlayerCredential | None:
    bounded = raw_key if 1 <= len(raw_key or "") <= 256 else ""
    parts = bounded.split(".") if bounded else []
    handle = parts[1] if len(parts) == 3 and parts[0] == "afk1" else "invalid"
    record = db.scalar(
        select(PlayerCredential)
        .options(joinedload(PlayerCredential.user))
        .where(PlayerCredential.public_handle == handle)
    )
    candidate = _digest(bounded or "invalid")
    valid_digest = record is not None and hmac.compare_digest(
        candidate, record.secret_hash
    )
    now = utcnow()
    valid = bool(
        valid_digest
        and record is not None
        and record.auth_scheme == "api_key_v1"
        and record.key_id == _key_id()
        and record.revoked_at is None
        and (record.expires_at is None or record.expires_at > now)
        and record.user.state == UserState.active
    )
    if not valid:
        if not allow_failed_attempt(client_ip, handle):
            raise PlayerCredentialError("Rate limit exceeded")
        return None
    if not allow_success(record.id):
        raise PlayerCredentialError("Rate limit exceeded")
    if record.last_used_at is None or record.last_used_at + timedelta(
        seconds=settings.opensubsonic_auth_touch_interval_seconds
    ) <= now:
        record.last_used_at = now
        db.commit()
    return record
