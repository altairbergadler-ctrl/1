"""Encrypted browser subscriptions and best-effort completion notifications."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PushSubscription, User, new_public_id, utcnow
from app.services.storage_secrets import (
    StorageSecretError,
    delete_storage_secret,
    read_storage_secret,
    save_storage_secret,
)


class WebPushError(RuntimeError):
    pass


def _private_key_path() -> Path:
    path = Path(settings.web_push_vapid_private_key_file)
    try:
        resolved = path.expanduser().resolve(strict=True)
        if not resolved.is_file() or resolved.stat().st_size > 16 * 1024:
            raise WebPushError("Web Push private key is unavailable")
        return resolved
    except OSError as exc:
        raise WebPushError("Web Push private key is unavailable") from exc


def vapid_public_key() -> str:
    try:
        private_key = serialization.load_pem_private_key(
            _private_key_path().read_bytes(),
            password=None,
        )
    except (OSError, ValueError, TypeError) as exc:
        raise WebPushError("Web Push private key is invalid") from exc
    if not isinstance(private_key, ec.EllipticCurvePrivateKey) or not isinstance(
        private_key.curve, ec.SECP256R1
    ):
        raise WebPushError("Web Push private key must use P-256")
    public = private_key.public_key().public_numbers()
    raw = b"\x04" + public.x.to_bytes(32, "big") + public.y.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def web_push_configured() -> bool:
    if not settings.web_push_enabled:
        return False
    subject = str(settings.web_push_vapid_subject or "")
    if not subject.startswith(("mailto:", "https://")):
        return False
    try:
        vapid_public_key()
    except WebPushError:
        return False
    return True


def _validate_endpoint(value: str) -> str:
    endpoint = str(value or "").strip()
    parsed = urlsplit(endpoint)
    allowed_hosts = {
        item.strip().casefold()
        for item in settings.web_push_allowed_host_suffixes.split(",")
        if item.strip()
    }
    hostname = parsed.hostname.casefold() if parsed.hostname else ""
    host_allowed = any(
        hostname == item or hostname.endswith("." + item)
        for item in allowed_hosts
    )
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebPushError("Invalid Web Push endpoint") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or not host_allowed
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or len(endpoint) > 4096
    ):
        raise WebPushError("Invalid Web Push endpoint")
    return endpoint


def endpoint_hash(endpoint: str) -> bytes:
    return hashlib.sha256(_validate_endpoint(endpoint).encode()).digest()


def subscription_payload(payload) -> dict:
    endpoint = _validate_endpoint(payload.endpoint)
    return {
        "endpoint": endpoint,
        "expirationTime": payload.expirationTime,
        "keys": {
            "p256dh": payload.keys.p256dh,
            "auth": payload.keys.auth,
        },
    }


def save_subscription(db: Session, user: User, payload) -> PushSubscription:
    secret_payload = subscription_payload(payload)
    digest = endpoint_hash(secret_payload["endpoint"])
    existing = db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint_hash == digest)
    )
    now = utcnow()
    if existing is not None and existing.user_id != user.id:
        raise WebPushError("Web Push subscription is already registered")
    if existing is None:
        subscription_id = new_public_id()
        secret = save_storage_secret(
            db,
            f"web_push_subscription:{subscription_id}",
            secret_payload,
            validated_at=now,
        )
        existing = PushSubscription(
            id=subscription_id,
            user_id=user.id,
            secret_id=secret.id,
            endpoint_hash=digest,
            created_at=now,
        )
        db.add(existing)
    else:
        save_storage_secret(
            db,
            existing.secret.name,
            secret_payload,
            validated_at=now,
        )
    existing.user_agent_label = (
        str(payload.user_agent_label or "").strip()[:128] or None
    )
    existing.updated_at = now
    existing.failure_count = 0
    existing.revoked_at = None
    db.flush()
    return existing


def delete_subscription(db: Session, subscription: PushSubscription) -> None:
    secret_name = subscription.secret.name
    db.delete(subscription)
    db.flush()
    delete_storage_secret(db, secret_name)


def _status_code(exc: WebPushException) -> int | None:
    response = getattr(exc, "response", None)
    return int(response.status_code) if response is not None else None


def send_workflow_completed(
    db: Session,
    *,
    user_id: int,
    workflow_id: int,
    ready: int,
    missing: int,
    needs_review: int,
) -> dict[str, int | str]:
    """Send one final notification; delivery errors never fail the workflow."""

    if not web_push_configured():
        return {"status": "disabled", "sent": 0, "failed": 0, "revoked": 0}
    subscriptions = list(
        db.scalars(
            select(PushSubscription).where(
                PushSubscription.user_id == user_id,
                PushSubscription.revoked_at.is_(None),
            )
        )
    )
    message = json.dumps(
        {
            "type": "workflow_completed",
            "title": "Music Service",
            "body": (
                f"Готово: {ready} треков доступны, "
                f"{missing} не найдены, {needs_review} требуют проверки"
            ),
            "url": "/#/playlists",
            "tag": f"workflow-{workflow_id}",
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    result = {"status": "completed", "sent": 0, "failed": 0, "revoked": 0}
    for subscription in subscriptions:
        try:
            webpush(
                subscription_info=read_storage_secret(db, subscription.secret.name),
                data=message,
                vapid_private_key=str(_private_key_path()),
                vapid_claims={"sub": settings.web_push_vapid_subject},
                ttl=settings.web_push_ttl_seconds,
            )
            subscription.last_success_at = utcnow()
            subscription.failure_count = 0
            result["sent"] += 1
        except WebPushException as exc:
            result["failed"] += 1
            subscription.failure_count = int(subscription.failure_count or 0) + 1
            if _status_code(exc) in {404, 410}:
                subscription.revoked_at = utcnow()
                result["revoked"] += 1
        except (StorageSecretError, WebPushError, OSError):
            result["failed"] += 1
            subscription.failure_count = int(subscription.failure_count or 0) + 1
        if subscription.failure_count >= settings.web_push_max_failures:
            subscription.revoked_at = subscription.revoked_at or utcnow()
    db.flush()
    return result

