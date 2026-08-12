from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from io import BytesIO
from xml.etree.ElementTree import fromstring

from mutagen.flac import Picture
from PIL import Image
from sqlalchemy import select

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
    PlayerCredential,
    ServiceEnum,
    StorageAccount,
    Track,
    UserRole,
    UserState,
    utcnow,
)
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.services.player_credentials import create_player_credential
from tests.helpers import ensure_user


def _seed(db, root, *, email="owner@example.test", service=ServiceEnum.spotify):
    user = ensure_user(
        db,
        email=email,
        role=UserRole.owner if email == "owner@example.test" else UserRole.user,
        bootstrap=email == "owner@example.test",
    )
    source = PlaylistSource(user_id=user.id, service=service)
    playlist = Playlist(source=source, user_id=user.id, external_id=email, name="Offline Mix")
    artist = Artist(name="Visible Artist", name_norm=normalize_artist("Visible Artist" + email))
    album = Album(
        artist=artist,
        title="Visible Album",
        title_norm=normalize_album("Visible Album" + email),
        year=2026,
    )
    track = Track(
        album=album,
        title="Visible Song",
        title_norm=normalize_title("Visible Song" + email),
        duration_ms=125_000,
        track_no=1,
        disc_no=1,
    )
    item = PlaylistItem(
        playlist=playlist,
        position=0,
        artist_raw=artist.name,
        album_raw=album.title,
        title_raw=track.title,
    )
    content = ("lossless-" + email).encode()
    path = root / (hashlib.sha1(email.encode()).hexdigest() + ".flac")
    path.write_bytes(content)
    file = File(
        track=track,
        path=str(path),
        format="flac",
        bit_depth=24,
        sample_rate=96000,
        size_bytes=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
    )
    match = Match(
        playlist_item=item,
        track=track,
        confidence=1,
        method="exact",
        status=MatchStatus.ready,
    )
    db.add_all([source, playlist, artist, album, track, item, file, match])
    db.flush()
    raw, _credential = create_player_credential(db, user.id, "Symfonium")
    db.commit()
    return user, raw, playlist, artist, album, track, item, content


def _params(key, **extra):
    return {"apiKey": key, "v": "1.16.1", "c": "symfonium-test", "f": "json", **extra}


def _flac_with_picture(picture_data: bytes) -> bytes:
    picture = Picture()
    picture.type = 3
    picture.mime = "image/png"
    picture.width = 32
    picture.height = 32
    picture.depth = 24
    picture.data = picture_data
    picture_block = picture.write()
    packed_stream_info = (44_100 << 44) | (15 << 36) | 44_100
    stream_info = (
        (4096).to_bytes(2, "big")
        + (4096).to_bytes(2, "big")
        + bytes(6)
        + packed_stream_info.to_bytes(8, "big")
        + bytes(16)
    )
    return (
        b"fLaC"
        + bytes([0])
        + len(stream_info).to_bytes(3, "big")
        + stream_info
        + bytes([0x86])
        + len(picture_block).to_bytes(3, "big")
        + picture_block
    )


def test_public_extension_and_api_key_only_auth(api_client):
    public = api_client.get("/rest/getOpenSubsonicExtensions", params={"f": "json"})
    assert public.status_code == 200
    extension = public.json()["subsonic-response"]["openSubsonicExtensions"]
    assert extension == [{"name": "apiKeyAuthentication", "versions": [1]}]
    assert api_client.get("/rest/ping", params={"v": "1.16.1", "f": "json"}).json()[
        "subsonic-response"
    ]["error"]["code"] == 10
    conflict = api_client.get(
        "/rest/ping", params={"apiKey": "x", "u": "x", "v": "1.16.1", "f": "json"}
    )
    assert conflict.json()["subsonic-response"]["error"]["code"] == 43


def test_version_negotiation(api_client, db, tmp_path):
    _user, key, *_ = _seed(db, tmp_path)
    assert api_client.get(
        "/rest/ping", params=_params(key, v="1.13.0")
    ).json()["subsonic-response"]["status"] == "ok"
    cases = (("1.12.0", 20), ("1.17.0", 30), ("invalid", 0))
    for version, code in cases:
        response = api_client.get("/rest/ping", params=_params(key, v=version))
        assert response.json()["subsonic-response"]["error"]["code"] == code


