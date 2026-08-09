from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import (
    Job,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
)
from app.services import qobuz as qobuz_service
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    QobuzSearchCandidate,
    import_files_to_library,
    select_best_track_candidate,
    search_albums,
    search_tracks,
    verify_staging_files,
)
from app.workers.tasks import qobuz_download_task


def _configure_qobuz(monkeypatch, *, enabled=True, email="user@example.com", password="secret"):
    monkeypatch.setattr("app.config.settings.qobuz_enabled", enabled)
    monkeypatch.setattr("app.config.settings.qobuz_email", email)
    monkeypatch.setattr("app.config.settings.qobuz_password", password)


class FakeQobuzClient:
    label = "Studio"

    def __init__(self, tracks=None, albums=None):
        self._tracks = list(tracks or [])
        self._albums = list(albums or [])

    def search_tracks(self, query, limit):
        return {"tracks": {"items": self._tracks[:limit]}}

    def search_albums(self, query, limit):
        return {"albums": {"items": self._albums[:limit]}}


def _track_payload(track_id, title, artist="Tagged Artist", duration=187, album="Tagged Album"):
    return {
        "id": track_id,
        "title": title,
        "duration": duration,
        "isrc": "USAAA2400001",
        "hires_streamable": True,
        "performer": {"name": artist},
        "album": {"title": album},
    }


