from __future__ import annotations

import hashlib
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote
from zipfile import ZIP_STORED, ZipFile

from app.models import (
    Album,
    Artist,
    DriveFileLocation,
    File,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    StorageAccount,
    Track,
)
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from tests.helpers import ensure_user


def _seed_delivery(db, root: Path):
    user = ensure_user(db)
    source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
    playlist = Playlist(
        source=source,
        user_id=user.id,
        external_id="delivery-playlist",
        name="Road / Trip",
    )
    artist = Artist(name="Delivery Artist", name_norm=normalize_artist("Delivery Artist"))
    album = Album(
        artist=artist,
        title="Delivery Album",
        title_norm=normalize_album("Delivery Album"),
        year=2026,
    )
    db.add_all([source, playlist, artist, album])
    entries = []
    for position, (title, content) in enumerate(
        [("First: Track", b"first-audio"), ("Second Track", b"second-audio")]
    ):
        track = Track(
            album=album,
            title=title,
            title_norm=normalize_title(title),
            track_no=position + 1,
            disc_no=1,
            duration_ms=180_000 + position * 1_000,
        )
        item = PlaylistItem(
            playlist=playlist,
            position=position,
            artist_raw=artist.name,
            title_raw=title,
            album_raw=album.title,
            artist_norm=artist.name_norm,
            title_norm=track.title_norm,
            album_norm=album.title_norm,
            duration_ms=track.duration_ms,
        )
        path = root / f"source-{position + 1}.flac"
        path.write_bytes(content)
        file = File(
            track=track,
            path=str(path),
            format="flac",
            bit_depth=24,
            sample_rate=96_000,
            size_bytes=len(content),
            sha1=f"{position + 10:040x}",
        )
        match = Match(
            playlist_item=item,
            track=track,
            confidence=1.0,
            method="isrc",
            status=MatchStatus.ready,
        )
        db.add_all([track, item, file, match])
        entries.append((item, track, path, content))
    db.commit()
    return playlist, album, entries


