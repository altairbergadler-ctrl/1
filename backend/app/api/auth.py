from __future__ import annotations

from html import escape

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import (
    OIDC_BINDING_COOKIE_NAME,
    RECOVERY_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    app_token_is_valid,
    get_current_user,
    get_recovery_session,
    require_csrf,
    require_recovery_csrf,
    require_request_origin,
)
from app.config import settings
from app.db import get_db
from app.models import SessionKind, User, UserRole, UserSession, UserState, utcnow
from app.schemas import (
    CurrentUserOut,
    LoginIn,
    LoginOut,
    RecoveryOwnerInvitationIn,
)
from app.services.authentication import (
    AuthenticationError,
    create_session,
    csrf_token_for_session,
    lock_identity_mutation,
    normalize_email,
    revoke_session,
    revoke_user_sessions,
    session_for_token,
)
from app.services.google_login import (
    GoogleAccountNotInvited,
    GoogleIdentityError,
    GoogleLoginConfigurationError,
    GoogleLoginStateError,
    GoogleLoginUnavailable,
    begin_google_login,
    complete_google_login,
    discard_google_login,
)

router = APIRouter()


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


def _set_session_cookie(
    response: Response,
    *,
    name: str,
    value: str,
    max_age: int,
    path: str,
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path=path,
    )


