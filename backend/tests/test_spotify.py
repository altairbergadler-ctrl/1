from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models import Playlist, PlaylistItem, PlaylistSource, ServiceEnum, utcnow
from app.services.spotify import (
    RedisOAuthStateStore,
    SpotifyConfigurationError,
    SpotifyOAuthStateError,
    SpotifyProviderError,
    SpotifyToken,
    create_spotify_authorization,
    create_spotify_oauth,
    exchange_spotify_code,
    import_spotify_playlists,
    refresh_spotify_playlist,
    refresh_spotify_token,
    save_spotify_token,
)


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def set(self, key: str, value: str, *, ex: int, nx: bool):
        if nx and key in self.values:
            return False
        self.values[key] = value
        self.ttls[key] = ex
        return True

    def getdel(self, key: str):
        self.ttls.pop(key, None)
        return self.values.pop(key, None)


class FakeOAuth:
    def __init__(self):
        self.codes: list[str] = []
        self.refreshes: list[str] = []

    def get_authorize_url(self, state=None):
        return f"https://accounts.spotify.test/authorize?state={state}"

    def get_access_token(self, code, *, as_dict=True, check_cache=False):
        assert as_dict is True
        assert check_cache is False
        self.codes.append(code)
        return {
            "access_token": "access-from-code",
            "refresh_token": "refresh-from-code",
            "expires_in": 3600,
            "token_type": "Bearer",
        }

    def refresh_access_token(self, refresh_token):
        self.refreshes.append(refresh_token)
        return {
            "access_token": "refreshed-access",
            "expires_in": 1800,
        }


def _playlist(external_id: str, name: str, snapshot: str):
    return {
        "id": external_id,
        "name": name,
        "snapshot_id": snapshot,
        "tracks": {"total": 1},
    }


def _track(
    track_id: str | None,
    title: str,
    *,
    artists: tuple[str, ...] = ("Artist",),
    album: str = "Album",
    isrc: str | None = "US-AAA-24-00001",
    duration_ms: int = 123_000,
):
    return {
        "track": {
            "id": track_id,
            "type": "track",
            "name": title,
            "artists": [{"name": name} for name in artists],
            "album": {"name": album},
            "external_ids": {"isrc": isrc} if isrc is not None else {},
            "duration_ms": duration_ms,
        }
    }


class FakeSpotify:
    def __init__(self):
        self.snapshots = {"p1": "snapshot-1", "p2": "snapshot-2"}
        self.playlist_calls: list[tuple[int, int]] = []
        self.item_calls: list[tuple[str, int, int, tuple[str, ...]]] = []
        self.changed_p1 = False
        self.invalid_p2 = False

    def current_user_playlists(self, *, limit, offset):
        self.playlist_calls.append((limit, offset))
        pages = {
            0: {
                "items": [
                    _playlist("p1", "Road Trip", self.snapshots["p1"]),
                ],
                "next": "https://api.spotify.test/me/playlists?offset=1",
            },
            1: {
                "items": [
                    _playlist("p2", "Quiet", self.snapshots["p2"]),
                    None,
                ],
                "next": None,
            },
        }
        return pages[offset]

    def playlist_items(
        self,
        playlist_id,
        *,
        limit,
        offset,
        additional_types,
    ):
        self.item_calls.append((playlist_id, limit, offset, additional_types))
        if playlist_id == "p1" and self.changed_p1:
            return {
                "items": [
                    _track(
                        "new-track",
                        "New Song (Remaster)",
                        artists=("New Artist",),
                        album="New Album",
                        isrc=None,
                        duration_ms=222_000,
                    )
                ],
                "next": None,
            }
        if playlist_id == "p1":
            if offset == 0:
                return {
                    "items": [
                        _track(
                            "t1",
                            "First Song",
                            artists=("Artist A", "Artist B"),
                        ),
                        _track(
                            None,
                            "Local Song",
                            artists=("Local Artist",),
                            isrc="not-an-isrc",
                        ),
                    ],
                    "next": "https://api.spotify.test/playlists/p1/tracks?offset=2",
                }
            return {
                "items": [
                    {"track": {"type": "episode", "name": "Podcast"}},
                    {"track": None},
                ],
                "next": None,
            }
        if self.invalid_p2:
            return {"next": None}
        item = _track("t2", "Quiet Song", album="Quiet Album")["track"]
        return {
            "items": [{"item": item}],
            "next": None,
        }


