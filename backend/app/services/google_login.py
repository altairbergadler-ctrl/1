"""Google OpenID Connect login with PKCE and invitation-only binding."""

from __future__ import annotations

import base64
import hashlib
import logging
import re
import secrets
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from datetime import timedelta
from pathlib import Path
from time import monotonic
from typing import Any, Mapping
from urllib.parse import urlencode, urlsplit

import httpx
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.config import settings
from app.models import GoogleLoginAttempt, User, UserState, utcnow
from app.services.authentication import (
    AuthenticationError,
    keyed_digest,
    lock_identity_mutation,
    normalize_email,
    open_login_value,
    seal_login_value,
)

DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"
EXPECTED_ISSUER = "https://accounts.google.com"
_ALLOWED_ENDPOINTS = {
    "authorization_endpoint": {"accounts.google.com"},
    "token_endpoint": {"oauth2.googleapis.com"},
    "jwks_uri": {"www.googleapis.com"},
}


class GoogleLoginError(RuntimeError):
    pass


class GoogleLoginConfigurationError(GoogleLoginError):
    pass


class GoogleLoginStateError(GoogleLoginError):
    pass


class GoogleIdentityError(GoogleLoginError):
    pass


class GoogleLoginUnavailable(GoogleLoginError):
    pass


class GoogleAccountNotInvited(GoogleLoginError):
    pass


@dataclass(frozen=True)
class GoogleAuthorization:
    url: str
    binding: str


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    display_name: str | None


_cache_lock = threading.Lock()
_metadata_cache: tuple[float, dict[str, Any]] | None = None
_jwks_cache: tuple[float, dict[str, dict[str, Any]]] | None = None

# Never emit token endpoint request details or authorization codes through the
# HTTP client's informational request logger.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def clear_google_oidc_cache() -> None:
    global _metadata_cache, _jwks_cache
    with _cache_lock:
        _metadata_cache = None
        _jwks_cache = None


def _client_secret() -> str:
    try:
        value = Path(settings.google_login_client_secret_file).read_text(
            encoding="utf-8"
        ).strip()
    except OSError as exc:
        raise GoogleLoginConfigurationError("Google login is not configured") from exc
    if len(value) < 8:
        raise GoogleLoginConfigurationError("Google login is not configured")
    return value


def google_login_configured() -> bool:
    if len(settings.google_login_client_id.strip()) < 20:
        return False
    try:
        _client_secret()
    except GoogleLoginConfigurationError:
        return False
    return True


def _cache_seconds(headers: Mapping[str, str], default: int) -> int:
    value = headers.get("cache-control", "")
    match = re.search(r"(?:^|,)\s*max-age=(\d+)", value, re.IGNORECASE)
    if not match:
        return default
    return max(60, min(int(match.group(1)), 24 * 60 * 60))


def _valid_https_endpoint(name: str, value: Any) -> str:
    candidate = str(value or "")
    parsed = urlsplit(candidate)
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or (parsed.hostname or "").casefold() not in _ALLOWED_ENDPOINTS[name]
    ):
        raise GoogleLoginUnavailable("Google discovery metadata is invalid")
    return candidate