def _write_flac(ffmpeg: str, path: Path, *, frequency: int, metadata: dict[str, str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:duration=0.12",
        "-ar",
        "44100",
        "-c:a",
        "flac",
    ]
    for key, value in metadata.items():
        command.extend(["-metadata", f"{key}={value}"])
    command.append(str(path))
    subprocess.run(command, check=True, capture_output=True, text=True)


# --- status / connect ---------------------------------------------------------


def test_status_reports_disabled_and_unconfigured(api_client, auth_headers):
    response = api_client.get("/api/qobuz/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "enabled": False,
        "configured": False,
        "quality": 27,
        "max_tracks_per_run": 25,
    }


def test_status_reports_configured_without_leaking_secrets(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)

    response = api_client.get("/api/qobuz/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["enabled"] is True
    assert body["configured"] is True
    assert "password" not in json.dumps(body).lower()
    assert "secret" not in body.values()


def test_connect_success_returns_label(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch)
    monkeypatch.setattr(
        "app.api.qobuz.create_qobuz_client", lambda: FakeQobuzClient()
    )

    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"connected": True, "label": "Studio"}


def test_connect_rejected_credentials_returns_400(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)

    def raise_auth():
        raise QobuzAuthError("rejected")

    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", raise_auth)
    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 400


def test_connect_unconfigured_returns_503(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch, enabled=False)
    monkeypatch.setattr(
        "app.api.qobuz.create_qobuz_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    response = api_client.post("/api/qobuz/connect", headers=auth_headers)

    assert response.status_code == 503


def test_qobuz_endpoints_require_auth(api_client):
    assert api_client.get("/api/qobuz/status").status_code == 401
    assert api_client.post("/api/qobuz/connect").status_code == 401
    assert api_client.get("/api/qobuz/search?q=x").status_code == 401
    assert api_client.post("/api/qobuz/download-url", json={"url": "u"}).status_code == 401
    assert (
        api_client.post("/api/qobuz/fetch-missing", json={"playlist_id": 1}).status_code
        == 401
    )


# --- search service mapping -----------------------------------------------------


def test_search_tracks_maps_qobuz_response():
    client = FakeQobuzClient(tracks=[_track_payload(5966783, "First Track")])

    candidates = search_tracks(client, "tagged first", limit=5)

    assert candidates == [
        QobuzSearchCandidate(
            qobuz_id="5966783",
            artist="Tagged Artist",
            title="First Track",
            album="Tagged Album",
            duration_ms=187_000,
            isrc="USAAA2400001",
            hires=True,
            url="https://play.qobuz.com/track/5966783",
        )
    ]


def test_search_albums_maps_qobuz_response():
    client = FakeQobuzClient(
        albums=[
            {
                "id": "abc123",
                "title": "Tagged Album",
                "duration": 3600,
                "hires_streamable": False,
                "artist": {"name": "Tagged Artist"},
            }
        ]
    )

    candidates = search_albums(client, "tagged", limit=5)

    assert len(candidates) == 1
    album = candidates[0]
    assert album.qobuz_id == "abc123"
    assert album.title == "Tagged Album"
    assert album.artist == "Tagged Artist"
    assert album.url == "https://play.qobuz.com/album/abc123"
    assert album.hires is False
    assert album.duration_ms == 3_600_000


def test_search_endpoint_maps_items(api_client, auth_headers, monkeypatch):
    _configure_qobuz(monkeypatch)
    client = FakeQobuzClient(tracks=[_track_payload(42, "First Track")])
    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", lambda: client)

    response = api_client.get(
        "/api/qobuz/search?q=tagged&type=track&limit=5", headers=auth_headers
    )

    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "track"
    assert items[0]["qobuz_id"] == "42"
    assert items[0]["hires"] is True


def test_search_endpoint_provider_error_returns_502(
    api_client, auth_headers, monkeypatch
):
    _configure_qobuz(monkeypatch)

    class BrokenClient(FakeQobuzClient):
        def search_tracks(self, query, limit):
            raise RuntimeError("network down")

    monkeypatch.setattr("app.api.qobuz.create_qobuz_client", lambda: BrokenClient())
    response = api_client.get("/api/qobuz/search?q=x", headers=auth_headers)

    assert response.status_code == 502


# --- candidate selection --------------------------------------------------------


def _candidate(title, artist="Tagged Artist", duration_ms=187_000):
    return QobuzSearchCandidate(
        qobuz_id="1",
        artist=artist,
        title=title,
        album="Tagged Album",
        duration_ms=duration_ms,
        isrc=None,
        hires=True,
        url="https://play.qobuz.com/track/1",
    )


def test_select_best_track_candidate_picks_best_fuzzy_match():
    best = select_best_track_candidate(
        artist_raw="The Tagged Artist",
        title_raw="First Track",
        duration_ms=None,
        candidates=[_candidate("Totally Different Song"), _candidate("First Track")],
    )

    assert best is not None
    assert best.title == "First Track"


def test_select_best_track_candidate_enforces_threshold():
    best = select_best_track_candidate(
        artist_raw="Unknown Artist",
        title_raw="Unknown Title",
        duration_ms=None,
        candidates=[_candidate("First Track")],
    )

    assert best is None


def test_select_best_track_candidate_enforces_duration_tolerance():
    within = _candidate("First Track", duration_ms=190_000)
    outside = _candidate("First Track", duration_ms=200_000)

    best = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        duration_ms=187_000,
        candidates=[outside, within],
    )

    assert best is within

    none_found = select_best_track_candidate(
        artist_raw="Tagged Artist",
        title_raw="First Track",
        duration_ms=187_000,
        candidates=[outside],
    )
    assert none_found is None


# --- job creation -----------------------------------------------------------------


def test_download_url_creates_single_active_job(
    api_client, auth_headers, db, monkeypatch
):
    _configure_qobuz(monkeypatch)
    queued: list[tuple] = []
    monkeypatch.setattr(
        "app.api.qobuz.qobuz_download_task.delay",
        lambda *args: queued.append(args),
    )

    url = "https://play.qobuz.com/track/123"
    first = api_client.post(
        "/api/qobuz/download-url", json={"url": url}, headers=auth_headers
    )
    duplicate = api_client.post(
        "/api/qobuz/download-url", json={"url": url}, headers=auth_headers
    )

    assert first.status_code == 202
    assert duplicate.status_code == 202
    assert duplicate.json()["id"] == first.json()["id"]
    assert len(queued) == 1
    job_id, mode, playlist_id, queued_url = queued[0]
    assert (job_id, mode, playlist_id, queued_url) == (
        first.json()["id"],
        "url",
        None,
        url,
    )
    job = db.get(Job, first.json()["id"])
    assert job.type == "qobuz_download"
    assert json.loads(job.payload)["mode"] == "url"


def test_fetch_missing_validates_playlist_and_reuses_active_job(
    api_client, auth_headers, db, monkeypatch
):
    _configure_qobuz(monkeypatch)
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(source=source, external_id="p1", name="Playlist")
    db.add_all([source, playlist])
    db.commit()
    queued: list[tuple] = []
    monkeypatch.setattr(
        "app.api.qobuz.qobuz_download_task.delay",
        lambda *args: queued.append(args),
    )

    missing_playlist = api_client.post(
        "/api/qobuz/fetch-missing", json={"playlist_id": 9999}, headers=auth_headers
    )
    first = api_client.post(
        "/api/qobuz/fetch-missing",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )
    duplicate = api_client.post(
        "/api/qobuz/fetch-missing",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )

    assert missing_playlist.status_code == 404
    assert first.status_code == 202
    assert duplicate.json()["id"] == first.json()["id"]
    assert len(queued) == 1
    assert queued[0][1:] == ("fetch_missing", playlist.id, None)


def test_download_endpoints_require_configuration(
    api_client, auth_headers, db, monkeypatch
):
    _configure_qobuz(monkeypatch, enabled=False)
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(source=source, external_id="p1", name="Playlist")
    db.add_all([source, playlist])
    db.commit()

    download = api_client.post(
        "/api/qobuz/download-url",
        json={"url": "https://play.qobuz.com/track/1"},
        headers=auth_headers,
    )
    fetch = api_client.post(
        "/api/qobuz/fetch-missing",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )

    assert download.status_code == 503
    assert fetch.status_code == 503


# --- staging verification and library import -------------------------------------


def test_verify_staging_files_rejects_broken_and_non_audio(tmp_path, ffmpeg_binary):
    valid = tmp_path / "valid.flac"
    _write_flac(
        ffmpeg_binary,
        valid,
        frequency=440,
        metadata={"artist": "A", "title": "T"},
    )
    broken = tmp_path / "broken.flac"
    broken.write_bytes(b"this is not flac content")
    empty = tmp_path / "empty.mp3"
    empty.touch()
    cover = tmp_path / "cover.jpg"
    cover.write_bytes(b"jpeg-bytes")

    verified, rejected = verify_staging_files([valid, broken, empty, cover])

    assert verified == [valid]
    reasons = {Path(entry["path"]).name: entry["reason"] for entry in rejected}
    assert set(reasons) == {"broken.flac", "empty.mp3", "cover.jpg"}
    assert reasons["cover.jpg"] == "unsupported extension"
    assert reasons["empty.mp3"] == "empty or missing file"


def test_import_moves_audio_and_preserves_structure(tmp_path):
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    source_file = staging / "Artist - Album (2024)" / "01. Song.flac"
    source_file.parent.mkdir(parents=True)
    source_file.write_bytes(b"audio")

    report = import_files_to_library([source_file], staging, library)

    target = library / "Artist - Album (2024)" / "01. Song.flac"
    assert report["imported"] == [str(target.resolve())]
    assert target.read_bytes() == b"audio"
    assert not source_file.exists()
    # Empty staging album folders are cleaned up after the move.
    assert not (staging / "Artist - Album (2024)").exists()


def test_import_never_overwrites_and_rejects_traversal(tmp_path):
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    conflict_source = staging / "Album" / "01. Song.flac"
    conflict_source.parent.mkdir(parents=True)
    conflict_source.write_bytes(b"new audio")
    existing = library / "Album" / "01. Song.flac"
    existing.parent.mkdir(parents=True)
    existing.write_bytes(b"original audio")
    outside = tmp_path / "outside.flac"
    outside.write_bytes(b"evil")

    report = import_files_to_library([conflict_source, outside], staging, library)

    assert report["imported"] == []
    assert len(report["conflicts"]) == 1
    assert existing.read_bytes() == b"original audio"
    assert conflict_source.exists()  # conflict stays in staging for review
    assert len(report["rejected"]) == 1
    assert report["rejected"][0]["path"] == str(outside)


# --- Redis bundle cache -----------------------------------------------------------


class FakeRedis:
    def __init__(self):
        self.values: dict[str, str] = {}

    def get(self, key):
        return self.values.get(key)

    def set(self, key, value, ex=None):
        self.values[key] = value
        return True

    def delete(self, key):
        self.values.pop(key, None)


def test_bundle_cache_roundtrip_and_unavailable_fallback(monkeypatch):
    import redis

    fake = FakeRedis()
    monkeypatch.setattr(
        redis.Redis, "from_url", staticmethod(lambda *args, **kwargs: fake)
    )

    assert qobuz_service._load_cached_bundle() is None
    qobuz_service._store_bundle_cache("123456789", ["sec1", "sec2"])
    assert qobuz_service._load_cached_bundle() == ("123456789", ["sec1", "sec2"])
    qobuz_service._drop_bundle_cache()
    assert qobuz_service._load_cached_bundle() is None

    def broken_redis(*args, **kwargs):
        raise redis.exceptions.RedisError("redis down")

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(broken_redis))
    assert qobuz_service._load_cached_bundle() is None  # non-fatal by design
    qobuz_service._store_bundle_cache("1", ["s"])  # must not raise
    qobuz_service._drop_bundle_cache()  # must not raise