def test_track_download_is_bit_perfect_and_supports_http_range(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _playlist, _album, entries = _seed_delivery(db, tmp_path)
    item, _track, _path, content = entries[0]

    full = api_client.get(
        f"/api/download/track/{item.id}",
        headers=auth_headers,
    )
    partial = api_client.get(
        f"/api/download/track/{item.id}",
        headers={**auth_headers, "Range": "bytes=2-6"},
    )

    assert full.status_code == 200
    assert full.content == content
    assert full.headers["accept-ranges"] == "bytes"
    assert "Delivery Artist - First_ Track.flac" in unquote(
        full.headers["content-disposition"]
    )
    assert partial.status_code == 206
    assert partial.content == content[2:7]
    assert partial.headers["content-range"] == f"bytes 2-6/{len(content)}"


def test_playlist_zip_is_stored_and_contains_relative_m3u8(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    playlist, _album, entries = _seed_delivery(db, tmp_path)

    response = api_client.get(
        f"/api/download/playlist/{playlist.id}",
        headers=auth_headers,
    )

    assert response.status_code == 200
    with ZipFile(BytesIO(response.content)) as archive:
        names = archive.namelist()
        assert names == [
            "001 - Delivery Artist - First_ Track.flac",
            "002 - Delivery Artist - Second Track.flac",
            "playlist.m3u8",
        ]
        assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
        assert archive.read(names[0]) == entries[0][3]
        assert archive.read(names[1]) == entries[1][3]
        manifest = archive.read("playlist.m3u8").decode("utf-8")
        assert str(tmp_path) not in manifest
        assert names[0] in manifest
        assert names[1] in manifest


def test_album_zip_and_standalone_m3u8(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    playlist, album, _entries = _seed_delivery(db, tmp_path)

    album_response = api_client.get(
        f"/api/download/album/{album.id}",
        headers=auth_headers,
    )
    m3u8_response = api_client.get(
        f"/api/download/playlist/{playlist.id}/m3u8",
        headers=auth_headers,
    )

    assert album_response.status_code == 200
    with ZipFile(BytesIO(album_response.content)) as archive:
        assert len(archive.infolist()) == 2
        assert all(info.compress_type == ZIP_STORED for info in archive.infolist())
    assert m3u8_response.status_code == 200
    assert m3u8_response.text.startswith("#EXTM3U\n")
    assert "001 - Delivery Artist - First_ Track.flac" in m3u8_response.text


def test_delivery_rejects_a_catalog_path_outside_the_library(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    library_root = tmp_path / "library"
    library_root.mkdir()
    outside_root = tmp_path / "outside"
    outside_root.mkdir()
    monkeypatch.setattr(
        "app.services.delivery.settings.music_library_path", str(library_root)
    )
    _playlist, _album, entries = _seed_delivery(db, outside_root)

    response = api_client.get(
        f"/api/download/track/{entries[0][0].id}",
        headers=auth_headers,
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "Matched file is unavailable"


def test_remote_track_range_and_mixed_playlist_zip_are_bit_perfect(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    library_root = tmp_path / "library"
    library_root.mkdir()
    cache_root = tmp_path / "cache"
    monkeypatch.setattr(
        "app.services.delivery.settings.music_library_path", str(library_root)
    )
    monkeypatch.setattr(
        "app.services.delivery.settings.storage_cache_path", str(cache_root)
    )
    playlist, _album, entries = _seed_delivery(db, library_root)
    item, track, path, content = entries[0]
    file = track.files[0]
    file.sha1 = hashlib.sha1(content).hexdigest()
    file.path = None
    account = StorageAccount(
        provider="google_drive",
        email="delivery@example.test",
        label="Delivery Drive",
        root_folder_id="root-id",
        enabled=True,
        priority=10,
        state="healthy",
        detail_code="drive_ready",
        credential_version=1,
    )
    db.add(account)
    db.flush()
    db.add(
        DriveFileLocation(
            file_id=file.id,
            account_id=account.id,
            remote_file_id="remote-first",
            remote_name=path.name,
            size_bytes=len(content),
            sha1=file.sha1,
            state="healthy",
        )
    )
    db.commit()

    class FakeResponse:
        def __init__(self, body, range_header=None):
            self.status_code = 200
            self.body = body
            self.headers = {
                "Content-Type": "application/octet-stream",
                "Accept-Ranges": "bytes",
                "Content-Length": str(len(body)),
            }
            if range_header:
                start, end = range_header.removeprefix("bytes=").split("-")
                start, end = int(start), int(end)
                self.body = body[start : end + 1]
                self.status_code = 206
                self.headers["Content-Length"] = str(len(self.body))
                self.headers["Content-Range"] = f"bytes {start}-{end}/{len(body)}"

        def iter_bytes(self, chunk_size=1024 * 1024):
            del chunk_size
            yield self.body

    class FakeDownload:
        def __init__(self, body, range_header=None):
            self.response = FakeResponse(body, range_header)

        def close(self):
            return None

    class FakeClient:
        def open_download(self, remote_file_id, *, range_header=None):
            assert remote_file_id == "remote-first"
            return FakeDownload(content, range_header)

    monkeypatch.setattr(
        "app.services.delivery.client_for_account", lambda _db, _account: FakeClient()
    )

    partial = api_client.get(
        f"/api/download/track/{item.id}",
        headers={**auth_headers, "Range": "bytes=2-6"},
    )
    archive_response = api_client.get(
        f"/api/download/playlist/{playlist.id}",
        headers=auth_headers,
    )

    assert partial.status_code == 206
    assert partial.content == content[2:7]
    assert partial.headers["content-range"] == f"bytes 2-6/{len(content)}"
    assert archive_response.status_code == 200
    with ZipFile(BytesIO(archive_response.content)) as archive:
        names = archive.namelist()
        assert archive.read(names[0]) == content
        assert archive.read(names[1]) == entries[1][3]
    assert cache_root.exists()
    assert list(cache_root.iterdir()) == []


def test_delivery_requires_auth_and_valid_resources(api_client, auth_headers):
    assert api_client.get("/api/download/track/1").status_code == 401
    assert (
        api_client.get("/api/download/track/999", headers=auth_headers).status_code
        == 404
    )
    assert (
        api_client.get("/api/download/album/999", headers=auth_headers).status_code
        == 404
    )
    assert (
        api_client.get("/api/download/playlist/999", headers=auth_headers).status_code
        == 404
    )
