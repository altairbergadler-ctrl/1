import json
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.models import (
    Job,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    utcnow,
)


def _seed_playlist(db):
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(
        source=source,
        external_id="spotify-playlist-1",
        name="API Playlist",
        snapshot_hash="snapshot-1",
        track_count=4,
    )
    items = [
        PlaylistItem(
            playlist=playlist,
            position=position,
            artist_raw=f"Artist {position}",
            title_raw=f"Title {position}",
            album_raw="Album",
            artist_norm=f"artist {position}",
            title_norm=f"title {position}",
            album_norm="album",
            external_track_id=f"track-{position}",
        )
        for position in range(4)
    ]
    db.add_all([source, playlist, *items])
    db.flush()
    db.add_all(
        [
            Match(
                playlist_item_id=items[0].id,
                confidence=1.0,
                method="isrc",
                status=MatchStatus.ready,
            ),
            Match(
                playlist_item_id=items[1].id,
                confidence=0.85,
                method="fuzzy",
                status=MatchStatus.needs_review,
            ),
            Match(
                playlist_item_id=items[2].id,
                status=MatchStatus.missing,
            ),
        ]
    )
    db.commit()
    return source, playlist


def test_playlist_list_detail_and_status_filter(api_client, auth_headers, db):
    _source, playlist = _seed_playlist(db)

    listing = api_client.get("/api/playlists", headers=auth_headers)
    detail = api_client.get(f"/api/playlists/{playlist.id}", headers=auth_headers)
    unmatched = api_client.get(
        f"/api/playlists/{playlist.id}/items",
        params={"status": "UNMATCHED"},
        headers=auth_headers,
    )
    ready = api_client.get(
        f"/api/playlists/{playlist.id}/items",
        params={"status": "READY"},
        headers=auth_headers,
    )

    assert listing.status_code == 200
    assert listing.json()["total"] == 1
    item = listing.json()["items"][0]
    assert item["service"] == "spotify"
    assert item["track_count"] == 4
    assert item["summary"] == {
        "ready": 1,
        "missing": 1,
        "review": 1,
        "unmatched": 1,
        "collected_percent": 25.0,
    }
    assert detail.status_code == 200
    assert detail.json()["snapshot_hash"] == "snapshot-1"
    assert unmatched.status_code == 200
    assert unmatched.json()["total"] == 1
    assert unmatched.json()["items"][0]["status"] == "UNMATCHED"
    assert ready.status_code == 200
    assert ready.json()["total"] == 1
    assert ready.json()["items"][0]["status"] == "READY"


def test_import_and_refresh_reuse_active_source_job(
    api_client, auth_headers, db, monkeypatch
):
    source, playlist = _seed_playlist(db)
    queued: list[tuple[int, int, int | None]] = []
    monkeypatch.setattr(
        "app.api.playlists.import_playlists_task.delay",
        lambda job_id, source_id, playlist_id=None: queued.append(
            (job_id, source_id, playlist_id)
        ),
    )

    imported = api_client.post(
        "/api/playlists/import",
        json={"source_id": source.id},
        headers=auth_headers,
    )
    refreshed = api_client.post(
        f"/api/playlists/{playlist.id}/refresh",
        headers=auth_headers,
    )

    assert imported.status_code == 202
    assert refreshed.status_code == 202
    assert imported.json()["status"] == "pending"
    assert refreshed.json()["status"] == "pending"
    assert refreshed.json()["id"] == imported.json()["id"]
    assert queued == [(imported.json()["id"], source.id, None)]
    jobs = list(db.scalars(select(Job)))
    assert len(jobs) == 1
    assert jobs[0].source_id == source.id


def test_terminal_import_allows_new_refresh_job(
    api_client, auth_headers, db, monkeypatch
):
    source, playlist = _seed_playlist(db)
    queued: list[tuple[int, int, int | None]] = []
    monkeypatch.setattr(
        "app.api.playlists.import_playlists_task.delay",
        lambda job_id, source_id, playlist_id=None: queued.append(
            (job_id, source_id, playlist_id)
        ),
    )

    imported = api_client.post(
        "/api/playlists/import",
        json={"source_id": source.id},
        headers=auth_headers,
    )
    first_job = db.get(Job, imported.json()["id"])
    first_job.status = JobStatus.done
    first_job.finished_at = utcnow()
    db.commit()

    refreshed = api_client.post(
        f"/api/playlists/{playlist.id}/refresh",
        headers=auth_headers,
    )

    assert refreshed.status_code == 202
    assert refreshed.json()["id"] != first_job.id
    assert queued == [
        (first_job.id, source.id, None),
        (refreshed.json()["id"], source.id, playlist.id),
    ]
    assert len(db.scalars(select(Job)).all()) == 2


