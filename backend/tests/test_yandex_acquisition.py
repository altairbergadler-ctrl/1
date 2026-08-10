from __future__ import annotations

import json
import base64
import hashlib
import hmac
import subprocess
from types import SimpleNamespace

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
    ProviderAttempt,
    ServiceEnum,
)
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.services.qobuz import provider_lookup_key
from app.services.yandex_acquisition import (
    _candidate,
    _is_allowed_download_url,
    _signed_request_from_sidecar,
    YandexAcquisitionProviderError,
    YandexLosslessInfo,
    build_yandex_lossless_request,
    download_yandex_track_to_staging,
    fetch_missing_yandex_tracks,
    get_yandex_lossless_info,
    yandex_download_eligibility,
)
from app.workers.tasks import yandex_download_task


def _track(track_id: str, title: str = "First Track"):
    return SimpleNamespace(
        id=track_id,
        title=title,
        version=None,
        artists=[SimpleNamespace(name="Tagged Artist")],
        albums=[SimpleNamespace(id="album-1", title="Tagged Album")],
        duration_ms=187_000,
        isrc="USAAA2400001",
    )


class FakeYandexDownloadClient:
    def __init__(self, tracks=None, search_results=None):
        self.track_map = dict(tracks or {})
        self.search_results = list(search_results or [])
        self.search_calls = []

    def tracks(self, track_ids):
        return [self.track_map[item] for item in track_ids if item in self.track_map]

    def search(self, text, **kwargs):
        self.search_calls.append((text, kwargs))
        return SimpleNamespace(
            tracks=SimpleNamespace(results=list(self.search_results))
        )


def _missing_playlist(db, *, service=ServiceEnum.spotify, count=1):
    source = PlaylistSource(service=service, access_token="test-token")
    playlist = Playlist(source=source, external_id=f"p-{service.value}", name="Playlist")
    db.add_all([source, playlist])
    db.flush()
    items = []
    for position in range(count):
        title = "First Track" if count == 1 else f"Track {position:02d}"
        item = PlaylistItem(
            playlist=playlist,
            position=position,
            artist_raw="Tagged Artist",
            title_raw=title,
            album_raw="Tagged Album",
            artist_norm=normalize_artist("Tagged Artist"),
            title_norm=normalize_title(title),
            album_norm=normalize_album("Tagged Album"),
            duration_ms=187_000,
            isrc="USAAA2400001" if count == 1 else None,
            external_track_id="ym-1" if service == ServiceEnum.yandex else None,
        )
        db.add(item)
        items.append(item)
    db.flush()
    db.add_all(
        [Match(playlist_item_id=item.id, status=MatchStatus.missing) for item in items]
    )
    db.commit()
    return playlist, items


def test_status_exposes_flac_only_without_exposing_secret(
    api_client, auth_headers, db, monkeypatch
):
    monkeypatch.setattr("app.config.settings.yandex_download_enabled", True)
    monkeypatch.setattr("app.api.yandex_download.YANDEX_LOSSLESS_AVAILABLE", True)
    db.add(PlaylistSource(service=ServiceEnum.yandex, access_token="do-not-return"))
    db.commit()

    response = api_client.get("/api/yandex-download/status", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "enabled": True,
        "configured": True,
        "supported_codecs": ["flac", "aac", "mp3"],
        "lossless_supported": True,
        "max_tracks_per_run": 25,
        "batch_delay_seconds": 30.0,
    }
    assert "do-not-return" not in json.dumps(body)


def test_status_stays_disabled_without_internal_signer_token(
    api_client, auth_headers, db, monkeypatch
):
    monkeypatch.setattr("app.config.settings.yandex_download_enabled", True)
    monkeypatch.setattr("app.config.settings.yandex_internal_token", "")
    db.add(PlaylistSource(service=ServiceEnum.yandex, access_token="test-token"))
    db.commit()

    response = api_client.get("/api/yandex-download/status", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "configured": False,
        "supported_codecs": [],
        "lossless_supported": False,
        "max_tracks_per_run": 25,
        "batch_delay_seconds": 30.0,
    }


def test_yandex_download_endpoints_require_auth(api_client):
    assert api_client.get("/api/yandex-download/status").status_code == 401
    assert (
        api_client.post(
            "/api/yandex-download/fetch-missing", json={"playlist_id": 1}
        ).status_code
        == 401
    )


