from __future__ import annotations

from dataclasses import dataclass, field

import httpx
from sqlalchemy import select

from app.models import Album, Artist, Track
from app.services.musicbrainz import MusicBrainzClient, enrich_album

RELEASE_MBID = "11111111-1111-1111-1111-111111111111"
ARTIST_MBID = "22222222-2222-2222-2222-222222222222"
RECORDING_MBID = "33333333-3333-3333-3333-333333333333"


@dataclass
class FakeClock:
    now: float = 0.0
    sleeps: list[float] = field(default_factory=list)

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeRedis:
    def __init__(self, clock: FakeClock):
        self.clock = clock
        self.values: dict[str, str] = {}
        self.expiries: dict[str, float] = {}

    def _purge(self, key: str) -> None:
        expiry = self.expiries.get(key)
        if expiry is not None and expiry <= self.clock.now:
            self.values.pop(key, None)
            self.expiries.pop(key, None)

    def get(self, key: str):
        self._purge(key)
        return self.values.get(key)

    def set(self, key: str, value: str, *, nx=False, px=None):
        self._purge(key)
        if nx and key in self.values:
            return False
        self.values[key] = value
        if px is not None:
            self.expiries[key] = self.clock.now + (px / 1000)
        return True

    def pttl(self, key: str) -> int:
        self._purge(key)
        if key not in self.values:
            return -2
        expiry = self.expiries.get(key)
        return -1 if expiry is None else max(0, round((expiry - self.clock.now) * 1000))

    def setex(self, key: str, ttl: int, value: str):
        self.values[key] = value
        self.expiries[key] = self.clock.now + ttl
        return True

    def delete(self, key: str):
        self.values.pop(key, None)
        self.expiries.pop(key, None)


def _search_response(releases=None):
    return {
        "releases": releases
        if releases is not None
        else [
            {
                "id": RELEASE_MBID,
                "title": "Test Album",
                "score": 100,
                "date": "2024-01-01",
                "track-count": 1,
                "artist-credit": [{"name": "Test Artist"}],
            }
        ]
    }


def _release_response():
    return {
        "id": RELEASE_MBID,
        "artist-credit": [{"artist": {"id": ARTIST_MBID, "name": "Test Artist"}}],
        "media": [
            {
                "position": 1,
                "tracks": [
                    {
                        "position": 1,
                        "recording": {
                            "id": RECORDING_MBID,
                            "title": "Test Track",
                            "length": 120000,
                            "isrcs": ["US-AAA-24-00001"],
                        },
                    }
                ],
            }
        ],
    }


def _client(handler, clock, redis, *, max_retries=0):
    return MusicBrainzClient(
        redis_client=redis,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        base_url="https://musicbrainz.test/ws/2",
        user_agent="MusicServiceTests/1.0 (tests@example.invalid)",
        sleeper=clock.sleep,
        max_retries=max_retries,
    )


def test_release_calls_are_cached_and_globally_rate_limited():
    clock = FakeClock()
    redis = FakeRedis(clock)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json=_search_response())
        return httpx.Response(200, json=_release_response())

    client = _client(handler, clock, redis)
    candidate = client.find_release(
        "Test Artist", "Test Album", year=2024, track_count=1
    )
    detail = client.lookup_release(candidate.mbid)
    cached_candidate = client.find_release("Test Artist", "Test Album")
    cached_detail = client.lookup_release(candidate.mbid)

    assert detail == cached_detail
    assert cached_candidate.mbid == RELEASE_MBID
    assert len(requests) == 2
    assert sum(clock.sleeps) >= 1.0
    assert all(
        request.headers["user-agent"].startswith("MusicServiceTests/")
        for request in requests
    )
    assert all(request.headers["accept"] == "application/json" for request in requests)
    assert requests[0].url.params["fmt"] == "json"


def test_negative_search_result_is_cached():
    clock = FakeClock()
    redis = FakeRedis(clock)
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_search_response([]))

    client = _client(handler, clock, redis)
    assert client.find_release("Nobody", "Nothing") is None
    assert client.find_release("Nobody", "Nothing") is None
    assert calls == 1


def test_retry_after_is_honored_for_throttled_request():
    clock = FakeClock()
    redis = FakeRedis(clock)
    calls = 0

    def handler(_request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(429, headers={"Retry-After": "3"})
        return httpx.Response(200, json=_search_response([]))

    client = _client(handler, clock, redis, max_retries=1)
    assert client.find_release("Nobody", "Nothing") is None
    assert calls == 2
    assert sum(clock.sleeps) >= 3


def test_ambiguous_release_is_not_selected():
    clock = FakeClock()
    redis = FakeRedis(clock)
    duplicate = _search_response()["releases"][0]

    def handler(_request: httpx.Request):
        return httpx.Response(
            200,
            json=_search_response(
                [duplicate, {**duplicate, "id": "44444444-4444-4444-4444-444444444444"}]
            ),
        )

    client = _client(handler, clock, redis)
    assert client.find_release("Test Artist", "Test Album") is None


def test_enrich_album_writes_release_recording_and_isrc(db):
    artist = Artist(name="Test Artist", name_norm="test artist")
    album = Album(
        artist=artist,
        title="Test Album",
        title_norm="test album",
        year=2024,
    )
    track = Track(
        album=album,
        title="Test Track",
        title_norm="test track",
        track_no=1,
        disc_no=1,
        duration_ms=120000,
    )
    db.add_all([artist, album, track])
    db.commit()

    clock = FakeClock()
    redis = FakeRedis(clock)
    calls: list[str] = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json=_search_response())
        return httpx.Response(200, json=_release_response())

    client = _client(handler, clock, redis)
    result = enrich_album(db, album.id, client)
    repeated = enrich_album(db, album.id, client)
    enriched_album = db.scalar(select(Album).where(Album.id == album.id))
    enriched_track = db.scalar(select(Track).where(Track.id == track.id))

    assert result == {"album_id": album.id, "status": "enriched", "tracks": 1}
    assert repeated == result
    assert len(calls) == 2
    assert enriched_album.mbid == RELEASE_MBID
    assert enriched_album.artist.mbid == ARTIST_MBID
    assert enriched_track.mbid == RECORDING_MBID
    assert enriched_track.isrc == "USAAA2400001"


def test_enrich_album_does_not_mix_conflicting_isrc_and_recording(db):
    artist = Artist(name="Test Artist", name_norm="test artist")
    album = Album(
        artist=artist,
        title="Test Album",
        title_norm="test album",
        year=2024,
    )
    track = Track(
        album=album,
        title="Test Track",
        title_norm="test track",
        track_no=1,
        disc_no=1,
        duration_ms=120000,
        isrc="GBBBB2500002",
    )
    db.add_all([artist, album, track])
    db.commit()
    clock = FakeClock()
    redis = FakeRedis(clock)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/release"):
            return httpx.Response(200, json=_search_response())
        return httpx.Response(200, json=_release_response())

    result = enrich_album(db, album.id, _client(handler, clock, redis))
    db.refresh(track)

    assert result["tracks"] == 0
    assert track.mbid is None
    assert track.isrc == "GBBBB2500002"
