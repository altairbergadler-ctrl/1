from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

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
from app.services import scanner as scanner_service
from app.services.normalize import (
    normalize_album,
    normalize_artist,
    normalize_playlist_item,
    normalize_title,
)
from app.services.scanner import (
    LibraryPathError,
    _tag_value,
    iter_audio_files,
    normalize_catalog_key,
    read_audio_metadata,
    scan_library,
)


def _count(db, model) -> int:
    return db.scalar(select(func.count(model.id)))


def test_extension_filter_is_recursive_and_case_insensitive(tmp_path):
    expected = {"flac", "alac", "wav", "dsf", "dff", "ape", "mp3", "aac", "m4a"}
    for extension in expected:
        path = tmp_path / "nested" / f"track.{extension.upper()}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "nested" / "ignored.txt").touch()

    discovered = {
        path.suffix.casefold().lstrip(".") for path in iter_audio_files(tmp_path)
    }
    assert discovered == expected


@pytest.mark.parametrize(
    ("extension", "codec", "format_args"),
    [
        ("mp3", "libmp3lame", []),
        ("m4a", "aac", []),
        ("aac", "aac", ["-f", "adts"]),
    ],
)
def test_metadata_reader_accepts_yandex_fallback_containers(
    tmp_path, ffmpeg_binary, extension, codec, format_args
):
    library = tmp_path / "library"
    target = library / "Tagged Artist" / "Tagged Album" / f"01 - Fallback.{extension}"
    target.parent.mkdir(parents=True)
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
            codec,
            *format_args,
            "-y",
            str(target),
        ],
        check=True,
    )

    metadata = read_audio_metadata(target, library)

    assert metadata.format == extension
    assert metadata.duration_ms is not None
    assert metadata.duration_ms > 0
    assert metadata.artist == "Tagged Artist"
    assert metadata.album == "Tagged Album"


def test_extension_filter_skips_file_symlinks(tmp_path, monkeypatch):
    library = tmp_path / "library"
    library.mkdir()
    link = library / "linked.flac"
    link.touch()
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == link or original_is_symlink(path),
    )

    assert list(iter_audio_files(library)) == []


def test_metadata_reader_rejects_paths_outside_library(tmp_path):
    library = tmp_path / "library"
    library.mkdir()
    outside = tmp_path / "outside.flac"
    outside.touch()

    with pytest.raises(LibraryPathError, match="outside the library root"):
        read_audio_metadata(outside, library)


def test_scan_three_flac_files_is_idempotent(db, three_flac_library):
    first = scan_library(db, three_flac_library)

    assert first.discovered == 3
    assert first.added == 3
    assert first.failed == 0
    assert _count(db, File) == 3
    assert _count(db, Track) == 3
    assert _count(db, Album) == 2
    assert _count(db, Artist) == 2

    files = db.scalars(select(File).order_by(File.path)).all()
    assert all(item.format == "flac" for item in files)
    assert all(item.bit_depth == 16 for item in files)
    assert {item.sample_rate for item in files} == {44100, 48000, 96000}
    assert all(item.size_bytes > 0 for item in files)
    assert all(len(item.sha1) == 40 for item in files)
    assert len({item.sha1 for item in files}) == 3

    fallback_artist = db.scalar(select(Artist).where(Artist.name == "Fallback Artist"))
    fallback_album = db.scalar(
        select(Album).where(Album.artist_id == fallback_artist.id)
    )
    fallback_track = db.scalar(select(Track).where(Track.album_id == fallback_album.id))
    assert fallback_album.title == "Fallback Album"
    assert fallback_album.year == 2001
    assert fallback_track.title == "Fallback Title"
    assert fallback_track.track_no == 3

    tagged_track = db.scalar(select(Track).where(Track.title == "First Track"))
    assert tagged_track.isrc == "USAAA2400001"
    scanned_at = {item.id: item.scanned_at for item in files}

    second = scan_library(db, three_flac_library)

    assert second.discovered == 3
    assert second.added == 0
    assert second.unchanged == 3
    assert second.failed == 0
    assert _count(db, File) == 3
    assert _count(db, Track) == 3
    assert _count(db, Album) == 2
    assert _count(db, Artist) == 2
    rescanned = db.scalars(select(File)).all()
    assert all(item.scanned_at >= scanned_at[item.id] for item in rescanned)


