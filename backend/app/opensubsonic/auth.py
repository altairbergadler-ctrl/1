from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from app.models import PlayerCredential
from app.opensubsonic.protocol import OpenSubsonicError, validate_version
from app.services.player_credentials import (
    PlayerCredentialError,
    PlayerRateLimitUnavailable,
    authenticate_player_key,
)

LEGACY_PARAMETERS = {"u", "p", "t", "s"}


def authenticate(request: Request, db: Session) -> PlayerCredential:
    validate_version(request)
    names = set(request.query_params.keys())
    raw = request.query_params.get("apiKey")
    if raw and names.intersection(LEGACY_PARAMETERS):
        raise OpenSubsonicError(43, "Multiple authentication mechanisms")
    if not raw:
        if "t" in names or "s" in names:
            raise OpenSubsonicError(41, "Token authentication is not supported")
        if "p" in names or "u" in names:
            raise OpenSubsonicError(42, "Password authentication is not supported")
        raise OpenSubsonicError(10, "Authentication is required")
    client_ip = request.client.host if request.client else "unknown"
    try:
        credential = authenticate_player_key(db, raw, client_ip=client_ip)
    except PlayerRateLimitUnavailable as exc:
        raise OpenSubsonicError(44, "Authentication is unavailable", 503) from exc
    except PlayerCredentialError as exc:
        raise OpenSubsonicError(44, "Authentication failed", 429) from exc
    if credential is None:
        raise OpenSubsonicError(44, "Authentication failed")
    return credential
