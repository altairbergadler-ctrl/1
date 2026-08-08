import hashlib
import hmac
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import settings

bearer = HTTPBearer(auto_error=False)
SESSION_COOKIE_NAME = "music_session"


def constant_time_equal(candidate: str | None, expected: str) -> bool:
    if candidate is None:
        return False
    return hmac.compare_digest(
        candidate.encode("utf-8"),
        expected.encode("utf-8"),
    )


def app_token_is_valid(candidate: str | None) -> bool:
    return constant_time_equal(candidate, settings.app_auth_token)


def session_cookie_value() -> str:
    """Return a stable HMAC session without exposing the configured API token."""

    return hmac.new(
        settings.app_auth_token.encode("utf-8"),
        b"music-service-mvp-session-v1",
        hashlib.sha256,
    ).hexdigest()


def require_auth(
    creds: HTTPAuthorizationCredentials = Depends(bearer),
    session_cookie: str | None = Cookie(default=None, alias=SESSION_COOKIE_NAME),
):
    """Accept the static bearer token or the HttpOnly PWA session cookie."""

    bearer_valid = bool(creds and app_token_is_valid(creds.credentials))
    cookie_valid = bool(
        session_cookie
        and constant_time_equal(session_cookie, session_cookie_value())
    )
    if not bearer_valid and not cookie_valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )
    return True