def test_create_client_requires_configuration(monkeypatch):
    _configure_qobuz(monkeypatch, enabled=False)
    with pytest.raises(QobuzConfigurationError):
        qobuz_service.create_qobuz_client()


# --- worker task --------------------------------------------------------------------


def _create_qobuz_job(session_factory, playlist=None, mode="fetch_missing"):
    session = session_factory()
    playlist_id = None
    if playlist is not None:
        session.add(playlist.source)
        session.add(playlist)
        session.flush()
        playlist_id = playlist.id
    job = Job(
        type="qobuz_download",
        status=JobStatus.pending,
        payload=json.dumps({"mode": mode, "playlist_id": playlist_id}),
    )
    session.add(job)
    session.commit()
    result = job.id, playlist_id
    session.close()
    return result


def test_qobuz_task_fails_without_retry_on_configuration_error(
    session_factory, monkeypatch
):
    job_id, _ = _create_qobuz_job(session_factory, mode="url")
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)

    def raise_config():
        raise QobuzConfigurationError("not configured")

    monkeypatch.setattr("app.workers.tasks.create_qobuz_client", raise_config)

    result = qobuz_download_task.run(
        job_id, "url", url="https://play.qobuz.com/track/1"
    )

    check = session_factory()
    job = check.get(Job, job_id)
    assert result["status"] == "failed"
    assert job.status == JobStatus.failed
    assert job.error == "Qobuz download failed (QobuzConfigurationError)"
    assert job.finished_at is not None
    check.close()


