from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import PlaylistSource, ServiceEnum, utcnow
from app.services.user_credentials import get_user_credential_payload
from app.services.spotify import (
    SpotifyAccessDeniedError,
    SpotifyAuthorizationRequest,
    SpotifyOAuthStateError,
    SpotifyOAuthStateStorageError,
    SpotifyProviderError,
    SpotifyToken,
    save_spotify_token,
)
from app.services.yandex import YandexConfigurationError


def test_spotify_connect_redirects_with_issued_state(
    api_client, auth_headers, monkeypatch
):
    state_store = object()
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: state_store
    )
    monkeypatch.setattr(
        "app.api.sources.create_spotify_authorization",
        lambda store, **_kwargs: SpotifyAuthorizationRequest(
            url="https://accounts.spotify.test/authorize?state=issued-state",
            state="issued-state",
        )
        if store is state_store
        else None,
    )

    response = api_client.post(
        "/api/sources/spotify/connect",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["authorization_url"].endswith("state=issued-state")


def test_spotify_callback_upserts_one_source_without_exposing_tokens(
    api_client, auth_headers, owner_user, db, monkeypatch
):
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: object()
    )
    tokens = iter(
        [
            SpotifyToken(
                access_token="first-access",
                refresh_token="refresh-token",
                expires_at=utcnow() + timedelta(hours=1),
            ),
            SpotifyToken(
                access_token="second-access",
                refresh_token=None,
                expires_at=utcnow() + timedelta(hours=2),
            ),
        ]
    )
    monkeypatch.setattr(
        "app.api.sources.exchange_spotify_code",
        lambda *_args, **_kwargs: next(tokens),
    )
    monkeypatch.setattr(
        "app.api.sources.validate_spotify_access", lambda _token: None
    )

    first = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "first-code", "state": "first-state"},
        headers=auth_headers,
        follow_redirects=False,
    )
    second = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "second-code", "state": "second-state"},
        headers=auth_headers,
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert first.headers["location"] == "/?spotify_connected=1#/playlists"
    assert second.headers["location"] == "/?spotify_connected=1#/playlists"
    assert (
        db.scalar(
            select(func.count(PlaylistSource.id)).where(
                PlaylistSource.service == ServiceEnum.spotify
            )
        )
        == 1
    )
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.spotify)
    )
    assert source.user_id == owner_user.id
    assert get_user_credential_payload(db, owner_user.id, "spotify") == {
        "access_token": "second-access",
        "refresh_token": "refresh-token",
    }


def test_spotify_callback_rejects_invalid_state(api_client, auth_headers, monkeypatch):
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        "app.api.sources.exchange_spotify_code",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SpotifyOAuthStateError("invalid")
        ),
    )

    response = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "code", "state": "invalid-state"},
        headers=auth_headers,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/?spotify_invalid_state=1#/playlists"


@pytest.mark.parametrize(
    ("exception", "notice"),
    [
        (SpotifyOAuthStateStorageError("redis unavailable"), "state_unavailable"),
        (SpotifyProviderError("spotify unavailable"), "unavailable"),
    ],
)
def test_spotify_callback_maps_provider_outages_safely(
    api_client, auth_headers, monkeypatch, exception, notice
):
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        "app.api.sources.exchange_spotify_code",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(exception),
    )

    response = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "code", "state": "state"},
        headers=auth_headers,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == f"/?spotify_{notice}=1#/playlists"


def test_spotify_callback_keeps_previous_credential_when_access_is_denied(
    api_client, auth_headers, owner_user, db, monkeypatch
):
    source = PlaylistSource(
        user_id=owner_user.id,
        service=ServiceEnum.spotify,
    )
    db.add(source)
    db.commit()
    save_spotify_token(
        db,
        SpotifyToken(
            access_token="previous-access",
            refresh_token="previous-refresh",
            expires_at=utcnow() + timedelta(hours=2),
        ),
        source=source,
    )
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: object()
    )
    token = SpotifyToken(
        access_token="rejected-access",
        refresh_token="rejected-refresh",
        expires_at=utcnow() + timedelta(hours=1),
    )
    monkeypatch.setattr(
        "app.api.sources.exchange_spotify_code",
        lambda *_args, **_kwargs: token,
    )
    monkeypatch.setattr(
        "app.api.sources.validate_spotify_access",
        lambda _token: (_ for _ in ()).throw(
            SpotifyAccessDeniedError("not allowlisted")
        ),
    )

    response = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "code", "state": "state"},
        headers=auth_headers,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"] == "/?spotify_not_allowed=1#/playlists"
    assert db.scalar(select(func.count(PlaylistSource.id))) == 1
    assert get_user_credential_payload(db, owner_user.id, "spotify") == {
        "access_token": "previous-access",
        "refresh_token": "previous-refresh",
    }


def test_yandex_connect_and_source_list_hide_token(
    api_client, auth_headers, owner_user, db, monkeypatch
):
    monkeypatch.setattr(
        "app.api.sources.create_yandex_client",
        lambda token: {"token_valid": bool(token)},
    )

    connected = api_client.post(
        "/api/sources/yandex/connect",
        headers=auth_headers,
        json={"token": "yandex-test-token"},
    )
    listing = api_client.get("/api/sources", headers=auth_headers)

    assert connected.status_code == 200
    assert connected.json()["service"] == "yandex"
    assert "token" not in connected.json()
    assert listing.status_code == 200
    assert listing.json()["items"] == [connected.json()]
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.yandex)
    )
    assert source.user_id == owner_user.id
    assert get_user_credential_payload(db, owner_user.id, "yandex") == {
        "token": "yandex-test-token"
    }


def test_yandex_connect_rejects_invalid_user_token(
    api_client, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        "app.api.sources.create_yandex_client",
        lambda _token: (_ for _ in ()).throw(YandexConfigurationError("invalid")),
    )

    response = api_client.post(
        "/api/sources/yandex/connect",
        headers=auth_headers,
        json={"token": "invalid-token-value"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Yandex Music token is invalid"


def test_source_management_requires_auth(api_client):
    assert api_client.get("/api/sources").status_code == 401
    assert (
        api_client.post(
            "/api/sources/spotify/connect", follow_redirects=False
        ).status_code
        == 401
    )
    assert api_client.post("/api/sources/yandex/connect").status_code == 401
