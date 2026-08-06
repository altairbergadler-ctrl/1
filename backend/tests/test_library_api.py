from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from app.config import settings
from app.models import Album, Artist, File, Job, JobStatus, Track, utcnow


def _populate_library(db):
    artist = Artist(name="API Artist", name_norm="api artist")
    album = Album(
        artist=artist,
        title="API Album",
        title_norm="api album",
        year=2026,
    )
    first = Track(album=album, title="One", title_norm="one", track_no=1)
    second = Track(album=album, title="Two", title_norm="two", track_no=2)
    db.add_all([artist, album, first, second])
    db.flush()
    db.add_all(
        [
            File(
                track=first,
                path="/music/library/api-one.flac",
                format="flac",
                size_bytes=100,
                sha1="a" * 40,
            ),
            File(
                track=second,
                path="/music/library/api-two.wav",
                format="wav",
                size_bytes=250,
                sha1="b" * 40,
            ),
        ]
    )
    db.commit()


def test_stats_and_album_search(api_client, auth_headers, db):
    _populate_library(db)

    stats = api_client.get("/api/library/stats", headers=auth_headers)
    albums = api_client.get(
        "/api/library/albums", params={"q": "API Artist"}, headers=auth_headers
    )

    assert stats.status_code == 200
    assert stats.json() == {
        "files": 2,
        "tracks": 2,
        "albums": 1,
        "bytes": 350,
        "formats": {
            "flac": {"files": 1, "bytes": 100},
            "wav": {"files": 1, "bytes": 250},
        },
    }
    assert albums.status_code == 200
    assert albums.json()["total"] == 1
    assert albums.json()["items"][0]["title"] == "API Album"
    assert albums.json()["items"][0]["artist"]["name"] == "API Artist"
    assert albums.json()["items"][0]["tracks"] == 2


def test_scan_creates_one_pending_job_and_jobs_endpoint_works(
    api_client, auth_headers, db, monkeypatch
):
    queued: list[int] = []
    monkeypatch.setattr(
        "app.api.library.scan_library_task.delay", lambda job_id: queued.append(job_id)
    )

    response = api_client.post("/api/library/scan", headers=auth_headers)
    repeated = api_client.post("/api/library/scan", headers=auth_headers)

    assert response.status_code == 202
    job_id = response.json()["id"]
    assert response.json()["status"] == "pending"
    assert queued == [job_id]
    assert repeated.status_code == 202
    assert repeated.json()["id"] == job_id
    assert db.scalar(select(func.count(Job.id))) == 1

    job_response = api_client.get(f"/api/jobs/{job_id}", headers=auth_headers)
    assert job_response.status_code == 200
    assert job_response.json()["payload"]["path"]


def test_library_requires_auth(api_client):
    assert api_client.get("/api/library/stats").status_code == 401
    assert api_client.get("/api/library/albums").status_code == 401
    assert api_client.post("/api/library/scan").status_code == 401


def test_stale_heartbeat_is_failed_before_a_replacement_job_is_queued(
    api_client, auth_headers, db, monkeypatch
):
    stale = Job(
        type="scan_library",
        status=JobStatus.running,
        payload="{}",
        heartbeat_at=utcnow() - timedelta(seconds=settings.scan_job_stale_seconds + 1),
    )
    db.add(stale)
    db.commit()
    queued: list[int] = []
    monkeypatch.setattr(
        "app.api.library.scan_library_task.delay", lambda job_id: queued.append(job_id)
    )

    response = api_client.post("/api/library/scan", headers=auth_headers)

    db.refresh(stale)
    assert response.status_code == 202
    assert response.json()["id"] != stale.id
    assert stale.status == JobStatus.failed
    assert stale.finished_at is not None
    assert queued == [response.json()["id"]]


def test_fresh_heartbeat_is_not_replaced(api_client, auth_headers, db, monkeypatch):
    active = Job(
        type="scan_library",
        status=JobStatus.running,
        payload="{}",
        heartbeat_at=utcnow(),
        lock_owner="active-task",
    )
    db.add(active)
    db.commit()
    queued: list[int] = []
    monkeypatch.setattr(
        "app.api.library.scan_library_task.delay", lambda job_id: queued.append(job_id)
    )

    response = api_client.post("/api/library/scan", headers=auth_headers)

    db.refresh(active)
    assert response.status_code == 202
    assert response.json()["id"] == active.id
    assert active.status == JobStatus.running
    assert active.lock_owner == "active-task"
    assert queued == []
