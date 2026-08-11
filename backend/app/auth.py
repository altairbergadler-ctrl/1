"""Authentication, role and CSRF dependencies.

APP_AUTH_TOKEN is deliberately absent from the normal-user dependency.  It is
accepted only by the explicitly separated recovery login endpoint.
"""

from __future__ import annotations

import hmac
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models import SessionKind, User, UserRole, UserSession
from app.services.authentication import csrf_hash, session_for_token
from app.services.credentials import CredentialKeyError

SESSION_COOKIE_NAME = "audiofeel_session"
RECOVERY_COOKIE_NAME = "audiofeel_recovery"
OIDC_BINDING_COOKIE_NAME = "audiofeel_oidc"
CSRF_HEADER_NAME = "x-csrf-token"


def constant_time_equal(candidate: str | None, expected: str) -> bool:
    if candidate is None:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def app_token_is_valid(candidate: str | None) -> bool:
    return constant_time_equal(candidate, settings.app_auth_token)


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")


def _load_session(
    request: Request,
    db: Session,
    *,
    cookie_name: str,
    kind: SessionKind,
) -> UserSession:
    raw = request.cookies.get(cookie_name, "")
    try:
        record = session_for_token(db, raw, kind=kind)
    except CredentialKeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is unavailable",
        ) from exc
    if record is None:
        raise _unauthorized()
    request.state.auth_session = record
    request.state.auth_session_token = raw
    return record


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User:
    return _load_session(
        request,
        db,
        cookie_name=SESSION_COOKIE_NAME,
        kind=SessionKind.google,
    ).user


def require_auth(current_user: User = Depends(get_current_user)) -> User:
    """Compatibility name used by routers; now returns the authenticated user."""

    return current_user


def require_owner(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return current_user


def get_recovery_session(
    request: Request, db: Session = Depends(get_db)
) -> UserSession:
    return _load_session(
        request,
        db,
        cookie_name=RECOVERY_COOKIE_NAME,
        kind=SessionKind.recovery,
    )


def _canonical_origin(value: str) -> tuple[str, str, int | None] | None:
    try:
        parsed = urlsplit(value)
        if parsed.username or parsed.password or not parsed.scheme or not parsed.hostname:
            return None
        port = parsed.port
    except ValueError:
        return None
    return parsed.scheme.casefold(), parsed.hostname.casefold(), port


def require_request_origin(request: Request) -> None:
    """Require the exact configured browser origin for a state-changing request."""

    expected = _canonical_origin(settings.public_origin)
    origin = request.headers.get("origin")
    if origin:
        source = _canonical_origin(origin)
    else:
        referer = request.headers.get("referer")
        source = _canonical_origin(referer) if referer else None
    if expected is None or source != expected:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    if request.headers.get("sec-fetch-site", "").casefold() == "cross-site":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")


def require_csrf(
    request: Request,
    current_user: User = Depends(get_current_user),
) -> User:
    require_request_origin(request)
    record: UserSession | None = getattr(request.state, "auth_session", None)
    candidate = request.headers.get(CSRF_HEADER_NAME, "")
    if (
        record is None
        or not candidate
        or not hmac.compare_digest(record.csrf_hash, csrf_hash(candidate))
    ):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    return current_user


def require_owner_csrf(current_user: User = Depends(require_csrf)) -> User:
    if current_user.role != UserRole.owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not allowed")
    return current_user


def require_recovery_csrf(
    request: Request,
    record: UserSession = Depends(get_recovery_session),
) -> UserSession:
    require_request_origin(request)
    candidate = request.headers.get(CSRF_HEADER_NAME, "")
    if not candidate or not hmac.compare_digest(record.csrf_hash, csrf_hash(candidate)):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Request rejected")
    return record


# Kept as a narrow compatibility alias while route declarations are migrated.
require_same_origin = require_request_origin