def test_eligibility_is_independent_per_provider(db):
    playlist, items = _missing_playlist(db)
    item = items[0]
    lookup_key = provider_lookup_key(item)
    db.add(
        ProviderAttempt(
            provider="qobuz",
            lookup_key=lookup_key,
            playlist_item_id=item.id,
            status="not_found",
        )
    )
    db.commit()

    assert yandex_download_eligibility(db, playlist) == {
        "total_missing": 1,
        "eligible": 1,
        "already_checked": 0,
    }


def test_fetch_uses_yandex_source_track_id_without_search(db, monkeypatch, tmp_path):
    playlist, _items = _missing_playlist(db, service=ServiceEnum.yandex)
    client = FakeYandexDownloadClient(tracks={"ym-1": _track("ym-1")})
    target = tmp_path / "First Track.flac"
    target.write_bytes(b"audio")
    monkeypatch.setattr("app.config.settings.yandex_request_delay_seconds", 0)
    monkeypatch.setattr(
        "app.services.yandex_acquisition.download_yandex_track_to_staging",
        lambda *args, **kwargs: ([target], {"codec": "flac", "bitrate_kbps": 0}),
    )

    summary, files = fetch_missing_yandex_tracks(db, playlist, client)

    assert files == [target]
    assert summary["downloaded"] == 1
    assert summary["items"][0]["selection"] == "source_id"
    assert summary["items"][0]["codec"] == "flac"
    assert client.search_calls == []


def test_fetch_checks_entire_playlist_in_batches(db, monkeypatch):
    playlist, _items = _missing_playlist(db, count=53)
    client = FakeYandexDownloadClient()
    monkeypatch.setattr("app.config.settings.yandex_max_tracks_per_run", 25)
    monkeypatch.setattr("app.config.settings.yandex_request_delay_seconds", 0)
    monkeypatch.setattr("app.config.settings.yandex_batch_delay_seconds", 7)
    sleeps = []
    monkeypatch.setattr("app.services.yandex_acquisition.time.sleep", sleeps.append)

    summary, files = fetch_missing_yandex_tracks(db, playlist, client)

    assert files == []
    assert summary["processed"] == 53
    assert summary["not_found"] == 53
    assert summary["batch_count"] == 3
    assert sleeps == [7, 7]
    assert db.query(ProviderAttempt).filter_by(provider="yandex").count() == 53
    assert yandex_download_eligibility(db, playlist)["eligible"] == 0


def test_fetch_persists_safe_provider_error_detail(db, monkeypatch):
    playlist, _items = _missing_playlist(db, service=ServiceEnum.yandex)
    client = FakeYandexDownloadClient(tracks={"ym-1": _track("ym-1")})
    monkeypatch.setattr("app.config.settings.yandex_request_delay_seconds", 0)
    monkeypatch.setattr(
        "app.services.yandex_acquisition.download_yandex_track_to_staging",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            YandexAcquisitionProviderError("Yandex returned an unsupported codec")
        ),
    )

    summary, files = fetch_missing_yandex_tracks(db, playlist, client)

    assert files == []
    assert summary["failed"] == 1
    assert summary["items"][0]["error_detail"] == (
        "Yandex returned an unsupported codec"
    )


