from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf
from app.config import settings
from app.db import get_db
from app.models import PlaylistSource, ServiceEnum, User
from app.schemas import GoogleOAuthStartOut, SourceListOut, SourceOut, YandexCredentialIn
from app.services.user_credentials import has_user_credential, save_user_credential
from app.services.spotify import (
    SpotifyAccessDeniedError,
    SpotifyConfigurationError,
    SpotifyOAuthStateError,
    SpotifyOAuthStateStorageError,
    SpotifyProviderError,
    SpotifyTokenError,
    create_spotify_authorization,
    create_spotify_state_store,
    exchange_spotify_code,
    save_spotify_token,
    validate_spotify_access,
    validate_spotify_state,
)
from app.services.yandex import YandexConfigurationError, create_yandex_client

router = APIRouter()


def _spotify_pwa_redirect(result: str) -> RedirectResponse:
    return RedirectResponse(
        url=f"/?spotify_{result}=1#/playlists",
        status_code=status.HTTP_302_FOUND,
    )


def _source_out(db: Session, source: PlaylistSource) -> dict:
    return {
        "id": source.id,
        "service": source.service.value,
        "connected": (
            True
            if source.service == ServiceEnum.manual
            else has_user_credential(db, source.user_id, source.service.value)
        ),
        "expires_at": source.expires_at,
    }


@router.get(
    "",
    response_model=SourceListOut,
    dependencies=[Depends(require_auth)],
)
def list_sources(
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    sources = db.scalars(
        select(PlaylistSource)
        .where(PlaylistSource.user_id == current_user.id)
        .order_by(PlaylistSource.id)
    ).all()
    return {"items": [_source_out(db, source) for source in sources]}


@router.post(
    "/spotify/connect",
    dependencies=[Depends(require_csrf)],
    response_model=GoogleOAuthStartOut,
)
def connect_spotify(
    request: Request,
    current_user: User = Depends(require_auth),
):
    try:
        state_store = create_spotify_state_store(
            ttl_seconds=settings.spotify_oauth_state_ttl_seconds
        )
        authorization = create_spotify_authorization(
            state_store,
            context=f"{current_user.id}:{request.state.auth_session.id}",
        )
    except SpotifyConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify OAuth is not configured",
        ) from exc
    except SpotifyOAuthStateStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify OAuth state storage is unavailable",
        ) from exc
    return {"authorization_url": authorization.url}


@router.get("/spotify/callback", response_class=RedirectResponse)
def spotify_callback(
    request: Request,
    code: str | None = Query(default=None),
    state_value: str | None = Query(default=None, alias="state"),
    error: str | None = Query(default=None),
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    try:
        if code is not None and len(code) > 4096:
            raise SpotifyTokenError("Spotify authorization code is invalid")
        if error is not None and len(error) > 256:
            raise SpotifyOAuthStateError("Spotify callback state is invalid")
        state_store = create_spotify_state_store(
            ttl_seconds=settings.spotify_oauth_state_ttl_seconds
        )
        if not state_value:
            raise SpotifyOAuthStateError("Spotify callback state is missing")
        context = f"{current_user.id}:{request.state.auth_session.id}"
        if error:
            validate_spotify_state(
                state_store,
                state_value,
                expected_context=context,
            )
            return _spotify_pwa_redirect("access_denied")
        token = exchange_spotify_code(
            code or "",
            state_value,
            state_store,
            expected_context=context,
        )
        validate_spotify_access(token)
        source = db.scalar(
            select(PlaylistSource).where(
                PlaylistSource.user_id == current_user.id,
                PlaylistSource.service == ServiceEnum.spotify,
            )
        )
        if source is None:
            source = PlaylistSource(
                user_id=current_user.id,
                service=ServiceEnum.spotify,
            )
            db.add(source)
            db.flush()
        save_spotify_token(db, token, source=source)
    except SpotifyOAuthStateStorageError:
        return _spotify_pwa_redirect("state_unavailable")
    except SpotifyAccessDeniedError:
        return _spotify_pwa_redirect("not_allowed")
    except SpotifyOAuthStateError:
        return _spotify_pwa_redirect("invalid_state")
    except SpotifyTokenError:
        return _spotify_pwa_redirect("token_exchange")
    except SpotifyProviderError:
        return _spotify_pwa_redirect("unavailable")
    except SpotifyConfigurationError:
        return _spotify_pwa_redirect("not_configured")
    return _spotify_pwa_redirect("connected")


@router.post(
    "/yandex/connect",
    response_model=SourceOut,
    dependencies=[Depends(require_auth)],
)
def connect_yandex(
    payload: YandexCredentialIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    token = payload.token.get_secret_value().strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Yandex Music token is required",
        )
    try:
        create_yandex_client(token)
    except YandexConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Yandex Music token is invalid",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Yandex Music token validation failed",
        ) from exc

    source = db.scalar(
        select(PlaylistSource).where(
            PlaylistSource.user_id == current_user.id,
            PlaylistSource.service == ServiceEnum.yandex,
        )
    )
    if source is None:
        source = PlaylistSource(
            user_id=current_user.id,
            service=ServiceEnum.yandex,
        )
        db.add(source)
    save_user_credential(db, current_user.id, "yandex", {"token": token})
    source.expires_at = None
    db.commit()
    db.refresh(source)
    return _source_out(db, source)
