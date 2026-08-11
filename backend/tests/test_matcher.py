from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.models import (
    Album,
    Artist,
    File,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    Track,
)
from app.services.matcher import (
    MatchMethod,
    get_review_candidates,
    load_catalog,
    match_playlist_item,
    run_matching,
)
from tests.helpers import ensure_user


def _playlist(db, *, external_id: str = "playlist-1") -> Playlist:
    user = ensure_user(db)
    source = db.scalar(
        select(PlaylistSource).where(
            PlaylistSource.user_id == user.id,
            PlaylistSource.service == ServiceEnum.spotify,
        )
    )
    if source is None:
        source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
        db.add(source)
        db.flush()
    playlist = Playlist(
        source=source,
        user_id=user.id,
        external_id=external_id,
        name=f"Playlist {external_id}",
        snapshot_hash=f"snapshot-{external_id}",
    )
    db.add(playlist)
    db.flush()
    return playlist


def _item(
    db,
    playlist: Playlist,
    *,
    position: int,
    artist: str,
    title: str,
    album: str,
    duration_ms: int | None,
    isrc: str | None = None,
) -> PlaylistItem:
    item = PlaylistItem(
        playlist=playlist,
        position=position,
        artist_raw=artist,
        title_raw=title,
        album_raw=album,
        artist_norm=artist.casefold(),
        title_norm=title.casefold(),
        album_norm=album.casefold(),
        duration_ms=duration_ms,
        isrc=isrc,
        external_track_id=f"external-{playlist.external_id}-{position}",
    )
    db.add(item)
    db.flush()
    return item


def _catalog_track(
    db,
    tmp_path: Path,
    *,
    artist: str,
    title: str,
    album: str,
    duration_ms: int | None,
    isrc: str | None = None,
    with_file: bool = True,
    bit_depth: int = 16,
    sample_rate: int = 44_100,
    suffix: str = ".flac",
) -> Track:
    artist_row = db.scalar(select(Artist).where(Artist.name_norm == artist.casefold()))
    if artist_row is None:
        artist_row = Artist(name=artist, name_norm=artist.casefold())
        db.add(artist_row)
        db.flush()

    album_row = db.scalar(
        select(Album).where(
            Album.artist_id == artist_row.id,
            Album.title_norm == album.casefold(),
        )
    )
    if album_row is None:
        album_row = Album(
            artist=artist_row,
            title=album,
            title_norm=album.casefold(),
        )
        db.add(album_row)
        db.flush()

    track = Track(
        album=album_row,
        title=title,
        title_norm=title.casefold(),
        duration_ms=duration_ms,
        isrc=isrc,
    )
    db.add(track)
    db.flush()
    if with_file:
        path = tmp_path / f"track-{track.id}{suffix}"
        path.write_bytes(f"audio-{track.id}".encode())
        db.add(
            File(
                track=track,
                path=str(path),
                format=suffix.lstrip("."),
                bit_depth=bit_depth,
                sample_rate=sample_rate,
                size_bytes=path.stat().st_size,
                sha1=f"{track.id:040x}",
            )
        )
        db.flush()
    return track


def test_isrc_is_first_cascade_step_and_wins_over_metadata(db, tmp_path):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Metadata Artist",
        title="Metadata Title",
        album="Metadata Album",
        duration_ms=180_000,
        isrc="US-AAA-24-00001",
    )
    _catalog_track(
        db,
        tmp_path,
        artist="Metadata Artist",
        title="Metadata Title",
        album="Metadata Album",
        duration_ms=180_000,
        isrc="USAAA2400999",
    )
    isrc_track = _catalog_track(
        db,
        tmp_path,
        artist="Different Artist",
        title="Different Title",
        album="Different Album",
        duration_ms=250_000,
        isrc="USAAA2400001",
        bit_depth=24,
        sample_rate=96_000,
    )

    decision = match_playlist_item(item, load_catalog(db))

    assert decision.track_id == isrc_track.id
    assert decision.method == MatchMethod.isrc
    assert decision.status == MatchStatus.ready
    assert decision.confidence == 1.0


@pytest.mark.parametrize(
    ("difference_ms", "expected_method"),
    [(2_000, MatchMethod.exact), (2_001, MatchMethod.fuzzy)],
)
def test_exact_duration_boundary_is_inclusive(
    db, tmp_path, difference_ms, expected_method
):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Exact Artist",
        title="Exact Title",
        album="Exact Album",
        duration_ms=180_000 + difference_ms,
    )
    track = _catalog_track(
        db,
        tmp_path,
        artist="Exact Artist",
        title="Exact Title",
        album="Exact Album",
        duration_ms=180_000,
    )

    decision = match_playlist_item(item, load_catalog(db))

    assert decision.track_id == track.id
    assert decision.method == expected_method
    assert decision.status == MatchStatus.ready
    assert decision.confidence >= 0.9


