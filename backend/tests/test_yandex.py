from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models import Playlist, PlaylistItem, PlaylistSource, ServiceEnum
from app.services import yandex
from app.services.yandex import (
    YandexImportError,
    YandexTrackData,
    build_yandex_snapshot_hash,
    import_yandex_playlists,
    refresh_yandex_playlist,
)


def obj(**values):
    return SimpleNamespace(**values)


def track_ref(track_id: str, album_id: str = "album-1", *, embedded=None):
    return obj(id=track_id, album_id=album_id, track=embedded)


def full_track(
    track_id: str,
    *,
    title: str,
    artists: tuple[str, ...] = ("Artist",),
    album: str = "Album",
    album_id: str = "album-1",
    duration_ms: int = 180_000,
    version: str | None = None,
    isrc: str | None = None,
):
    return obj(
        id=track_id,
        title=title,
        version=version,
        artists=[obj(name=name) for name in artists],
        albums=[obj(id=album_id, title=album)],
        duration_ms=duration_ms,
        isrc=isrc,
    )


def playlist(
    kind: int,
    *,
    title: str,
    tracks=None,
    owner_id: int = 42,
    revision: int = 1,
    snapshot: int = 1,
):
    return obj(
        kind=kind,
        uid=owner_id,
        owner=obj(uid=owner_id),
        title=title,
        tracks=tracks,
        revision=revision,
        snapshot=snapshot,
        track_count=None if tracks is None else len(tracks),
    )


class FakeYandexClient:
    def __init__(self, summaries, details, tracks, *, broken_kinds=()):
        self.summaries = list(summaries)
        self.details = dict(details)
        self.track_map = dict(tracks)
        self.broken_kinds = set(broken_kinds)
        self.detail_calls = []
        self.track_calls = []

    def users_playlists_list(self):
        return self.summaries

    def users_playlists(self, kind, user_id=None):
        self.detail_calls.append((kind, user_id))
        if kind in self.broken_kinds:
            raise RuntimeError("remote details failed")
        return self.details[(str(user_id), int(kind))]

    def tracks(self, track_ids):
        ids = list(track_ids)
        self.track_calls.append(ids)
        return [self.track_map[item] for item in ids if item in self.track_map]


@pytest.fixture()
def yandex_source(db):
    source = PlaylistSource(service=ServiceEnum.yandex, access_token="secret-token")
    db.add(source)
    db.commit()
    return source