def test_incompatible_active_refresh_returns_conflict(
    api_client, auth_headers, db, monkeypatch
):
    source, first_playlist = _seed_playlist(db)
    second_playlist = Playlist(
        source_id=source.id,
        external_id="spotify-playlist-2",
        name="Second playlist",
        snapshot_hash="snapshot-2",
    )
    db.add(second_playlist)
    db.commit()
    queued: list[tuple[int, int, int | None]] = []
    monkeypatch.setattr(
        "app.api.playlists.import_playlists_task.delay",
        lambda job_id, source_id, playlist_id=None: queued.append(
            (job_id, source_id, playlist_id)
        ),
    )

    first = api_client.post(
        f"/api/playlists/{first_playlist.id}/refresh",
        headers=auth_headers,
    )
    other_refresh = api_client.post(
        f"/api/playlists/{second_playlist.id}/refresh",
        headers=auth_headers,
    )
    full_import = api_client.post(
        "/api/playlists/import",
        json={"source_id": source.id},
        headers=auth_headers,
    )
    same_refresh = api_client.post(
        f"/api/playlists/{first_playlist.id}/refresh",
        headers=auth_headers,
    )

    assert first.status_code == 202
    assert other_refresh.status_code == 409
    assert full_import.status_code == 409
    assert same_refresh.status_code == 202
    assert same_refresh.json()["id"] == first.json()["id"]
    assert other_refresh.json()["detail"]["job_id"] == first.json()["id"]
    assert full_import.json()["detail"]["job_id"] == first.json()["id"]
    assert queued == [(first.json()["id"], source.id, first_playlist.id)]
    assert len(db.scalars(select(Job)).all()) == 1


def test_stale_import_is_revoked_before_replacement_is_queued(
    api_client, auth_headers, db, monkeypatch
):
    source, playlist = _seed_playlist(db)
    stale = Job(
        type="import_playlists",
        source_id=source.id,
        status=JobStatus.running,
        payload=json.dumps({"source_id": source.id}),
        heartbeat_at=utcnow()
        - timedelta(seconds=settings.playlist_import_job_stale_seconds + 1),
        lock_owner="stale-worker",
    )
    db.add(stale)
    db.commit()
    queued: list[tuple[int, int, int | None]] = []
    monkeypatch.setattr(
        "app.api.playlists.import_playlists_task.delay",
        lambda job_id, source_id, playlist_id=None: queued.append(
            (job_id, source_id, playlist_id)
        ),
    )

    response = api_client.post(
        f"/api/playlists/{playlist.id}/refresh",
        headers=auth_headers,
    )

    db.refresh(stale)
    assert response.status_code == 202
    assert response.json()["id"] != stale.id
    assert stale.status == JobStatus.failed
    assert stale.error == "Playlist import job expired before completion"
    assert stale.lock_owner is None
    assert stale.finished_at is not None
    assert queued == [(response.json()["id"], source.id, playlist.id)]


def test_database_rejects_two_active_import_jobs_for_one_source(db):
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    db.add(source)
    db.flush()
    first = Job(
        type="import_playlists",
        source_id=source.id,
        status=JobStatus.pending,
        payload=json.dumps({"source_id": source.id}),
    )
    db.add(first)
    db.commit()

    db.add(
        Job(
            type="import_playlists",
            source_id=source.id,
            status=JobStatus.running,
            payload=json.dumps({"source_id": source.id}),
        )
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_playlist_endpoints_validate_auth_and_ids(api_client, auth_headers, db):
    assert api_client.get("/api/playlists").status_code == 401
    assert (
        api_client.post(
            "/api/playlists/import",
            json={"source_id": 999},
            headers=auth_headers,
        ).status_code
        == 404
    )
    assert api_client.get("/api/playlists/999", headers=auth_headers).status_code == 404
    assert (
        api_client.post(
            "/api/playlists/999/refresh",
            headers=auth_headers,
        ).status_code
        == 404
    )