def test_wrong_expired_revoked_and_disabled_player_keys(api_client, db, tmp_path):
    user, key, *_ = _seed(db, tmp_path)
    wrong = api_client.get("/rest/ping", params=_params("afk1.invalid.invalid"))
    assert wrong.json()["subsonic-response"]["error"]["code"] == 44

    credential = db.scalar(select(PlayerCredential).where(PlayerCredential.user_id == user.id))
    credential.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    expired = api_client.get("/rest/ping", params=_params(key))
    assert expired.json()["subsonic-response"]["error"]["code"] == 44

    revoked_key, revoked_credential = create_player_credential(db, user.id, "Revoked")
    revoked_credential.revoked_at = utcnow()
    db.commit()
    revoked = api_client.get("/rest/ping", params=_params(revoked_key))
    assert revoked.json()["subsonic-response"]["error"]["code"] == 44

    disabled_key, _disabled_credential = create_player_credential(db, user.id, "Disabled")
    user.state = UserState.disabled
    db.commit()
    disabled = api_client.get("/rest/ping", params=_params(disabled_key))
    assert disabled.json()["subsonic-response"]["error"]["code"] == 44


def test_failed_player_authentication_is_rate_limited(
    api_client, monkeypatch
):
    from app.services import player_credentials

    player_credentials._test_limits.clear()
    monkeypatch.setattr(player_credentials.settings, "opensubsonic_failed_attempts", 2)
    responses = [
        api_client.get("/rest/ping", params=_params("afk1.fixed.invalid"))
        for _ in range(3)
    ]
    assert [response.status_code for response in responses] == [200, 200, 429]
    assert responses[-1].headers["retry-after"] == "60"
    player_credentials._test_limits.clear()