def _source(db) -> PlaylistSource:
    source = PlaylistSource(
        service=ServiceEnum.spotify,
        access_token="test-access",
        refresh_token="test-refresh",
        expires_at=utcnow() + timedelta(hours=1),
    )
    db.add(source)
    db.commit()
    return source


def _simple_normalize(value: str) -> str:
    return " ".join(value.casefold().split())


def test_oauth_state_is_random_one_time_and_checked_before_code_exchange():
    redis = FakeRedis()
    store = RedisOAuthStateStore(redis, ttl_seconds=600)
    oauth = FakeOAuth()

    authorization = create_spotify_authorization(store, oauth=oauth)

    assert len(authorization.state) >= 32
    assert parse_qs(urlparse(authorization.url).query)["state"] == [authorization.state]
    assert all(authorization.state not in key for key in redis.values)
    assert list(redis.ttls.values()) == [600]

    token = exchange_spotify_code(
        "authorization-code",
        authorization.state,
        store,
        oauth=oauth,
    )
    assert token.access_token == "access-from-code"
    assert token.refresh_token == "refresh-from-code"
    assert oauth.codes == ["authorization-code"]

    with pytest.raises(SpotifyOAuthStateError):
        exchange_spotify_code(
            "replayed-code",
            authorization.state,
            store,
            oauth=oauth,
        )
    assert oauth.codes == ["authorization-code"]


def test_create_oauth_requires_all_credentials():
    config = SimpleNamespace(
        spotify_client_id="client",
        spotify_client_secret="",
        spotify_redirect_uri="https://service.test/callback",
    )

    with pytest.raises(SpotifyConfigurationError):
        create_spotify_oauth(config)


def test_refresh_preserves_rotating_token_when_spotify_omits_it():
    oauth = FakeOAuth()

    token = refresh_spotify_token("original-refresh", oauth=oauth)

    assert token.access_token == "refreshed-access"
    assert token.refresh_token == "original-refresh"
    assert oauth.refreshes == ["original-refresh"]


def test_exchange_wraps_provider_errors_without_exposing_details():
    redis = FakeRedis()
    store = RedisOAuthStateStore(redis, ttl_seconds=600)
    authorization = create_spotify_authorization(store, oauth=FakeOAuth())

    class BrokenOAuth(FakeOAuth):
        def get_access_token(self, code, *, as_dict=True, check_cache=False):
            raise RuntimeError("provider response with secret details")

    with pytest.raises(SpotifyProviderError) as error:
        exchange_spotify_code(
            "authorization-code",
            authorization.state,
            store,
            oauth=BrokenOAuth(),
        )

    assert str(error.value) == "Spotify token exchange failed"


def test_save_token_creates_then_updates_the_single_spotify_source(db):
    first = SpotifyToken(
        access_token="first-access",
        refresh_token="first-refresh",
        expires_at=datetime(2030, 1, 1),
    )
    source = save_spotify_token(db, first)
    source_id = source.id

    second = SpotifyToken(
        access_token="second-access",
        refresh_token=None,
        expires_at=datetime(2030, 2, 1),
    )
    updated = save_spotify_token(db, second, source=source)

    assert updated.id == source_id
    assert updated.access_token == "second-access"
    assert updated.refresh_token == "first-refresh"
    assert db.scalar(select(func.count()).select_from(PlaylistSource)) == 1


def test_import_paginates_maps_tracks_and_is_idempotent_by_snapshot(db):
    source = _source(db)
    spotify = FakeSpotify()

    first = import_spotify_playlists(
        db,
        source,
        spotify,
        normalizer=_simple_normalize,
    )

    assert first.to_dict() == {
        "discovered": 2,
        "created": 2,
        "updated": 0,
        "unchanged": 0,
        "tracks_imported": 3,
        "skipped_items": 2,
        "failed": 0,
        "errors": [],
    }
    assert spotify.playlist_calls == [(50, 0), (50, 1)]
    assert spotify.item_calls == [
        ("p1", 50, 0, ("track",)),
        ("p1", 50, 2, ("track",)),
        ("p2", 50, 0, ("track",)),
    ]

    p1 = db.scalar(
        select(Playlist).where(
            Playlist.source_id == source.id,
            Playlist.external_id == "p1",
        )
    )
    items = list(
        db.scalars(
            select(PlaylistItem)
            .where(PlaylistItem.playlist_id == p1.id)
            .order_by(PlaylistItem.position)
        )
    )
    assert p1.snapshot_hash == "snapshot-1"
    assert p1.track_count == 2
    assert [(item.position, item.external_track_id) for item in items] == [
        (0, "t1"),
        (1, None),
    ]
    assert items[0].artist_raw == "Artist A, Artist B"
    assert items[0].artist_norm == "artist a, artist b"
    assert items[0].isrc == "USAAA2400001"
    assert items[0].duration_ms == 123_000
    assert items[1].isrc is None

    item_calls_before_repeat = list(spotify.item_calls)
    second = import_spotify_playlists(
        db,
        source,
        spotify,
        normalizer=_simple_normalize,
    )

    assert second.discovered == 2
    assert second.unchanged == 2
    assert second.tracks_imported == 0
    assert spotify.item_calls == item_calls_before_repeat
    assert db.scalar(select(func.count()).select_from(Playlist)) == 2
    assert db.scalar(select(func.count()).select_from(PlaylistItem)) == 3