def test_deleted_file_is_removed_from_catalog(db, three_flac_library):
    scan_library(db, three_flac_library)
    deleted = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    deleted.unlink()

    result = scan_library(db, three_flac_library)

    assert result.discovered == 2
    assert result.removed == 1
    assert _count(db, File) == 2
    assert _count(db, Track) == 2
    assert db.scalar(select(Artist).where(Artist.name == "Fallback Artist")) is None


def test_deleted_local_cache_keeps_verified_drive_catalog_row(
    db, three_flac_library
):
    scan_library(db, three_flac_library)
    deleted = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    library_file = db.scalar(select(File).where(File.path == str(deleted.resolve())))
    track_id = library_file.track_id
    account = StorageAccount(
        provider="google_drive",
        email="scanner@example.test",
        label="Scanner Drive",
        root_folder_id="root-id",
        enabled=True,
        priority=0,
        state="healthy",
        credential_version=1,
    )
    db.add(account)
    db.flush()
    db.add(
        DriveFileLocation(
            file_id=library_file.id,
            account_id=account.id,
            remote_file_id="remote-id",
            remote_name=deleted.name,
            size_bytes=library_file.size_bytes,
            sha1=library_file.sha1,
            state="healthy",
        )
    )
    db.commit()
    deleted.unlink()

    result = scan_library(db, three_flac_library)
    db.refresh(library_file)

    assert result.removed == 0
    assert library_file.path is None
    assert db.get(Track, track_id) is not None
    assert db.query(DriveFileLocation).count() == 1


def test_deleting_last_file_invalidates_ready_match(db, three_flac_library):
    scan_library(db, three_flac_library)
    deleted = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    library_file = db.scalar(select(File).where(File.path == str(deleted.resolve())))
    track_id = library_file.track_id
    from tests.helpers import ensure_user

    user = ensure_user(db)
    source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
    playlist = Playlist(
        source=source,
        user_id=user.id,
        external_id="deleted-track",
        name="Deleted track",
        track_count=1,
    )
    item = PlaylistItem(
        playlist=playlist,
        position=0,
        artist_raw="Fallback Artist",
        title_raw="Fallback Title",
    )
    match = Match(
        playlist_item=item,
        track_id=track_id,
        confidence=1.0,
        method="manual",
        status=MatchStatus.ready,
    )
    db.add_all([source, playlist, item, match])
    db.commit()
    deleted.unlink()

    result = scan_library(db, three_flac_library)
    db.refresh(match)

    assert result.removed == 1
    assert match.status == MatchStatus.missing
    assert match.track_id is None
    assert match.method == "none"
    assert match.confidence == 0.0
    assert db.get(Track, track_id) is None


@pytest.mark.parametrize(
    ("raw", "field_normalizer"),
    [
        ("Artist (feat. Guest)", normalize_artist),
        ("Album (2024 Remaster)", normalize_album),
        ("Song [Live]", normalize_title),
    ],
)
def test_catalog_normalizer_uses_shared_normalization_contract(raw, field_normalizer):
    assert normalize_catalog_key(raw) == field_normalizer(raw)