def test_scoped_catalog_symfonium_empty_search_playlist_order_and_alias(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, playlist, artist, album, track, _item, _content = _seed(db, tmp_path)
    search = api_client.get(
        "/rest/search3.view",
        params=_params(
            key,
            query='""',
            artistCount="500",
            albumCount="500",
            songCount="500",
        ),
    )
    assert search.status_code == 200
    result = search.json()["subsonic-response"]["searchResult3"]
    assert [row["id"] for row in result["song"]] == [f"so:{track.opensubsonic_id}"]
    assert result["album"][0]["id"] == f"al:{album.opensubsonic_id}"
    assert result["album"][0]["created"].endswith("Z")
    assert result["artist"][0]["id"] == f"ar:{artist.opensubsonic_id}"
    fetched = api_client.get(
        "/rest/getPlaylist", params=_params(key, id=f"pl:{playlist.opensubsonic_id}")
    ).json()["subsonic-response"]["playlist"]
    assert fetched["songCount"] == 1
    assert fetched["readonly"] is True
    assert fetched["entry"][0]["id"] == f"so:{track.opensubsonic_id}"


def test_search_pagination_empty_results_and_ready_playlist_duplicates(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, playlist, _artist, _album, track, _item, _content = _seed(db, tmp_path)
    duplicate = PlaylistItem(
        playlist=playlist,
        position=1,
        artist_raw="duplicate",
        album_raw="duplicate",
        title_raw="duplicate",
    )
    hidden = PlaylistItem(
        playlist=playlist,
        position=2,
        artist_raw="hidden",
        album_raw="hidden",
        title_raw="hidden",
    )
    db.add_all(
        [
            duplicate,
            hidden,
            Match(
                playlist_item=duplicate,
                track=track,
                confidence=1,
                method="exact",
                status=MatchStatus.ready,
            ),
            Match(
                playlist_item=hidden,
                track=track,
                confidence=0,
                method="none",
                status=MatchStatus.missing,
            ),
        ]
    )
    db.commit()
    public_song_id = f"so:{track.opensubsonic_id}"
    fetched = api_client.get(
        "/rest/getPlaylist", params=_params(key, id=f"pl:{playlist.opensubsonic_id}")
    ).json()["subsonic-response"]["playlist"]
    assert [entry["id"] for entry in fetched["entry"]] == [public_song_id, public_song_id]
    paged = api_client.get(
        "/rest/search3",
        params=_params(
            key,
            query="",
            artistCount="0",
            albumCount="0",
            songCount="1",
            songOffset="1",
        ),
    ).json()["subsonic-response"]["searchResult3"]
    assert paged == {"artist": [], "album": [], "song": []}
    empty = api_client.get(
        "/rest/search3", params=_params(key, query="does-not-exist")
    ).json()["subsonic-response"]["searchResult3"]
    assert empty == {"artist": [], "album": [], "song": []}


def test_unknown_optional_numeric_metadata_is_omitted_instead_of_null(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, _playlist, _artist, album, track, _item, _content = _seed(db, tmp_path)
    album.year = None
    track.track_no = None
    track.disc_no = None
    file = db.scalar(select(File).where(File.track_id == track.id))
    file.bit_depth = None
    file.sample_rate = None
    db.commit()

    result = api_client.get(
        "/rest/search3.view",
        params=_params(
            key,
            query='""',
            artistCount="500",
            albumCount="500",
            songCount="500",
        ),
    ).json()["subsonic-response"]["searchResult3"]
    assert "year" not in result["album"][0]
    for name in ("year", "track", "discNumber", "bitDepth", "samplingRate"):
        assert name not in result["song"][0]


def test_all_foreign_and_missing_ids_are_indistinguishable(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _owner, owner_key, *_ = _seed(db, tmp_path)
    _other, _other_key, foreign_playlist, foreign_artist, foreign_album, foreign_track, *_ = _seed(
        db, tmp_path, email="other@example.test", service=ServiceEnum.yandex
    )
    missing_uuid = "00000000-0000-0000-0000-000000000000"
    cases = (
        ("getArtist", f"ar:{foreign_artist.opensubsonic_id}", f"ar:{missing_uuid}"),
        ("getAlbum", f"al:{foreign_album.opensubsonic_id}", f"al:{missing_uuid}"),
        ("getSong", f"so:{foreign_track.opensubsonic_id}", f"so:{missing_uuid}"),
        ("getPlaylist", f"pl:{foreign_playlist.opensubsonic_id}", f"pl:{missing_uuid}"),
        ("getCoverArt", f"ca:al:{foreign_album.opensubsonic_id}", f"ca:al:{missing_uuid}"),
        ("stream", f"so:{foreign_track.opensubsonic_id}", f"so:{missing_uuid}"),
        ("download", f"so:{foreign_track.opensubsonic_id}", f"so:{missing_uuid}"),
    )
    for method, foreign_id, missing_id in cases:
        foreign = api_client.get(f"/rest/{method}", params=_params(owner_key, id=foreign_id))
        missing = api_client.get(f"/rest/{method}", params=_params(owner_key, id=missing_id))
        assert foreign.status_code == missing.status_code == 200
        assert foreign.json()["subsonic-response"]["error"] == missing.json()[
            "subsonic-response"
        ]["error"]


def test_system_and_empty_sync_endpoints_do_not_load_catalog(
    api_client, db, tmp_path, monkeypatch
):
    _user, key, *_ = _seed(db, tmp_path)

    def fail_catalog(*_args, **_kwargs):
        raise AssertionError("system endpoint loaded the catalog")

    monkeypatch.setattr("app.api.opensubsonic.visible_tracks", fail_catalog)
    for method in (
        "ping",
        "getLicense",
        "getMusicFolders",
        "getStarred2",
        "getBookmarks",
        "getGenres",
    ):
        response = api_client.get(f"/rest/{method}", params=_params(key))
        assert response.json()["subsonic-response"]["status"] == "ok"


def test_symfonium_supplemental_sync_collections_are_empty_json_and_xml(
    api_client, db, tmp_path
):
    _user, key, *_ = _seed(db, tmp_path)
    cases = (
        ("getStarred2", "starred2", {"artist": [], "album": [], "song": []}),
        ("getBookmarks", "bookmarks", {"bookmark": []}),
        ("getGenres", "genres", {"genre": []}),
    )

    namespace = "{http://subsonic.org/restapi}"
    for method, collection, expected in cases:
        json_response = api_client.get(f"/rest/{method}.view", params=_params(key))
        assert json_response.status_code == 200
        assert json_response.json()["subsonic-response"][collection] == expected

        xml_response = api_client.get(
            f"/rest/{method}.view",
            params={"apiKey": key, "v": "1.16.1", "c": "symfonium-test"},
        )
        assert xml_response.status_code == 200
        assert fromstring(xml_response.content).find(f"{namespace}{collection}") is not None


def test_album_list_requires_and_honors_supported_type(api_client, db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, *_ = _seed(db, tmp_path)
    missing = api_client.get("/rest/getAlbumList2", params=_params(key))
    assert missing.json()["subsonic-response"]["error"]["code"] == 10
    alphabetical = api_client.get(
        "/rest/getAlbumList2", params=_params(key, type="alphabeticalByName")
    ).json()["subsonic-response"]["albumList2"]["album"]
    assert alphabetical[0]["created"].endswith("Z")
    by_year = api_client.get(
        "/rest/getAlbumList2",
        params=_params(key, type="byYear", fromYear="2026", toYear="2026"),
    ).json()["subsonic-response"]["albumList2"]["album"]
    assert len(by_year) == 1
    invalid = api_client.get(
        "/rest/getAlbumList2", params=_params(key, type="not-a-list")
    )
    assert invalid.json()["subsonic-response"]["error"]["code"] == 0


def test_stream_and_download_are_original_and_support_range(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, _playlist, _artist, _album, track, _item, content = _seed(db, tmp_path)
    params = _params(key, id=f"so:{track.opensubsonic_id}", format="raw")
    full = api_client.get("/rest/download", params=params)
    partial = api_client.get("/rest/stream.view", params=params, headers={"Range": "bytes=2-6"})
    head = api_client.head("/rest/stream", params=params)
    assert full.content == content
    assert partial.status_code == 206
    assert partial.content == content[2:7]
    assert partial.headers["content-range"] == f"bytes 2-6/{len(content)}"
    assert head.status_code == 200
    assert head.content == b""
    assert head.headers["content-length"] == str(len(content))


def test_metadata_and_download_use_the_same_playable_fallback(
    api_client, db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, key, _playlist, _artist, _album, track, _item, content = _seed(db, tmp_path)
    db.add(
        File(
            track=track,
            path=str(tmp_path / "missing-hires.wav"),
            format="wav",
            bit_depth=32,
            sample_rate=384000,
            size_bytes=999999,
            sha1="f" * 40,
        )
    )
    db.commit()
    params = _params(key, id=f"so:{track.opensubsonic_id}")
    song = api_client.get("/rest/getSong", params=params).json()["subsonic-response"]["song"]
    downloaded = api_client.get("/rest/download", params=params)
    assert song["contentType"] == "audio/flac"
    assert song["suffix"] == "flac"
    assert song["size"] == len(content)
    assert downloaded.content == content


def test_drive_stream_and_metadata_share_remote_source(
    api_client, db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(library))
    _user, key, _playlist, _artist, _album, track, _item, content = _seed(db, library)
    file = db.scalar(select(File).where(File.track_id == track.id))
    file.path = None
    account = StorageAccount(
        provider="google_drive",
        email="opensubsonic-drive@example.test",
        root_folder_id="root",
        enabled=True,
        priority=1,
        state="healthy",
        credential_version=1,
    )
    db.add(account)
    db.flush()
    db.add(
        DriveFileLocation(
            file_id=file.id,
            account_id=account.id,
            remote_file_id="remote-song",
            remote_name="remote-song.flac",
            size_bytes=len(content),
            sha1=file.sha1,
            state="healthy",
        )
    )
    db.commit()

    class FakeResponse:
        def __init__(self, body, range_header):
            self.status_code = 200
            self.headers = {"Content-Length": str(len(body)), "Accept-Ranges": "bytes"}
            self.body = body
            if range_header:
                start, end = (
                    int(value)
                    for value in range_header.removeprefix("bytes=").split("-")
                )
                self.body = body[start : end + 1]
                self.status_code = 206
                self.headers.update(
                    {
                        "Content-Length": str(len(self.body)),
                        "Content-Range": f"bytes {start}-{end}/{len(body)}",
                    }
                )

        def iter_bytes(self, chunk_size=1024 * 1024):
            del chunk_size
            yield self.body

    class FakeDownload:
        def __init__(self, body, range_header):
            self.response = FakeResponse(body, range_header)

        def close(self):
            return None

    class FakeClient:
        def open_download(self, remote_file_id, *, range_header=None):
            assert remote_file_id == "remote-song"
            return FakeDownload(content, range_header)

    monkeypatch.setattr(
        "app.services.delivery.client_for_account", lambda _db, _account: FakeClient()
    )
    params = _params(key, id=f"so:{track.opensubsonic_id}")
    song = api_client.get("/rest/getSong", params=params).json()["subsonic-response"]["song"]
    partial = api_client.get(
        "/rest/stream", params=params, headers={"Range": "bytes=2-6"}
    )
    assert song["size"] == len(content)
    assert song["suffix"] == "flac"
    assert partial.status_code == 206
    assert partial.content == content[2:7]


def test_xml_response_never_echoes_api_key(api_client, db, tmp_path):
    _user, key, *_ = _seed(db, tmp_path)
    response = api_client.get(
        "/rest/ping", params={"apiKey": key, "v": "1.16.1", "c": "test"}
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/xml")
    assert response.headers["cache-control"] == "private, no-store"
    assert key not in response.text


def test_player_secret_and_user_identity_are_absent_from_logs(
    api_client, db, tmp_path, caplog
):
    user, key, *_ = _seed(db, tmp_path)
    with caplog.at_level(logging.DEBUG):
        response = api_client.get("/rest/ping", params=_params(key))
    assert response.status_code == 200
    assert key not in caplog.text
    assert user.email not in caplog.text


def test_cover_art_is_owned_bounded_and_does_not_follow_outside_symlink(
    api_client, db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(library))
    monkeypatch.setattr(
        "app.opensubsonic.artwork.settings.opensubsonic_artwork_cache_path",
        str(tmp_path / "artwork-cache"),
    )
    _user, key, _playlist, _artist, album, _track, _item, _content = _seed(db, library)
    output = BytesIO()
    Image.new("RGB", (32, 32), "red").save(output, format="PNG")
    (library / "cover.png").write_bytes(output.getvalue())
    response = api_client.get(
        "/rest/getCoverArt", params=_params(key, id=f"ca:al:{album.opensubsonic_id}", size="16")
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert len(response.content) < 20_000

    (library / "cover.png").unlink()
    outside = tmp_path / "outside.png"
    outside.write_bytes(output.getvalue())
    (library / "cover.png").symlink_to(outside)
    blocked = api_client.get(
        "/rest/getCoverArt", params=_params(key, id=f"ca:al:{album.opensubsonic_id}")
    )
    assert blocked.json()["subsonic-response"]["error"]["code"] == 70


def test_cover_art_reads_bounded_embedded_picture_from_drive(
    api_client, db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(library))
    monkeypatch.setattr(
        "app.opensubsonic.artwork.settings.opensubsonic_artwork_cache_path",
        str(tmp_path / "artwork-cache"),
    )
    monkeypatch.setattr(
        "app.opensubsonic.artwork.settings.opensubsonic_artwork_remote_prefix_bytes",
        64 * 1024,
    )
    _user, key, _playlist, _artist, album, track, _item, _content = _seed(db, library)
    picture = BytesIO()
    Image.new("RGB", (32, 32), "green").save(picture, format="PNG")
    remote_body = _flac_with_picture(picture.getvalue())
    file = db.scalar(select(File).where(File.track_id == track.id))
    file.path = None
    file.size_bytes = len(remote_body)
    file.sha1 = hashlib.sha1(remote_body).hexdigest()
    account = StorageAccount(
        provider="google_drive",
        email="opensubsonic-artwork@example.test",
        root_folder_id="root",
        enabled=True,
        priority=1,
        state="healthy",
        credential_version=1,
    )
    db.add(account)
    db.flush()
    db.add(
        DriveFileLocation(
            file_id=file.id,
            account_id=account.id,
            remote_file_id="remote-artwork-song",
            remote_name="remote-artwork-song.flac",
            size_bytes=len(remote_body),
            sha1=file.sha1,
            state="healthy",
        )
    )
    db.commit()
    requested_ranges = []

    class FakeResponse:
        def iter_bytes(self, chunk_size=64 * 1024):
            del chunk_size
            yield remote_body

    class FakeDownload:
        response = FakeResponse()

        def close(self):
            return None

    class FakeClient:
        def open_download(self, remote_file_id, *, range_header=None):
            assert remote_file_id == "remote-artwork-song"
            requested_ranges.append(range_header)
            return FakeDownload()

    monkeypatch.setattr(
        "app.services.delivery.client_for_account", lambda _db, _account: FakeClient()
    )
    response = api_client.get(
        "/rest/getCoverArt",
        params=_params(key, id=f"ca:al:{album.opensubsonic_id}", size="16"),
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/jpeg")
    assert requested_ranges == ["bytes=0-65535"]


def test_cover_art_rejects_decompression_bomb_dimensions(
    api_client, db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(library))
    monkeypatch.setattr(
        "app.opensubsonic.artwork.settings.opensubsonic_artwork_cache_path",
        str(tmp_path / "artwork-cache"),
    )
    monkeypatch.setattr("app.opensubsonic.artwork.settings.opensubsonic_artwork_max_pixels", 64)
    _user, key, _playlist, _artist, album, _track, _item, _content = _seed(db, library)
    output = BytesIO()
    Image.new("RGB", (10, 10), "blue").save(output, format="PNG")
    (library / "cover.png").write_bytes(output.getvalue())
    response = api_client.get(
        "/rest/getCoverArt", params=_params(key, id=f"ca:al:{album.opensubsonic_id}")
    )
    assert response.json()["subsonic-response"]["error"]["code"] == 70
