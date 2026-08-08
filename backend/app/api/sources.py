from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_auth
from app.config import settings
from app.db import get_db
from app.models import PlaylistSource, ServiceEnum
from app.schemas import SourceListOut, SourceOut
from app.services.spotify import (
    SpotifyConfigurationError,
    SpotifyOAuthStateError,
    SpotifyOAuthStateStorageError,
    SpotifyProviderError,
    SpotifyTokenError,
    create_spotify_authorization,
    create_spotify_state_store,
    exchange_spotify_code,
    save_spotify_token,
    validate_spotify_state,
)
from app.services.yandex import YandexConfigurationError, create_yandex_client

router = APIRouter()


def _source_out(source: PlaylistSource) -> dict:
    return {
        "id": source.id,
        "service": source.service.value,
        "connected": bool(source.access_token),
        "expires_at": source.expires_at,
    }


@router.get(
    "",
    response_model=SourceListOut,
    dependencies=[Depends(require_auth)],
)
def list_sources(db: Session = Depends(get_db)):
    sources = db.scalars(select(PlaylistSource).order_by(PlaylistSource.id)).all()
    return {"items": [_source_out(source) for source in sources]}


@router.get(
    "/spotify/connect",
    dependencies=[Depends(require_auth)],
    response_class=RedirectResponse,
)
def connect_spotify():
    try:
        state_store = create_spotify_state_store(
            ttl_seconds=settings.spotify_oauth_state_ttl_seconds
        )
        authorization = create_spotify_authorization(state_store)
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
    return RedirectResponse(authorization.url, status_code=status.HTTP_302_FOUND)


@router.get("/spotify/callback", response_model=SourceOut)
def spotify_callback(
    code: str | None = Query(default=None),
    state_value: str | None = Query(default=None, alias="state"),
    error: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    try:
        state_store = create_spotify_state_store(
            ttl_seconds=settings.spotify_oauth_state_ttl_seconds
        )
        if not state_value:
            raise SpotifyOAuthStateError("Spotify callback state is missing")
        if error:
            validate_spotify_state(state_store, state_value)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Spotify authorization was declined",
            )
        token = exchange_spotify_code(code or "", state_value, state_store)
        source = save_spotify_token(db, token)
    except HTTPException:
        raise
    except SpotifyOAuthStateStorageError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify OAuth state storage is unavailable",
        ) from exc
    except SpotifyOAuthStateError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Spotify OAuth state is invalid or expired",
        ) from exc
    except SpotifyTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Spotify token exchange failed",
        ) from exc
    except SpotifyProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Spotify token exchange is unavailable",
        ) from exc
    except SpotifyConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Spotify OAuth is not configured",
        ) from exc
    return _source_out(source)


@router.post(
    "/yandex/connect",
    response_model=SourceOut,
    dependencies=[Depends(require_auth)],
)
def connect_yandex(
    db: Session = Depends(get_db),
):
    token = settings.yandex_token.strip()
    if not token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="YANDEX_TOKEN is not configured",
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
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.yandex)
    )
    if source is None:
        source = PlaylistSource(service=ServiceEnum.yandex)
        db.add(source)
    source.access_token = token
    source.refresh_token = None
    source.expires_at = None
    db.commit()
    db.refresh(source)
    return _source_out(source)