def test_scanned_flac_and_playlist_item_store_identical_normalized_keys(
    db, three_flac_library
):
    path = next(three_flac_library.rglob("01 - First Track.flac"))
    artist_raw = "Main Artist (feat. Guest)"
    album_raw = "Album — Name (2024 Remaster)"
    title_raw = "Song – Title [Live]"
    audio = FLAC(path)
    audio["artist"] = [artist_raw]
    audio["album"] = [album_raw]
    audio["title"] = [title_raw]
    audio.save()

    result = scan_library(db, three_flac_library)
    normalized = normalize_playlist_item(artist_raw, title_raw, album_raw)
    imported_item = PlaylistItem(
        position=0,
        artist_raw=artist_raw,
        title_raw=title_raw,
        album_raw=album_raw,
        **normalized,
    )
    scanned_file = db.scalar(select(File).where(File.path == str(path.resolve())))

    assert result.failed == 0
    assert (
        scanned_file.track.album.artist.name_norm,
        scanned_file.track.album.title_norm,
        scanned_file.track.title_norm,
    ) == (
        imported_item.artist_norm,
        imported_item.album_norm,
        imported_item.title_norm,
    )
    assert normalized == {
        "artist_norm": "main artist",
        "title_norm": "song-title",
        "album_norm": "album-name",
    }


def test_repeat_scan_relinks_legacy_catalog_norms_without_changing_file(
    db, three_flac_library
):
    path = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    scan_library(db, three_flac_library)
    library_file = db.scalar(select(File).where(File.path == str(path.resolve())))
    old_track_id = library_file.track_id
    library_file.track.album.artist.name_norm = "legacy fallback artist"
    library_file.track.album.title_norm = "legacy fallback album"
    library_file.track.title_norm = "legacy fallback title"
    library_file.track.album.artist.mbid = "artist-mbid"
    library_file.track.album.mbid = "album-mbid"
    library_file.track.mbid = "recording-mbid"
    library_file.track.isrc = "USAAA2400999"
    db.commit()

    result = scan_library(db, three_flac_library)
    db.refresh(library_file)

    assert result.updated == 1
    assert result.unchanged == 2
    assert library_file.track_id == old_track_id
    assert library_file.track.album.artist.name_norm == "fallback artist"
    assert library_file.track.album.title_norm == "fallback album"
    assert library_file.track.title_norm == "fallback title"
    assert library_file.track.album.artist.mbid == "artist-mbid"
    assert library_file.track.album.mbid == "album-mbid"
    assert library_file.track.mbid == "recording-mbid"
    assert library_file.track.isrc == "USAAA2400999"


def test_duplicate_sha1_does_not_create_a_file_row(db, three_flac_library):
    scan_library(db, three_flac_library)
    source = next(three_flac_library.rglob("01 - First Track.flac"))
    duplicate = (
        three_flac_library
        / "Copy Artist"
        / "Copy Album (2025)"
        / "01 - Physical Copy.FLAC"
    )
    duplicate.parent.mkdir(parents=True)
    shutil.copy2(source, duplicate)

    result = scan_library(db, three_flac_library)

    assert result.discovered == 4
    assert result.duplicate_content == 1
    assert result.failed == 0
    assert _count(db, File) == 3
    assert _count(db, Track) == 3


def test_broken_flac_is_reported_without_rolling_back_good_files(
    db, three_flac_library
):
    broken = three_flac_library / "Broken" / "Broken (2020)" / "01 - Broken.flac"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"this is not a FLAC file")

    result = scan_library(db, three_flac_library)

    assert result.discovered == 4
    assert result.added == 3
    assert result.failed == 1
    assert len(result.issues) == 1
    assert result.issues[0].path.endswith("01 - Broken.flac")
    assert _count(db, File) == 3