def test_qobuz_fetch_missing_end_to_end(
    session_factory, monkeypatch, tmp_path, ffmpeg_binary
):
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.config.settings.qobuz_staging_path", str(staging))
    monkeypatch.setattr("app.config.settings.music_library_path", str(library))
    monkeypatch.setattr("app.config.settings.qobuz_request_delay_seconds", 0)
    monkeypatch.setattr("app.config.settings.musicbrainz_enabled", False)

    session = session_factory()
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="token")
    playlist = Playlist(source=source, external_id="qobuz-pl", name="Qobuz Playlist")
    tracks = [
        ("Tagged Artist", "First Track", "Tagged Album"),
        ("Tagged Artist", "Second Track", "Tagged Album"),
    ]
    items = []
    for position, (artist, title, album) in enumerate(tracks):
        item = PlaylistItem(
            playlist=playlist,
            position=position,
            artist_raw=artist,
            title_raw=title,
            album_raw=album,
            artist_norm=normalize_artist(artist),
            title_norm=normalize_title(title),
            album_norm=normalize_album(album),
        )
        items.append(item)
    # UNMATCHED item without a Match row must be ignored by fetch-missing.
    unmatched = PlaylistItem(
        playlist=playlist,
        position=2,
        artist_raw="Tagged Artist",
        title_raw="Third Track",
        album_raw="Tagged Album",
        artist_norm=normalize_artist("Tagged Artist"),
        title_norm=normalize_title("Third Track"),
        album_norm=normalize_album("Tagged Album"),
    )
    session.add_all([source, playlist, *items, unmatched])
    session.flush()
    for item in items:
        session.add(
            Match(
                playlist_item_id=item.id,
                status=MatchStatus.missing,
                confidence=0.0,
                method="none",
            )
        )
    job = Job(
        type="qobuz_download",
        status=JobStatus.pending,
        payload=json.dumps({"mode": "fetch_missing", "playlist_id": playlist.id}),
    )
    session.add(job)
    session.commit()
    job_id, playlist_id = job.id, playlist.id
    session.close()

    fake_client = FakeQobuzClient(
        tracks=[
            _track_payload(101, "First Track"),
            _track_payload(102, "Second Track"),
            _track_payload(103, "Third Track"),
        ]
    )
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks.create_qobuz_client", lambda: fake_client)

    def fake_download(client, track_id, staging_dir, quality, embed_art):
        titles = {"101": "First Track", "102": "Second Track", "103": "Third Track"}
        title = titles[str(track_id)]
        target = (
            Path(staging_dir)
            / "Tagged Artist - Tagged Album (2024) [24B-96kHz]"
            / f"{title}.flac"
        )
        _write_flac(
            ffmpeg_binary,
            target,
            frequency=440 if title == "First Track" else 550,
            metadata={
                "artist": "Tagged Artist",
                "album": "Tagged Album",
                "title": title,
                "date": "2024",
            },
        )
        return [target]

    monkeypatch.setattr(
        "app.services.qobuz.download_track_to_staging", fake_download
    )

    result = qobuz_download_task.run(
        job_id, "fetch_missing", playlist_id=playlist_id
    )

    assert result["phase"] == "completed"
    assert result["downloads"]["total_missing"] == 2
    assert result["downloads"]["downloaded"] == 2
    assert result["downloads"]["failed"] == 0
    assert len(result["import"]["imported"]) == 2
    assert result["scan"]["status"] == "completed"
    assert result["scan"]["added"] == 2
    assert result["matching"]["ready"] == 2
    assert result["matching"]["missing"] == 1  # the untouched UNMATCHED item

    imported = [Path(path) for path in result["import"]["imported"]]
    assert all(path.is_file() for path in imported)
    assert all(library.resolve() in path.resolve().parents for path in imported)
    assert not list(staging.rglob("*.flac"))

    check = session_factory()
    job = check.get(Job, job_id)
    assert job.status == JobStatus.done
    assert job.lock_owner is None
    matches = check.scalars(
        select(Match)
        .join(PlaylistItem, PlaylistItem.id == Match.playlist_item_id)
        .where(PlaylistItem.playlist_id == playlist_id)
    ).all()
    statuses = [match.status for match in matches]
    assert statuses.count(MatchStatus.ready) == 2
    assert statuses.count(MatchStatus.missing) == 1
    ready_matches = [m for m in matches if m.status == MatchStatus.ready]
    assert all(match.track_id is not None for match in ready_matches)
    check.close()