def test_ambiguous_fuzzy_match_requires_review_and_returns_ranked_candidates(
    db, tmp_path
):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="The Example Band",
        title="Northern Lights",
        album="A Playlist Album",
        duration_ms=201_000,
    )
    first = _catalog_track(
        db,
        tmp_path,
        artist="Example Band",
        title="The Northern Lights",
        album="Studio Album",
        duration_ms=200_000,
    )
    second = _catalog_track(
        db,
        tmp_path,
        artist="The Example Band",
        title="Northern Lights Extended",
        album="Another Album",
        duration_ms=202_000,
    )

    decision = match_playlist_item(item, load_catalog(db))
    candidates = get_review_candidates(db, item, limit=5)

    assert decision.status == MatchStatus.needs_review
    assert decision.method == MatchMethod.fuzzy
    assert decision.track_id in {first.id, second.id}
    assert 0.7 <= decision.confidence < 0.9
    assert {candidate.track_id for candidate in candidates} == {first.id, second.id}
    assert candidates == sorted(
        candidates,
        key=lambda candidate: (-candidate.confidence, candidate.track_id),
    )


def test_live_version_does_not_become_a_false_exact_ready_match(db, tmp_path):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Versioned Artist",
        title="One Song (Live)",
        album="One Album",
        duration_ms=180_000,
    )
    # Stage 3 normalization deliberately strips a live suffix. This test keeps
    # that normalized key to ensure the matcher still compares edition markers.
    item.title_norm = "one song"
    _catalog_track(
        db,
        tmp_path,
        artist="Versioned Artist",
        title="One Song",
        album="One Album",
        duration_ms=180_000,
    )

    decision = match_playlist_item(item, load_catalog(db))

    assert decision.method != MatchMethod.exact
    assert decision.status != MatchStatus.ready
    assert decision.confidence < 0.9


def test_live_album_marker_does_not_become_a_false_exact_match(db, tmp_path):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Album Artist",
        title="Album Song",
        album="Concert (Live)",
        duration_ms=180_000,
    )
    item.album_norm = "concert"
    _catalog_track(
        db,
        tmp_path,
        artist="Album Artist",
        title="Album Song",
        album="Concert",
        duration_ms=180_000,
    )

    decision = match_playlist_item(item, load_catalog(db))

    assert decision.method != MatchMethod.exact
    assert decision.status != MatchStatus.ready


def test_manual_ready_is_recomputed_after_its_track_loses_every_file(db, tmp_path):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Manual Artist",
        title="Manual Title",
        album="Manual Album",
        duration_ms=180_000,
    )
    track = _catalog_track(
        db,
        tmp_path,
        artist="Manual Artist",
        title="Manual Title",
        album="Manual Album",
        duration_ms=180_000,
    )
    db.commit()
    run_matching(db, playlist.user_id, playlist.id)
    item.match.method = MatchMethod.manual.value
    item.match.confidence = 1.0
    library_file = db.scalar(select(File).where(File.track_id == track.id))
    db.delete(library_file)
    db.commit()

    summary = run_matching(db, playlist.user_id, playlist.id)

    assert summary.missing == 1
    assert item.match.status == MatchStatus.missing
    assert item.match.track_id is None
    assert item.match.method == MatchMethod.none.value


def test_catalog_track_without_a_file_is_never_ready(db, tmp_path):
    playlist = _playlist(db)
    item = _item(
        db,
        playlist,
        position=0,
        artist="Unavailable Artist",
        title="Unavailable Title",
        album="Unavailable Album",
        duration_ms=180_000,
        isrc="GBBBB2400001",
    )
    _catalog_track(
        db,
        tmp_path,
        artist="Unavailable Artist",
        title="Unavailable Title",
        album="Unavailable Album",
        duration_ms=180_000,
        isrc="GBBBB2400001",
        with_file=False,
    )

    decision = match_playlist_item(item, load_catalog(db))

    assert decision.track_id is None
    assert decision.method == MatchMethod.none
    assert decision.status == MatchStatus.missing
    assert decision.confidence == 0.0


def test_run_matching_is_idempotent_and_can_be_scoped_to_one_playlist(db, tmp_path):
    first_playlist = _playlist(db, external_id="first")
    second_playlist = _playlist(db, external_id="second")
    ready_item = _item(
        db,
        first_playlist,
        position=0,
        artist="Ready Artist",
        title="Ready Title",
        album="Ready Album",
        duration_ms=180_000,
        isrc="USCCC2400001",
    )
    missing_item = _item(
        db,
        first_playlist,
        position=1,
        artist="Missing Artist",
        title="Missing Title",
        album="Missing Album",
        duration_ms=180_000,
    )
    untouched_item = _item(
        db,
        second_playlist,
        position=0,
        artist="Other Artist",
        title="Other Title",
        album="Other Album",
        duration_ms=180_000,
    )
    track = _catalog_track(
        db,
        tmp_path,
        artist="Ready Artist",
        title="Ready Title",
        album="Ready Album",
        duration_ms=180_000,
        isrc="USCCC2400001",
    )
    db.commit()

    first = run_matching(db, first_playlist.user_id, playlist_id=first_playlist.id)
    second = run_matching(db, first_playlist.user_id, playlist_id=first_playlist.id)

    assert first.to_dict() == {
        "total": 2,
        "ready": 1,
        "needs_review": 0,
        "missing": 1,
    }
    assert second.to_dict() == first.to_dict()
    assert db.scalar(select(func.count(Match.id))) == 2
    assert ready_item.match.track_id == track.id
    assert ready_item.match.status == MatchStatus.ready
    assert missing_item.match.track_id is None
    assert missing_item.match.status == MatchStatus.missing
    assert untouched_item.match is None