def test_moved_untagged_file_rebuilds_path_fallback_catalog(db, three_flac_library):
    scan_library(db, three_flac_library)
    source = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    original = db.scalar(select(File).where(File.path == str(source.resolve())))
    stable_ids = (
        original.track.opensubsonic_id,
        original.track.album.opensubsonic_id,
        original.track.album.artist.opensubsonic_id,
    )
    destination = (
        three_flac_library
        / "Moved Artist"
        / "Moved Album (2005)"
        / "04 - Moved Title.flac"
    )
    destination.parent.mkdir(parents=True)
    shutil.move(source, destination)

    result = scan_library(db, three_flac_library)

    assert result.moved == 1
    moved_file = db.scalar(select(File).where(File.path == str(destination.resolve())))
    assert moved_file.track.title == "Moved Title"
    assert moved_file.track.track_no == 4
    assert moved_file.track.album.title == "Moved Album"
    assert moved_file.track.album.year == 2005
    assert moved_file.track.album.artist.name == "Moved Artist"
    assert (
        moved_file.track.opensubsonic_id,
        moved_file.track.album.opensubsonic_id,
        moved_file.track.album.artist.opensubsonic_id,
    ) == stable_ids
    assert db.scalar(select(Artist).where(Artist.name == "Fallback Artist")) is None
    assert _count(db, File) == 3
    assert _count(db, Track) == 3


def test_retagged_file_updates_path_row_and_prunes_old_catalog(db, three_flac_library):
    scan_library(db, three_flac_library)
    path = next(three_flac_library.rglob("03 - Fallback Title.flac"))
    original = db.scalar(select(File).where(File.path == str(path.resolve())))
    stable_ids = (
        original.track.opensubsonic_id,
        original.track.album.opensubsonic_id,
        original.track.album.artist.opensubsonic_id,
    )
    audio = FLAC(path)
    audio["artist"] = ["Replacement Artist"]
    audio["album"] = ["Replacement Album"]
    audio["title"] = ["Replacement Title"]
    audio["date"] = ["2025"]
    audio["tracknumber"] = ["7"]
    audio["isrc"] = ["GBBBB2500002"]
    audio.save()

    result = scan_library(db, three_flac_library)

    assert result.updated == 1
    replacement = db.scalar(select(Track).where(Track.title == "Replacement Title"))
    assert replacement.track_no == 7
    assert replacement.isrc == "GBBBB2500002"
    assert replacement.album.title == "Replacement Album"
    assert replacement.album.artist.name == "Replacement Artist"
    assert (
        replacement.opensubsonic_id,
        replacement.album.opensubsonic_id,
        replacement.album.artist.opensubsonic_id,
    ) == stable_ids
    assert db.scalar(select(Artist).where(Artist.name == "Fallback Artist")) is None
    assert _count(db, File) == 3
    assert _count(db, Track) == 3


def test_removing_isrc_tag_clears_stale_track_isrc(db, three_flac_library):
    scan_library(db, three_flac_library)
    path = next(three_flac_library.rglob("01 - First Track.flac"))
    audio = FLAC(path)
    del audio["isrc"]
    audio.save()

    result = scan_library(db, three_flac_library)

    assert result.updated == 1
    track = db.scalar(select(Track).where(Track.title == "First Track"))
    assert track.isrc is None


def test_empty_preferred_tag_falls_back_to_valid_alias():
    tags = {"albumartist": [""], "artist": ["Actual Artist"]}
    assert _tag_value(tags, ("albumartist", "artist")) == "Actual Artist"


def test_database_operational_error_fails_the_scan(db, three_flac_library, monkeypatch):
    def fail_upsert(_db, _metadata):
        raise OperationalError("insert", {}, RuntimeError("database unavailable"))

    monkeypatch.setattr(scanner_service, "upsert_audio_file", fail_upsert)
    with pytest.raises(OperationalError):
        scan_library(db, three_flac_library)


def test_unexpected_programming_error_fails_the_scan(
    db, three_flac_library, monkeypatch
):
    def fail_metadata(*_args, **_kwargs):
        raise RuntimeError("unexpected scanner defect")

    monkeypatch.setattr(scanner_service, "read_audio_metadata", fail_metadata)
    with pytest.raises(RuntimeError, match="unexpected scanner defect"):
        scan_library(db, three_flac_library)