def test_import_is_idempotent_and_maps_tracks_in_batches(
    db, yandex_source, monkeypatch
):
    monkeypatch.setattr(yandex, "YANDEX_TRACK_BATCH_SIZE", 2)
    refs = [track_ref("1"), track_ref("2"), track_ref("3")]
    remote = playlist(7, title="Road Trip", tracks=refs, revision=5, snapshot=9)
    client = FakeYandexClient(
        [playlist(7, title="Road Trip")],
        {("42", 7): remote},
        {
            "1:album-1": full_track(
                "1",
                title="First (feat. Guest)",
                artists=("Main", "Guest"),
                duration_ms=111_000,
                isrc="us-aaa-24-00001",
            ),
            "2:album-1": full_track(
                "2", title="Second", album="Other Album", duration_ms=222_000
            ),
            "3:album-1": full_track(
                "3", title="Third", version="Live", duration_ms=333_000
            ),
        },
    )

    first = import_yandex_playlists(db, yandex_source, client=client)
    stored = db.scalar(select(Playlist))
    items = db.scalars(select(PlaylistItem).order_by(PlaylistItem.position)).all()

    assert first.as_dict() == {
        "imported": 1,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    assert stored.external_id == "42:7"
    assert stored.name == "Road Trip"
    assert stored.track_count == 3
    assert len(stored.snapshot_hash) == 64
    assert client.detail_calls == [(7, "42")]
    assert client.track_calls == [
        ["1:album-1", "2:album-1"],
        ["3:album-1"],
    ]
    assert [item.external_track_id for item in items] == [
        "1:album-1",
        "2:album-1",
        "3:album-1",
    ]
    assert items[0].artist_raw == "Main, Guest"
    assert items[0].title_raw == "First (feat. Guest)"
    assert items[0].artist_norm == "main, guest"
    assert items[0].title_norm == "first"
    assert items[0].album_raw == "Album"
    assert items[0].duration_ms == 111_000
    assert items[0].isrc == "USAAA2400001"
    assert items[2].title_raw == "Third (Live)"
    assert items[2].title_norm == "third"

    first_item_ids = [item.id for item in items]
    second = import_yandex_playlists(db, yandex_source, client=client)

    assert second.imported == 0
    assert second.updated == 0
    assert second.skipped == 1
    assert db.scalar(select(func.count()).select_from(Playlist)) == 1
    assert db.scalar(select(func.count()).select_from(PlaylistItem)) == 3
    assert (
        db.scalars(select(PlaylistItem.id).order_by(PlaylistItem.position)).all()
        == first_item_ids
    )


def test_summary_with_empty_track_placeholder_loads_playlist_details(
    db, yandex_source
):
    summary = playlist(9, title="Summary", tracks=[])
    summary.track_count = 2
    refs = [track_ref("1"), track_ref("2")]
    remote = playlist(9, title="Complete", tracks=refs)
    client = FakeYandexClient(
        [summary],
        {("42", 9): remote},
        {
            "1:album-1": full_track("1", title="First"),
            "2:album-1": full_track("2", title="Second"),
        },
    )

    result = import_yandex_playlists(db, yandex_source, client=client)

    assert result.imported == 1
    assert result.failed == 0
    assert client.detail_calls == [(9, "42")]
    assert db.scalar(select(Playlist.track_count)) == 2
    assert db.scalar(select(func.count()).select_from(PlaylistItem)) == 2


def test_changed_snapshot_replaces_items_and_updates_playlist(db, yandex_source):
    original = playlist(
        8,
        title="Old title",
        tracks=[track_ref("old")],
        revision=1,
        snapshot=1,
    )
    client = FakeYandexClient(
        [original],
        {},
        {"old:album-1": full_track("old", title="Old song")},
    )
    assert import_yandex_playlists(db, yandex_source, client=client).imported == 1
    old_hash = db.scalar(select(Playlist.snapshot_hash))

    changed = playlist(
        8,
        title="New title",
        tracks=[track_ref("new")],
        revision=2,
        snapshot=2,
    )
    client.summaries = [changed]
    client.track_map = {
        "new:album-1": full_track("new", title="New song", duration_ms=99_000)
    }
    summary = import_yandex_playlists(db, yandex_source, client=client)
    stored = db.scalar(select(Playlist))
    item = db.scalar(select(PlaylistItem))

    assert summary.updated == 1
    assert summary.imported == summary.skipped == summary.failed == 0
    assert stored.name == "New title"
    assert stored.snapshot_hash != old_hash
    assert stored.track_count == 1
    assert item.external_track_id == "new:album-1"
    assert item.title_raw == "New song"


def test_one_broken_playlist_does_not_rollback_successful_import(db, yandex_source):
    good = playlist(1, title="Good", tracks=[])
    broken = playlist(2, title="Broken", tracks=None)
    client = FakeYandexClient(
        [good, broken],
        {},
        {},
        broken_kinds={2},
    )

    summary = import_yandex_playlists(db, yandex_source, client=client)

    assert summary.imported == 1
    assert summary.failed == 1
    assert summary.errors == ["42:2: RuntimeError"]
    assert db.scalar(select(func.count()).select_from(Playlist)) == 1


def test_import_propagates_database_errors_for_celery_retry(
    db, yandex_source, monkeypatch
):
    remote = playlist(1, title="Database failure", tracks=[])
    client = FakeYandexClient([remote], {}, {})

    def fail_database(*_args, **_kwargs):
        raise OperationalError("INSERT playlist", {}, RuntimeError("db down"))

    monkeypatch.setattr(yandex, "_save_remote_playlist", fail_database)

    with pytest.raises(OperationalError):
        import_yandex_playlists(db, yandex_source, client=client)


def test_refresh_uses_stored_owner_and_kind(db, yandex_source):
    stored = Playlist(
        source_id=yandex_source.id,
        external_id="42:11",
        name="Before",
        snapshot_hash="old",
        track_count=0,
    )
    db.add(stored)
    db.commit()

    remote = playlist(
        11,
        title="After",
        tracks=[track_ref("11")],
        revision=3,
        snapshot=4,
    )
    client = FakeYandexClient(
        [],
        {("42", 11): remote},
        {"11:album-1": full_track("11", title="Refreshed")},
    )

    summary = refresh_yandex_playlist(db, stored, client=client)

    assert summary.updated == 1
    assert client.detail_calls == [(11, "42")]
    assert stored.name == "After"
    assert stored.items[0].title_raw == "Refreshed"


def test_snapshot_hash_is_deterministic_and_order_sensitive():
    track = YandexTrackData(
        position=0,
        artist_raw="Артист",
        title_raw="Песня",
        album_raw="Альбом",
        artist_norm="артист",
        title_norm="песня",
        album_norm="альбом",
        isrc=None,
        duration_ms=123,
        external_track_id="1:2",
    )
    arguments = {
        "external_id": "42:1",
        "name": "Плейлист",
        "revision": 3,
        "snapshot": 4,
        "tracks": [track, replace(track, position=1, external_track_id="2:2")],
    }

    first = build_yandex_snapshot_hash(**arguments)
    second = build_yandex_snapshot_hash(**arguments)
    reversed_hash = build_yandex_snapshot_hash(
        **{**arguments, "tracks": list(reversed(arguments["tracks"]))}
    )

    assert first == second
    assert len(first) == 64
    assert reversed_hash != first


def test_playlist_list_failure_raises_sanitized_import_error(db, yandex_source):
    class BrokenClient:
        def users_playlists_list(self):
            raise RuntimeError("secret provider response")

    with pytest.raises(YandexImportError, match="Could not list") as caught:
        import_yandex_playlists(db, yandex_source, client=BrokenClient())

    assert "secret provider response" not in str(caught.value)
