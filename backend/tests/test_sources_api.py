from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import PlaylistSource, ServiceEnum, utcnow
from app.services.credentials import get_credential_payload
from app.services.spotify import (
    SpotifyAuthorizationRequest,
    SpotifyOAuthStateError,
    SpotifyOAuthStateStorageError,
    SpotifyProviderError,
    SpotifyToken,
)


def test_spotify_connect_redirects_with_issued_state(
    api_client, auth_headers, monkeypatch
):
    state_store = object()
    monkeypatch.setattr(
        "app.api.sources.create_spotify_state_store", lambda **_kwargs: state_store
    )
    monkeypatch.setattr(
        "app.api.sources.create_spotify_authorization",
        lambda store: SpotifyAuthorizationRequest(
            url="https://accounts.spotify.test/authorize?state=issued-state",
            state="issued-state",
        )
        if store is state_store
        else None,
    )

    response = api_client.get(
        "/api/sources/spotify/connect",
        headers=auth_headers,
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert response.headers["location"].endswith("state=issued-state")


def test_spotify_callback_upserts_one_source_without_exposing_tokens(
    api_client, db, monkeypatch
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

    first = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "first-code", "state": "first-state"},
        follow_redirects=False,
    )
    second = api_client.get(
        "/api/sources/spotify/callback",
        params={"code": "second-code", "state": "second-state"},
        follow_redirects=False,
    )

    assert first.status_code == 302
    assert second.status_code == 302
    assert first.headers["location"] == "/#/playlists"
    assert second.headers["location"] == "/#/playlists"
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
    assert source.access_token is None
    assert source.refresh_token is None
    assert get_credential_payload(db, "spotify") == {
        "access_token": "second-access",
        "refresh_token": "refresh-token",
    }


def test_spotify_callback_rejects_invalid_state(api_client, monkeypatch):
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
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Spotify OAuth state is invalid or expired"


@pytest.mark.parametrize(
    ("exception", "expected_status"),
    [
        (SpotifyOAuthStateStorageError("redis unavailable"), 503),
        (SpotifyProviderError("spotify unavailable"), 502),
    ],
)
def test_spotify_callback_maps_provider_outages_safely(
    api_client, monkeypatch, exception, expected_status
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
    )

    assert response.status_code == expected_status
    assert "unavailable" in response.json()["detail"]


def test_yandex_connect_and_source_list_hide_token(
    api_client, auth_headers, db, monkeypatch
):
    monkeypatch.setattr("app.api.sources.settings.yandex_token", "yandex-test-token")
    monkeypatch.setattr(
        "app.api.sources.create_yandex_client",
        lambda token: {"token_valid": bool(token)},
    )

    connected = api_client.post(
        "/api/sources/yandex/connect",
        headers=auth_headers,
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
    assert source.access_token is None
    assert get_credential_payload(db, "yandex") == {"token": "yandex-test-token"}


def test_yandex_connect_reads_token_only_from_settings(
    api_client, auth_headers, monkeypatch
):
    monkeypatch.setattr("app.api.sources.settings.yandex_token", "")
    monkeypatch.setattr(
        "app.api.sources.create_yandex_client",
        lambda _token: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    response = api_client.post(
        "/api/sources/yandex/connect",
        headers=auth_headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "YANDEX_TOKEN is not configured"


def test_source_management_requires_auth(api_client):
    assert api_client.get("/api/sources").status_code == 401
    assert (
        api_client.get(
            "/api/sources/spotify/connect", follow_redirects=False
        ).status_code
        == 401
    )
    assert api_client.post("/api/sources/yandex/connect").status_code == 401