def _metadata(*, force: bool = False) -> dict[str, Any]:
    global _metadata_cache
    now = monotonic()
    with _cache_lock:
        if not force and _metadata_cache is not None and _metadata_cache[0] > now:
            return dict(_metadata_cache[1])
    try:
        response = httpx.get(DISCOVERY_URL, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        raw = response.json()
    except Exception as exc:
        raise GoogleLoginUnavailable("Google discovery is unavailable") from exc
    if not isinstance(raw, dict) or raw.get("issuer") != EXPECTED_ISSUER:
        raise GoogleLoginUnavailable("Google discovery metadata is invalid")
    metadata = {
        "issuer": EXPECTED_ISSUER,
        "authorization_endpoint": _valid_https_endpoint(
            "authorization_endpoint", raw.get("authorization_endpoint")
        ),
        "token_endpoint": _valid_https_endpoint(
            "token_endpoint", raw.get("token_endpoint")
        ),
        "jwks_uri": _valid_https_endpoint("jwks_uri", raw.get("jwks_uri")),
    }
    methods = raw.get("code_challenge_methods_supported") or []
    if "S256" not in methods:
        raise GoogleLoginUnavailable("Google PKCE support is unavailable")
    with _cache_lock:
        _metadata_cache = (
            now + _cache_seconds(response.headers, 60 * 60),
            metadata,
        )
    return dict(metadata)


def _jwks(*, force: bool = False) -> dict[str, dict[str, Any]]:
    global _jwks_cache
    now = monotonic()
    with _cache_lock:
        if not force and _jwks_cache is not None and _jwks_cache[0] > now:
            return dict(_jwks_cache[1])
    uri = _metadata(force=force)["jwks_uri"]
    try:
        response = httpx.get(uri, timeout=10.0, follow_redirects=False)
        response.raise_for_status()
        raw = response.json()
    except Exception as exc:
        raise GoogleLoginUnavailable("Google signing keys are unavailable") from exc
    keys: dict[str, dict[str, Any]] = {}
    if isinstance(raw, dict) and isinstance(raw.get("keys"), list):
        for item in raw["keys"]:
            if (
                isinstance(item, dict)
                and isinstance(item.get("kid"), str)
                and item.get("kty") == "RSA"
                and item.get("use") in (None, "sig")
                and item.get("alg") in (None, "RS256")
            ):
                keys[item["kid"]] = dict(item)
    if not keys:
        raise GoogleLoginUnavailable("Google signing keys are invalid")
    with _cache_lock:
        _jwks_cache = (now + _cache_seconds(response.headers, 60 * 60), keys)
    return dict(keys)


def _pkce_challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


def begin_google_login(db: Session) -> GoogleAuthorization:
    client_id = settings.google_login_client_id.strip()
    if not google_login_configured():
        raise GoogleLoginConfigurationError("Google login is not configured")
    metadata = _metadata()
    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    binding = secrets.token_urlsafe(32)
    attempt_id = str(uuid.uuid4())
    ciphertext, encrypted_nonce, auth_key_id = seal_login_value(attempt_id, verifier)
    now = utcnow()
    db.execute(
        delete(GoogleLoginAttempt).where(
            (GoogleLoginAttempt.expires_at <= now)
            | GoogleLoginAttempt.consumed_at.is_not(None)
        )
    )
    db.add(
        GoogleLoginAttempt(
            id=attempt_id,
            state_hash=keyed_digest("google-state-v1", state),
            browser_binding_hash=keyed_digest("google-binding-v1", binding),
            nonce_hash=keyed_digest("google-nonce-v1", nonce),
            pkce_ciphertext=ciphertext,
            pkce_nonce=encrypted_nonce,
            key_id=auth_key_id,
            created_at=now,
            expires_at=now
            + timedelta(seconds=settings.google_login_state_ttl_seconds),
        )
    )
    db.commit()
    query = urlencode(
        {
            "client_id": client_id,
            "redirect_uri": settings.google_login_redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _pkce_challenge(verifier),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
    )
    return GoogleAuthorization(
        url=f"{metadata['authorization_endpoint']}?{query}", binding=binding
    )


def _consume_attempt(db: Session, state: str, binding: str) -> GoogleLoginAttempt:
    if not state or not binding or len(state) > 512 or len(binding) > 512:
        raise GoogleLoginStateError("Google login state is invalid")
    attempt = db.scalar(
        select(GoogleLoginAttempt)
        .where(
            GoogleLoginAttempt.state_hash
            == keyed_digest("google-state-v1", state)
        )
        .with_for_update()
    )
    now = utcnow()
    if (
        attempt is None
        or attempt.consumed_at is not None
        or attempt.expires_at <= now
        or not secrets.compare_digest(
            attempt.browser_binding_hash,
            keyed_digest("google-binding-v1", binding),
        )
    ):
        raise GoogleLoginStateError("Google login state is invalid")
    attempt.consumed_at = now
    db.commit()
    return attempt


def discard_google_login(db: Session, *, state: str, binding: str) -> None:
    _consume_attempt(db, state, binding)


def _exchange_code(code: str, verifier: str) -> str:
    if not code or len(code) > 4096:
        raise GoogleIdentityError("Google authorization code is invalid")
    metadata = _metadata()
    try:
        response = httpx.post(
            metadata["token_endpoint"],
            data={
                "code": code,
                "client_id": settings.google_login_client_id.strip(),
                "client_secret": _client_secret(),
                "redirect_uri": settings.google_login_redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": verifier,
            },
            timeout=15.0,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
    except GoogleLoginConfigurationError:
        raise
    except Exception as exc:
        raise GoogleLoginUnavailable("Google token exchange is unavailable") from exc
    token = payload.get("id_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or not token or len(token) > 32_768:
        raise GoogleIdentityError("Google token response has no identity token")
    return token


def validate_google_id_token(
    token: str,
    *,
    expected_nonce_hash: bytes,
    attempt_created_at,
) -> GoogleIdentity:
    try:
        header = jwt.get_unverified_header(token)
    except JWTError as exc:
        raise GoogleIdentityError("Google identity token header is invalid") from exc
    kid = header.get("kid") if isinstance(header, dict) else None
    algorithm = header.get("alg") if isinstance(header, dict) else None
    if algorithm != "RS256" or not isinstance(kid, str) or not kid:
        raise GoogleIdentityError("Google identity token is invalid")
    key = _jwks().get(kid)
    if key is None:
        key = _jwks(force=True).get(kid)
    if key is None:
        raise GoogleIdentityError("Google signing key is unknown")
    client_id = settings.google_login_client_id.strip()
    try:
        claims = jwt.decode(
            token,
            key,
            algorithms=["RS256"],
            audience=client_id,
            issuer=EXPECTED_ISSUER,
            options={
                "require_aud": True,
                "require_exp": True,
                "require_iat": True,
                "require_iss": True,
                "require_sub": True,
                "leeway": settings.google_login_clock_skew_seconds,
            },
        )
    except JWTError as exc:
        reason = str(exc).casefold()
        if "issuer" in reason:
            message = "Google identity issuer is invalid"
        elif "audience" in reason:
            message = "Google identity audience is invalid"
        elif "expired" in reason or "expiration" in reason:
            message = "Google identity token is expired"
        elif "signature" in reason:
            message = "Google identity signature is invalid"
        else:
            message = "Google identity claims are invalid"
        raise GoogleIdentityError(message) from exc
    audience = claims.get("aud")
    if isinstance(audience, str):
        audience_ok = audience == client_id
        multiple_audiences = False
    elif isinstance(audience, list):
        audience_ok = all(isinstance(item, str) for item in audience) and (
            client_id in audience
        )
        multiple_audiences = len(audience) > 1
    else:
        audience_ok = False
        multiple_audiences = False
    if not audience_ok:
        raise GoogleIdentityError("Google identity audience is invalid")
    authorized_party = claims.get("azp")
    if (multiple_audiences and authorized_party != client_id) or (
        authorized_party is not None and authorized_party != client_id
    ):
        raise GoogleIdentityError("Google authorized party is invalid")
    now = utcnow()
    try:
        if isinstance(claims["iat"], bool):
            raise ValueError("boolean issue time")
        issued_at = int(claims["iat"])
        issued = datetime.fromtimestamp(issued_at, tz=UTC).replace(tzinfo=None)
    except Exception as exc:
        raise GoogleIdentityError("Google issue time is invalid") from exc
    skew = timedelta(seconds=settings.google_login_clock_skew_seconds)
    if (
        issued > now + skew
        or issued < attempt_created_at - skew
        or now - issued
        > timedelta(seconds=settings.google_login_max_token_age_seconds) + skew
    ):
        raise GoogleIdentityError("Google identity token is too old")
    nonce = claims.get("nonce")
    if not isinstance(nonce, str) or not secrets.compare_digest(
        expected_nonce_hash, keyed_digest("google-nonce-v1", nonce)
    ):
        raise GoogleIdentityError("Google nonce is invalid")
    if claims.get("email_verified") is not True:
        raise GoogleIdentityError("Google email is not verified")
    sub = claims.get("sub")
    email = claims.get("email")
    if not isinstance(sub, str) or not sub or len(sub) > 255:
        raise GoogleIdentityError("Google subject is invalid")
    if not isinstance(email, str):
        raise GoogleIdentityError("Google email is invalid")
    try:
        normalized_email, _ = normalize_email(email)
    except AuthenticationError as exc:
        raise GoogleIdentityError("Google email is invalid") from exc
    name = claims.get("name")
    display_name = str(name).strip()[:255] if isinstance(name, str) else None
    return GoogleIdentity(sub=sub, email=normalized_email, display_name=display_name or None)


def complete_google_login(
    db: Session,
    *,
    state: str,
    binding: str,
    code: str,
) -> User:
    attempt = _consume_attempt(db, state, binding)
    verifier = open_login_value(
        attempt.id,
        attempt.pkce_ciphertext,
        attempt.pkce_nonce,
        attempt.key_id,
    )
    identity = validate_google_id_token(
        _exchange_code(code, verifier),
        expected_nonce_hash=attempt.nonce_hash,
        attempt_created_at=attempt.created_at,
    )
    _, email_key = normalize_email(identity.email)
    lock_identity_mutation(db)
    user = db.scalar(
        select(User).where(User.google_sub == identity.sub).with_for_update()
    )
    if user is not None:
        if user.state != UserState.active:
            raise GoogleAccountNotInvited("Google account is not allowed")
    else:
        user = db.scalar(
            select(User)
            .where(
                User.email_key == email_key,
                User.state == UserState.pending,
                User.google_sub.is_(None),
            )
            .with_for_update()
        )
        if user is None:
            raise GoogleAccountNotInvited("Google account is not allowed")
        user.google_sub = identity.sub
        user.state = UserState.active
        user.activated_at = utcnow()
    if identity.display_name:
        user.display_name = identity.display_name
    user.last_login_at = utcnow()
    db.commit()
    db.refresh(user)
    return user