def _delete_cookie(response: Response, *, name: str, path: str) -> None:
    response.delete_cookie(
        key=name,
        path=path,
        secure=settings.auth_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def _auth_error_page(status_code: int, message: str) -> HTMLResponse:
    body = f"""<!doctype html><html lang=\"ru\"><head><meta charset=\"utf-8\">
<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Audiofeel</title></head><body><main><h1>Вход не выполнен</h1>
<p>{escape(message)}</p><p><a href=\"/\">Вернуться в Audiofeel</a></p></main></body></html>"""
    response = HTMLResponse(body, status_code=status_code)
    _no_store(response)
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'"
    return response


@router.get("/google/start", include_in_schema=False)
def google_start(db: Session = Depends(get_db)):
    try:
        authorization = begin_google_login(db)
    except GoogleLoginConfigurationError as exc:
        raise HTTPException(status_code=503, detail="Google login is unavailable") from exc
    except GoogleLoginUnavailable as exc:
        raise HTTPException(status_code=502, detail="Google login is unavailable") from exc
    response = RedirectResponse(authorization.url, status_code=302)
    _set_session_cookie(
        response,
        name=OIDC_BINDING_COOKIE_NAME,
        value=authorization.binding,
        max_age=settings.google_login_state_ttl_seconds,
        path="/api/auth/google/callback",
    )
    _no_store(response)
    return response


@router.get("/google/callback", include_in_schema=False)
def google_callback(
    request: Request,
    state_value: str | None = Query(default=None, alias="state"),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    binding = request.cookies.get(OIDC_BINDING_COOKIE_NAME, "")
    invalid_query = (
        not state_value
        or len(state_value) > 512
        or not binding
        or len(binding) > 512
        or (code is not None and len(code) > 4096)
        or (error is not None and len(error) > 256)
    )
    if invalid_query:
        response = _auth_error_page(400, "Попробуйте начать вход заново.")
        _delete_cookie(
            response,
            name=OIDC_BINDING_COOKIE_NAME,
            path="/api/auth/google/callback",
        )
        return response
    if error or not code:
        try:
            discard_google_login(db, state=state_value, binding=binding)
        except GoogleLoginStateError:
            db.rollback()
        response = _auth_error_page(400, "Авторизация Google была отменена.")
        _delete_cookie(
            response,
            name=OIDC_BINDING_COOKIE_NAME,
            path="/api/auth/google/callback",
        )
        return response
    try:
        user = complete_google_login(
            db,
            state=state_value,
            binding=binding,
            code=code,
        )
    except GoogleAccountNotInvited:
        db.rollback()
        response = _auth_error_page(
            403,
            "Этот аккаунт не приглашён. Обратитесь к владельцу сервиса.",
        )
        _delete_cookie(response, name=OIDC_BINDING_COOKIE_NAME, path="/api/auth/google/callback")
        return response
    except (
        GoogleLoginStateError,
        GoogleIdentityError,
        AuthenticationError,
        IntegrityError,
    ):
        db.rollback()
        response = _auth_error_page(400, "Попробуйте начать вход заново.")
        _delete_cookie(response, name=OIDC_BINDING_COOKIE_NAME, path="/api/auth/google/callback")
        return response
    except (GoogleLoginConfigurationError, GoogleLoginUnavailable):
        db.rollback()
        response = _auth_error_page(502, "Google временно недоступен.")
        _delete_cookie(response, name=OIDC_BINDING_COOKIE_NAME, path="/api/auth/google/callback")
        return response

    previous = request.cookies.get(SESSION_COOKIE_NAME, "")
    if previous:
        old_session = session_for_token(db, previous, kind=SessionKind.google, touch=False)
        if old_session is not None:
            revoke_session(db, old_session)
    raw, _, _ = create_session(db, user)
    db.commit()
    response = RedirectResponse(url="/#/playlists", status_code=303)
    _set_session_cookie(
        response,
        name=SESSION_COOKIE_NAME,
        value=raw,
        max_age=settings.auth_cookie_max_age_seconds,
        path="/api",
    )
    _delete_cookie(response, name=OIDC_BINDING_COOKIE_NAME, path="/api/auth/google/callback")
    _no_store(response)
    return response


@router.get("/me", response_model=CurrentUserOut)
def me(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
):
    _no_store(response)
    raw = getattr(request.state, "auth_session_token", "")
    return {
        "id": current_user.id,
        "email": current_user.email or "",
        "display_name": current_user.display_name,
        "role": current_user.role.value,
        "csrf_token": csrf_token_for_session(raw),
    }


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    _: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    record: UserSession = request.state.auth_session
    revoke_session(db, record)
    db.commit()
    _delete_cookie(response, name=SESSION_COOKIE_NAME, path="/api")
    _no_store(response)


@router.post("/recovery/login", response_model=LoginOut)
def recovery_login(
    payload: LoginIn,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
):
    require_request_origin(request)
    if not app_token_is_valid(payload.token.get_secret_value()):
        raise HTTPException(status_code=401, detail="Recovery authentication failed")
    owner = db.scalar(
        select(User).where(User.is_bootstrap_owner.is_(True)).with_for_update()
    )
    if owner is None or owner.role != UserRole.owner:
        raise HTTPException(status_code=503, detail="Recovery is unavailable")
    previous = request.cookies.get(RECOVERY_COOKIE_NAME, "")
    if previous:
        old_session = session_for_token(
            db, previous, kind=SessionKind.recovery, touch=False
        )
        if old_session is not None:
            revoke_session(db, old_session)
    raw, csrf, _ = create_session(db, owner, kind=SessionKind.recovery)
    db.commit()
    _set_session_cookie(
        response,
        name=RECOVERY_COOKIE_NAME,
        value=raw,
        max_age=settings.auth_recovery_max_age_seconds,
        path="/api/auth/recovery",
    )
    _no_store(response)
    return {"authenticated": True, "csrf_token": csrf}


@router.get("/recovery/status")
def recovery_status(
    response: Response,
    record: UserSession = Depends(get_recovery_session),
):
    _no_store(response)
    owner = record.user
    return {
        "owner_invited": bool(owner.email),
        "owner_active": owner.state == UserState.active,
    }


@router.post("/recovery/owner-invitation", status_code=status.HTTP_204_NO_CONTENT)
def recovery_owner_invitation(
    payload: RecoveryOwnerInvitationIn,
    response: Response,
    record: UserSession = Depends(require_recovery_csrf),
    db: Session = Depends(get_db),
):
    lock_identity_mutation(db)
    owner = db.scalar(
        select(User).where(User.id == record.user_id).with_for_update()
    )
    if owner is None or not owner.is_bootstrap_owner or owner.role != UserRole.owner:
        raise HTTPException(status_code=403, detail="Not allowed")
    try:
        email, email_key = normalize_email(payload.email)
    except AuthenticationError as exc:
        raise HTTPException(status_code=422, detail="Email address is invalid") from exc
    duplicate = db.scalar(
        select(func.count(User.id)).where(
            User.email_key == email_key,
            User.id != owner.id,
        )
    )
    if duplicate:
        raise HTTPException(status_code=409, detail="Invitation cannot be updated")
    owner.email = email
    owner.email_key = email_key
    owner.google_sub = None
    owner.display_name = None
    owner.state = UserState.pending
    owner.activated_at = None
    owner.last_login_at = None
    revoke_user_sessions(db, owner.id)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="Invitation cannot be updated"
        ) from exc
    _delete_cookie(response, name=RECOVERY_COOKIE_NAME, path="/api/auth/recovery")
    _no_store(response)


@router.post("/recovery/revoke-owner-sessions", status_code=status.HTTP_204_NO_CONTENT)
def recovery_revoke_owner_sessions(
    response: Response,
    record: UserSession = Depends(require_recovery_csrf),
    db: Session = Depends(get_db),
):
    if not record.user.is_bootstrap_owner:
        raise HTTPException(status_code=403, detail="Not allowed")
    revoke_user_sessions(db, record.user_id)
    db.commit()
    _delete_cookie(response, name=RECOVERY_COOKIE_NAME, path="/api/auth/recovery")
    _no_store(response)


@router.post("/recovery/logout", status_code=status.HTTP_204_NO_CONTENT)
def recovery_logout(
    response: Response,
    record: UserSession = Depends(require_recovery_csrf),
    db: Session = Depends(get_db),
):
    revoke_session(db, record)
    db.commit()
    _delete_cookie(response, name=RECOVERY_COOKIE_NAME, path="/api/auth/recovery")
    _no_store(response)