def test_fetch_endpoint_rejects_provider_without_lossless_support(
    api_client, auth_headers, db, monkeypatch
):
    monkeypatch.setattr("app.config.settings.yandex_download_enabled", True)
    monkeypatch.setattr("app.api.yandex_download.YANDEX_LOSSLESS_AVAILABLE", False)
    source = PlaylistSource(service=ServiceEnum.yandex, access_token="test-token")
    playlist = Playlist(source=source, external_id="ym", name="Yandex")
    db.add_all([source, playlist])
    db.commit()
    queued = []
    monkeypatch.setattr(
        "app.api.yandex_download.yandex_download_task.delay",
        lambda *args: queued.append(args),
    )

    response = api_client.post(
        "/api/yandex-download/fetch-missing",
        json={"playlist_id": playlist.id},
        headers=auth_headers,
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "Yandex lossless download is not available"
    assert queued == []
    assert db.scalar(select(Job).where(Job.type == "yandex_download")) is None


def test_download_host_allowlist():
    assert _is_allowed_download_url("https://storage.mds.yandex.net/get-file/x")
    assert _is_allowed_download_url("https://strm.yandex.ru/music/x")
    assert not _is_allowed_download_url("http://storage.mds.yandex.net/x")
    assert not _is_allowed_download_url("https://yandex.net.evil.example/x")
    assert not _is_allowed_download_url("https://127.0.0.1/x")
    assert not _is_allowed_download_url("https://storage.mds.yandex.net:bad/x")


def test_lossless_request_signs_flac_only_contract():
    params = build_yandex_lossless_request(
        "117708948",
        timestamp=1_724_399_849,
        key="unit-test-key",
    )

    message = (
        "1724399849117708948lossless"
        "flacaache-aacmp3flac-mp4aac-mp4he-aac-mp4raw"
    )
    expected = base64.b64encode(
        hmac.new(b"unit-test-key", message.encode(), hashlib.sha256).digest()
    ).decode()[:-1]
    assert params == {
        "ts": 1_724_399_849,
        "trackId": "117708948",
        "quality": "lossless",
        "codecs": "flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4",
        "transports": "raw",
        "sign": expected,
    }


def test_sidecar_signed_contract_fails_closed_on_mismatch(monkeypatch):
    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "ts": "1724399849",
                "trackId": "different-track",
                "quality": "lossless",
                "codecs": "flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4",
                "transports": "raw",
                "sign": "signature",
            }

    monkeypatch.setattr(
        "app.config.settings.yandex_internal_token", "test-yandex-internal-token"
    )
    monkeypatch.setattr(
        "app.services.yandex_acquisition.httpx.post",
        lambda *_args, **_kwargs: FakeResponse(),
    )

    with pytest.raises(YandexAcquisitionProviderError, match="mismatched contract"):
        _signed_request_from_sidecar("117708948", 1_724_399_849)


def test_get_lossless_info_accepts_only_matching_flac_response(monkeypatch):
    calls = []

    def fake_request(_client, params):
        calls.append(params)
        return {
            "download_info": {
                "track_id": "117708948",
                "real_id": "117708948",
                "quality": "lossless",
                "codec": "flac",
                "transport": "raw",
                "bitrate": 0,
                "size": 123456,
                "urls": [
                    "https://media.strm.yandex.net/music-v2/raw/example/flac"
                ],
            }
        }

    monkeypatch.setattr(
        "app.services.yandex_acquisition._request_yandex_file_info",
        fake_request,
    )
    client = SimpleNamespace()
    info = get_yandex_lossless_info(
        client,
        "117708948",
        timestamp=1_724_399_849,
        key="unit-test-key",
    )

    assert info.codec == "flac"
    assert info.quality == "lossless"
    assert info.transport == "raw"
    assert info.size == 123456
    assert info.url.endswith("/flac")
    assert calls[0]["codecs"] == (
        "flac,aac,he-aac,mp3,flac-mp4,aac-mp4,he-aac-mp4"
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("transport", "encrypted", "unsupported transport"),
        ("track_id", "999", "different track"),
    ],
)
def test_get_lossless_info_fails_closed_on_response_downgrade(
    field, value, message, monkeypatch
):
    payload = {
        "track_id": "117708948",
        "real_id": "117708948",
        "quality": "lossless",
        "codec": "flac",
        "transport": "raw",
        "urls": ["https://media.strm.yandex.net/music-v2/raw/example/flac"],
    }
    payload[field] = value
    monkeypatch.setattr(
        "app.services.yandex_acquisition._request_yandex_file_info",
        lambda *_args, **_kwargs: {"download_info": payload},
    )
    client = SimpleNamespace()

    with pytest.raises(YandexAcquisitionProviderError, match=message):
        get_yandex_lossless_info(
            client,
            "117708948",
            timestamp=1_724_399_849,
            key="unit-test-key",
        )


def test_get_lossless_info_rejects_untrusted_download_url(monkeypatch):
    monkeypatch.setattr(
        "app.services.yandex_acquisition._request_yandex_file_info",
        lambda *_args, **_kwargs: {
            "download_info": {
                "track_id": "117708948",
                "quality": "lossless",
                "codec": "flac",
                "transport": "raw",
                "urls": ["https://yandex.net.evil.example/track.flac"],
            }
        },
    )
    client = SimpleNamespace()

    with pytest.raises(YandexAcquisitionProviderError, match="trusted audio URL"):
        get_yandex_lossless_info(
            client,
            "117708948",
            timestamp=1_724_399_849,
            key="unit-test-key",
        )


