from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from app.models import Album, Artist, File, Track
from app.services import scanner as scanner_service
from app.services.scanner import (
    LibraryPathError,
    _tag_value,
    iter_audio_files,
    read_audio_metadata,
    scan_library,
)


def _count(db, model) -> int:
    return db.scalar(select(func.count(model.id)))


def test_extension_filter_is_recursive_and_case_insensitive(tmp_path):
    expected = {"flac", "alac", "wav", "dsf", "dff", "ape"}
    for extension in expected:
        path = tmp_path / "nested" / f"track.{extension.upper()}"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    (tmp_path / "nested" / "ignored.mp3").touch()

    discovered = {
        path.suffix.casefold().lstrip(".") for path in iter_audio_files(tmp_path)
    }
    assert discovered == expected


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
    assert db.scalar(select(Artist).where(Artist.name == "Fallback Artist")) is None
    assert _count(db, File) == 3
    assert _count(db, Track) == 3


def test_retagged_file_updates_path_row_and_prunes_old_catalog(db, three_flac_library):
    scan_library(db, three_flac_library)
    path = next(three_flac_library.rglob("03 - Fallback Title.flac"))
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