def test_qobuz_url_task_downloads_imports_and_scans(
    session_factory, monkeypatch, tmp_path, ffmpeg_binary
):
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.config.settings.qobuz_staging_path", str(staging))
    monkeypatch.setattr("app.config.settings.music_library_path", str(library))

    job_id, _ = _create_qobuz_job(session_factory, mode="url")
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.create_qobuz_client", lambda: FakeQobuzClient()
    )

    def fake_url_download(client, url, staging_dir, quality, embed_art):
        target = Path(staging_dir) / "Artist - Album (2024) [24B-96kHz]" / "Song.flac"
        _write_flac(
            ffmpeg_binary,
            target,
            frequency=440,
            metadata={"artist": "Artist", "album": "Album", "title": "Song"},
        )
        return [target]

    monkeypatch.setattr(
        "app.workers.tasks.download_url_to_staging", fake_url_download
    )

    result = qobuz_download_task.run(
        job_id, "url", url="https://play.qobuz.com/album/abc123"
    )

    assert result["phase"] == "completed"
    assert result["mode"] == "url"
    assert result["downloads"]["downloaded"] == 1
    assert len(result["import"]["imported"]) == 1
    assert result["scan"]["added"] == 1
    assert result["matching"] is None

    check = session_factory()
    job = check.get(Job, job_id)
    assert job.status == JobStatus.done
    check.close()