def test_import_uses_shared_normalizer_by_default(db):
    source = _source(db)
    spotify = FakeSpotify()
    spotify.changed_p1 = True

    import_spotify_playlists(db, source, spotify)

    item = db.scalar(
        select(PlaylistItem).join(Playlist).where(Playlist.external_id == "p1")
    )
    assert item.artist_norm == "new artist"
    assert item.title_norm == "new song"
    assert item.album_norm == "new album"


def test_changed_snapshot_replaces_items_without_duplicate_playlist(db):
    source = _source(db)
    spotify = FakeSpotify()
    import_spotify_playlists(db, source, spotify, normalizer=_simple_normalize)
    spotify.snapshots["p1"] = "snapshot-1b"
    spotify.changed_p1 = True

    summary = import_spotify_playlists(
        db,
        source,
        spotify,
        normalizer=_simple_normalize,
    )

    assert summary.updated == 1
    assert summary.unchanged == 1
    assert summary.tracks_imported == 1
    p1 = db.scalar(
        select(Playlist).where(
            Playlist.source_id == source.id,
            Playlist.external_id == "p1",
        )
    )
    items = list(
        db.scalars(select(PlaylistItem).where(PlaylistItem.playlist_id == p1.id))
    )
    assert p1.snapshot_hash == "snapshot-1b"
    assert p1.track_count == 1
    assert [(item.external_track_id, item.title_raw) for item in items] == [
        ("new-track", "New Song (Remaster)")
    ]
    assert db.scalar(select(func.count()).select_from(Playlist)) == 2


def test_single_playlist_refresh_uses_snapshot_and_target_id(db):
    source = _source(db)
    spotify = FakeSpotify()
    import_spotify_playlists(db, source, spotify, normalizer=_simple_normalize)
    playlist = db.scalar(select(Playlist).where(Playlist.external_id == "p1"))
    spotify.item_calls.clear()

    unchanged = refresh_spotify_playlist(
        db,
        playlist,
        spotify,
        normalizer=_simple_normalize,
    )
    assert unchanged.discovered == 1
    assert unchanged.unchanged == 1
    assert spotify.item_calls == []

    spotify.snapshots["p1"] = "snapshot-1c"
    spotify.changed_p1 = True
    changed = refresh_spotify_playlist(
        db,
        playlist,
        spotify,
        normalizer=_simple_normalize,
    )
    assert changed.updated == 1
    assert spotify.item_calls == [("p1", 50, 0, ("track",))]


def test_import_isolates_a_bad_playlist_and_keeps_successful_imports(db):
    source = _source(db)
    spotify = FakeSpotify()
    spotify.invalid_p2 = True

    summary = import_spotify_playlists(
        db,
        source,
        spotify,
        normalizer=_simple_normalize,
    )

    assert summary.failed == 1
    assert summary.errors == ["p2: SpotifyImportError"]
    assert db.scalar(select(func.count()).select_from(Playlist)) == 1
    assert db.scalar(select(func.count()).select_from(PlaylistItem)) == 2


def test_import_propagates_database_errors_for_celery_retry(db, monkeypatch):
    source = _source(db)
    spotify = FakeSpotify()

    def fail_database(*_args, **_kwargs):
        raise OperationalError("INSERT playlist", {}, RuntimeError("db down"))

    monkeypatch.setattr("app.services.spotify._upsert_playlist", fail_database)

    with pytest.raises(OperationalError):
        import_spotify_playlists(
            db,
            source,
            spotify,
            normalizer=_simple_normalize,
        )