def test_download_accepts_aac_mp4_fallback(
    db, monkeypatch, tmp_path, ffmpeg_binary
):
    _playlist, items = _missing_playlist(db)
    item = items[0]
    candidate = _candidate(_track("ym-1"))
    assert candidate is not None
    source = tmp_path / "source.m4a"
    subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-y",
            str(source),
        ],
        check=True,
    )
    staging = tmp_path / "staging"
    monkeypatch.setattr("app.config.settings.yandex_staging_path", str(staging))

    monkeypatch.setattr(
        "app.services.yandex_acquisition._request_yandex_file_info",
        lambda *_args, **_kwargs: {
            "download_info": {
                "track_id": "ym-1",
                "quality": "high",
                "codec": "aac-mp4",
                "transport": "raw",
                "bitrate": 192,
                "urls": ["https://storage.mds.yandex.net/aac256-mp4"],
            }
        },
    )

    def fake_stream(_url, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(
        "app.services.yandex_acquisition._stream_to_file", fake_stream
    )

    files, quality = download_yandex_track_to_staging(
        SimpleNamespace(),
        candidate,
        item,
        timestamp=1_724_399_849,
        key="unit-test-key",
    )

    assert len(files) == 1
    assert files[0].suffix == ".m4a"
    assert quality["codec"] == "aac"
    assert quality["source_codec"] == "aac-mp4"
    assert quality["lossless"] is False
    assert quality["bitrate_kbps"] > 0


def test_download_accepts_mp3_fallback(
    db, monkeypatch, tmp_path, ffmpeg_binary
):
    _playlist, items = _missing_playlist(db)
    item = items[0]
    candidate = _candidate(_track("ym-1"))
    assert candidate is not None
    source = tmp_path / "source.mp3"
    subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            "192k",
            "-y",
            str(source),
        ],
        check=True,
    )
    staging = tmp_path / "staging"
    monkeypatch.setattr("app.config.settings.yandex_staging_path", str(staging))
    monkeypatch.setattr(
        "app.services.yandex_acquisition.get_yandex_lossless_info",
        lambda *_args, **_kwargs: YandexLosslessInfo(
            track_id="ym-1",
            real_id="ym-1",
            quality="high",
            codec="mp3",
            transport="raw",
            bitrate=192,
            size=source.stat().st_size,
            url="https://storage.mds.yandex.net/mp3192",
            decryption_key=None,
        ),
    )

    def fake_stream(_url, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(
        "app.services.yandex_acquisition._stream_to_file", fake_stream
    )

    files, quality = download_yandex_track_to_staging(
        SimpleNamespace(), candidate, item
    )

    assert files[0].suffix == ".mp3"
    assert quality["codec"] == "mp3"
    assert quality["source_codec"] == "mp3"
    assert quality["lossless"] is False
    assert quality["bitrate_kbps"] > 0


def test_download_rejects_unknown_codec(db, monkeypatch):
    _playlist, items = _missing_playlist(db)
    item = items[0]
    candidate = _candidate(_track("ym-1"))
    assert candidate is not None
    monkeypatch.setattr(
        "app.services.yandex_acquisition._request_yandex_file_info",
        lambda *_args, **_kwargs: {
            "download_info": {
                "track_id": "ym-1",
                "quality": "high",
                "codec": "opus",
                "transport": "raw",
                "urls": ["https://storage.mds.yandex.net/opus"],
            }
        },
    )

    with pytest.raises(YandexAcquisitionProviderError, match="unsupported codec"):
        download_yandex_track_to_staging(
            SimpleNamespace(),
            candidate,
            item,
            timestamp=1_724_399_849,
            key="unit-test-key",
        )


def test_download_remuxes_flac_mp4_without_reencoding(
    db, monkeypatch, tmp_path, ffmpeg_binary
):
    _playlist, items = _missing_playlist(db)
    item = items[0]
    candidate = _candidate(_track("117708948"))
    assert candidate is not None
    source = tmp_path / "source.m4a"
    subprocess.run(
        [
            ffmpeg_binary,
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:a",
            "flac",
            "-f",
            "mp4",
            "-y",
            str(source),
        ],
        check=True,
    )
    staging = tmp_path / "staging"
    monkeypatch.setattr("app.config.settings.yandex_staging_path", str(staging))
    monkeypatch.setattr(
        "app.services.yandex_acquisition.get_yandex_lossless_info",
        lambda *_args, **_kwargs: YandexLosslessInfo(
            track_id="117708948",
            real_id="117708948",
            quality="lossless",
            codec="flac-mp4",
            transport="raw",
            bitrate=0,
            size=source.stat().st_size,
            url="https://media.strm.yandex.net/music-v2/raw/example/flac-mp4",
            decryption_key=None,
        ),
    )

    def fake_stream(_url, target, **_kwargs):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())

    monkeypatch.setattr(
        "app.services.yandex_acquisition._stream_to_file", fake_stream
    )

    files, quality = download_yandex_track_to_staging(object(), candidate, item)

    assert len(files) == 1
    assert files[0].suffix == ".flac"
    assert files[0].is_file()
    assert not list(staging.rglob("*.m4a"))
    assert quality["codec"] == "flac"
    assert quality["source_codec"] == "flac-mp4"
    assert quality["remuxed"] is True
    probe = subprocess.run(
        [
            ffmpeg_binary.replace("ffmpeg", "ffprobe"),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(files[0]),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert probe.stdout.strip() == "flac"


def test_yandex_download_task_imports_scans_and_matches(
    session_factory, monkeypatch, tmp_path, ffmpeg_binary
):
    staging = tmp_path / "staging"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.config.settings.yandex_download_enabled", True)
    monkeypatch.setattr("app.config.settings.yandex_staging_path", str(staging))
    monkeypatch.setattr("app.config.settings.music_library_path", str(library))
    monkeypatch.setattr("app.config.settings.yandex_request_delay_seconds", 0)
    monkeypatch.setattr("app.config.settings.musicbrainz_enabled", False)

    session = session_factory()
    source = PlaylistSource(service=ServiceEnum.spotify, access_token="test-source")
    yandex_source = PlaylistSource(
        service=ServiceEnum.yandex, access_token="test-yandex"
    )
    playlist = Playlist(source=source, external_id="ym-task", name="Task Playlist")
    item = PlaylistItem(
        playlist=playlist,
        position=0,
        artist_raw="Tagged Artist",
        title_raw="First Track",
        album_raw="Tagged Album",
        artist_norm=normalize_artist("Tagged Artist"),
        title_norm=normalize_title("First Track"),
        album_norm=normalize_album("Tagged Album"),
        duration_ms=1000,
        isrc="USAAA2400001",
    )
    session.add_all([source, yandex_source, playlist, item])
    session.flush()
    session.add(
        Match(
            playlist_item_id=item.id,
            status=MatchStatus.missing,
            confidence=0.0,
            method="none",
        )
    )
    job = Job(
        type="yandex_download",
        status=JobStatus.pending,
        payload=json.dumps({"playlist_id": playlist.id}),
    )
    session.add(job)
    session.commit()
    job_id, playlist_id, item_id = job.id, playlist.id, item.id
    session.close()

    client = FakeYandexDownloadClient(search_results=[_track("ym-1")])
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.create_yandex_acquisition_client", lambda _db: client
    )

    def fake_download(_client, _candidate_value, _item):
        target = staging / "Tagged Artist" / "Tagged Album" / "First Track.flac"
        target.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                ffmpeg_binary,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=1",
                "-metadata",
                "artist=Tagged Artist",
                "-metadata",
                "album=Tagged Album",
                "-metadata",
                "title=First Track",
                "-codec:a",
                "flac",
                "-y",
                str(target),
            ],
            check=True,
        )
        return [target], {"codec": "flac", "bitrate_kbps": 0}

    monkeypatch.setattr(
        "app.services.yandex_acquisition.download_yandex_track_to_staging",
        fake_download,
    )

    result = yandex_download_task.run(job_id, playlist_id)

    assert result["phase"] == "completed"
    assert result["downloads"]["stored"] == 1
    assert result["downloads"]["items"][0]["codec"] == "flac"
    assert result["scan"]["status"] == "completed"
    assert result["scan"]["added"] == 1
    assert result["matching"]["ready"] == 1
    assert not list(staging.rglob("*.flac"))

    check = session_factory()
    stored_job = check.get(Job, job_id)
    stored_match = check.scalar(
        select(Match).where(Match.playlist_item_id == item_id)
    )
    attempt = check.scalar(
        select(ProviderAttempt).where(ProviderAttempt.provider == "yandex")
    )
    assert stored_job.status == JobStatus.done
    assert stored_job.lock_owner is None
    assert stored_match.status == MatchStatus.ready
    assert attempt.status == "stored"
    check.close()
