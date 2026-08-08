from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models import (
    Album,
    Artist,
    File,
    Job,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    Track,
)
from app.services.matcher import run_matching
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.workers.tasks import run_matching_task


def _seed_review_case(db, tmp_path: Path):
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(
        source=source,
        external_id="review-playlist",
        name="Review Playlist",
        snapshot_hash="review-snapshot",
    )
    item = PlaylistItem(
        playlist=playlist,
        position=0,
        artist_raw="The Example Band",
        title_raw="Northern Lights",
        album_raw="Playlist Album",
        artist_norm=normalize_artist("The Example Band"),
        title_norm=normalize_title("Northern Lights"),
        album_norm=normalize_album("Playlist Album"),
        duration_ms=201_000,
    )
    db.add_all([source, playlist, item])
    for index, (artist_name, title, duration) in enumerate(
        [
            ("Example Band", "The Northern Lights", 200_000),
            ("The Example Band", "Northern Lights Extended", 202_000),
        ],
        start=1,
    ):
        artist = Artist(
            name=artist_name,
            name_norm=normalize_artist(artist_name),
        )
        album = Album(
            artist=artist,
            title=f"Catalog Album {index}",
            title_norm=normalize_album(f"Catalog Album {index}"),
        )
        track = Track(
            album=album,
            title=title,
            title_norm=normalize_title(title),
            duration_ms=duration,
        )
        db.add(track)
        db.flush()
        path = tmp_path / f"candidate-{index}.flac"
        path.write_bytes(f"candidate-{index}".encode())
        db.add(
            File(
                track=track,
                path=str(path),
                format="flac",
                bit_depth=24 if index == 1 else 16,
                sample_rate=96_000 if index == 1 else 44_100,
                size_bytes=path.stat().st_size,
                sha1=f"{index:040x}",
            )
        )
    db.commit()
    run_matching(db, playlist.id)
    db.refresh(item)
    return playlist, item


def test_run_endpoint_queues_one_background_job(
    api_client, auth_headers, db, monkeypatch
):
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(
        source=source,
        external_id="run-playlist",
        name="Run Playlist",
    )
    db.add_all([source, playlist])
    db.commit()
    queued: list[tuple[int, int | None]] = []
    monkeypatch.setattr(
        "app.api.matching.run_matching_task.delay",
        lambda job_id, playlist_id=None: queued.append((job_id, playlist_id)),
    )

    first = api_client.post(
        "/api/matching/run",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )
    duplicate = api_client.post(
        "/api/matching/run",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == first.json()["id"]
    assert first.json()["type"] == "run_matching"
    assert queued == [(first.json()["id"], playlist.id)]


def test_database_rejects_two_active_matching_jobs(db):
    db.add(
        Job(
            type="run_matching",
            status=JobStatus.pending,
            payload='{"playlist_id":null}',
        )
    )
    db.commit()
    db.add(
        Job(
            type="run_matching",
            status=JobStatus.running,
            payload='{"playlist_id":null}',
        )
    )

    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_review_endpoint_and_manual_resolve(api_client, auth_headers, db, tmp_path):
    playlist, item = _seed_review_case(db, tmp_path)

    review = api_client.get("/api/matching/review", headers=auth_headers)

    assert review.status_code == 200
    assert review.json()["total"] == 1
    review_item = review.json()["items"][0]
    assert review_item["match_id"] == item.match.id
    assert review_item["playlist_id"] == playlist.id
    assert len(review_item["candidates"]) == 2
    candidate_id = review_item["candidates"][0]["track_id"]

    resolved = api_client.post(
        f"/api/matching/{item.match.id}/resolve",
        json={"track_id": candidate_id},
        headers=auth_headers,
    )

    assert resolved.status_code == 200
    assert resolved.json() == {
        "match_id": item.match.id,
        "playlist_item_id": item.id,
        "track_id": candidate_id,
        "confidence": 1.0,
        "method": "manual",
        "status": "READY",
    }
    db.expire_all()
    run_matching(db, playlist.id)
    assert db.get(Match, item.match.id).track_id == candidate_id
    assert db.get(Match, item.match.id).method == "manual"


def test_review_can_be_resolved_as_missing(api_client, auth_headers, db, tmp_path):
    _playlist, item = _seed_review_case(db, tmp_path)

    response = api_client.post(
        f"/api/matching/{item.match.id}/resolve",
        json={"track_id": None},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["status"] == "MISSING"
    assert response.json()["method"] == "manual_missing"
    assert response.json()["track_id"] is None


def test_resolve_rejects_a_track_outside_the_candidate_set(
    api_client, auth_headers, db, tmp_path
):
    _playlist, item = _seed_review_case(db, tmp_path)
    unrelated_artist = Artist(name="Other", name_norm="other")
    unrelated_album = Album(
        artist=unrelated_artist,
        title="Other",
        title_norm="other",
    )
    unrelated = Track(
        album=unrelated_album,
        title="Other",
        title_norm="other",
    )
    db.add(unrelated)
    db.flush()
    unrelated_path = tmp_path / "unrelated.flac"
    unrelated_path.write_bytes(b"unrelated")
    db.add(
        File(
            track=unrelated,
            path=str(unrelated_path),
            format="flac",
            size_bytes=unrelated_path.stat().st_size,
            sha1="f" * 40,
        )
    )
    db.commit()

    response = api_client.post(
        f"/api/matching/{item.match.id}/resolve",
        json={"track_id": unrelated.id},
        headers=auth_headers,
    )

    assert response.status_code == 422


def test_matching_task_updates_job_lifecycle(session_factory, tmp_path, monkeypatch):
    seed = session_factory()
    playlist, _item = _seed_review_case(seed, tmp_path)
    job = Job(
        type="run_matching",
        status=JobStatus.pending,
        payload=f'{{"playlist_id":{playlist.id}}}',
    )
    seed.add(job)
    seed.commit()
    job_id = job.id
    playlist_id = playlist.id
    seed.close()
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)

    result = run_matching_task.run(job_id, playlist_id)

    check = session_factory()
    finished = check.get(Job, job_id)
    assert finished.status == JobStatus.done
    assert finished.finished_at is not None
    assert finished.lock_owner is None
    assert result["phase"] == "completed"
    assert result["matching"]["total"] == 1
    assert check.scalar(
        select(Match.status).join(PlaylistItem).where(PlaylistItem.playlist_id == playlist_id)
    ) == MatchStatus.needs_review
    check.close()


def test_matching_endpoints_require_auth_and_validate_ids(
    api_client, auth_headers, monkeypatch
):
    assert api_client.get("/api/matching/review").status_code == 401
    assert (
        api_client.post(
            "/api/matching/run",
            json={"playlist_id": 999},
            headers=auth_headers,
        ).status_code
        == 404
    )
    assert (
        api_client.post(
            "/api/matching/999/resolve",
            json={"track_id": None},
            headers=auth_headers,
        ).status_code
        == 404
    )
