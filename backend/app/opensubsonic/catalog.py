from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Album,
    Artist,
    DriveFileLocation,
    File,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    Track,
)
from app.services.delivery import (
    DeliveryFileUnavailable,
    best_playable_file,
    playable_file_size,
    playable_file_suffix,
)


def artist_id(row: Artist) -> str:
    return f"ar:{row.opensubsonic_id}"


def album_id(row: Album) -> str:
    return f"al:{row.opensubsonic_id}"


def song_id(row: Track) -> str:
    return f"so:{row.opensubsonic_id}"


def playlist_id(row: Playlist) -> str:
    return f"pl:{row.opensubsonic_id}"


def album_cover_id(row: Album) -> str:
    return f"ca:al:{row.opensubsonic_id}"


def _claimed_playable(track: Track) -> bool:
    try:
        best_playable_file(track)
        return True
    except DeliveryFileUnavailable:
        return False


def visible_tracks(db: Session, user_id: int) -> list[Track]:
    rows = db.scalars(
        select(Track)
        .join(Match, Match.track_id == Track.id)
        .join(PlaylistItem, PlaylistItem.id == Match.playlist_item_id)
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(Playlist.user_id == user_id, Match.status == MatchStatus.ready)
        .options(
            selectinload(Track.album).selectinload(Album.artist),
            selectinload(Track.files)
            .selectinload(File.drive_locations)
            .selectinload(DriveFileLocation.account),
        )
        .distinct()
        .order_by(Track.id)
    ).unique().all()
    return [row for row in rows if _claimed_playable(row)]


def find_song(db: Session, user_id: int, public_id: str) -> Track | None:
    return next(
        (row for row in visible_tracks(db, user_id) if song_id(row) == public_id),
        None,
    )


def find_album(db: Session, user_id: int, public_id: str) -> Album | None:
    tracks = visible_tracks(db, user_id)
    return next((row.album for row in tracks if album_id(row.album) == public_id), None)


def find_artist(db: Session, user_id: int, public_id: str) -> Artist | None:
    tracks = visible_tracks(db, user_id)
    return next(
        (row.album.artist for row in tracks if artist_id(row.album.artist) == public_id),
        None,
    )


def visible_playlist(db: Session, user_id: int, public_id: str) -> Playlist | None:
    return db.scalar(
        select(Playlist).where(
            Playlist.user_id == user_id,
            Playlist.opensubsonic_id == public_id.removeprefix("pl:"),
        )
    ) if public_id.startswith("pl:") else None


def playlist_tracks(db: Session, playlist: Playlist) -> list[tuple[PlaylistItem, Track]]:
    items = db.scalars(
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist.id)
        .options(
            selectinload(PlaylistItem.match)
            .selectinload(Match.track)
            .selectinload(Track.album)
            .selectinload(Album.artist),
            selectinload(PlaylistItem.match)
            .selectinload(Match.track)
            .selectinload(Track.files)
            .selectinload(File.drive_locations)
            .selectinload(DriveFileLocation.account),
        )
        .order_by(PlaylistItem.position, PlaylistItem.id)
    ).unique().all()
    return [
        (item, item.match.track)
        for item in items
        if item.match is not None
        and item.match.status == MatchStatus.ready
        and item.match.track is not None
        and _claimed_playable(item.match.track)
    ]


def changed(value) -> str:
    if value is None:
        return "1970-01-01T00:00:00Z"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def song_dto(track: Track) -> dict:
    file, path, remote = best_playable_file(track)
    suffix = playable_file_suffix(file, path, remote) or "flac"
    mime = {
        "flac": "audio/flac",
        "mp3": "audio/mpeg",
        "m4a": "audio/mp4",
        "aac": "audio/aac",
        "ogg": "audio/ogg",
        "opus": "audio/ogg",
        "wav": "audio/wav",
    }.get(suffix, "application/octet-stream")
    album = track.album
    artist = album.artist
    return {
        "id": song_id(track),
        "parent": album_id(album),
        "title": track.title,
        "album": album.title,
        "artist": artist.name,
        "artistId": artist_id(artist),
        "albumId": album_id(album),
        "isDir": False,
        "isVideo": False,
        "type": "music",
        "duration": max(0, round((track.duration_ms or 0) / 1000)),
        "track": track.track_no,
        "discNumber": track.disc_no,
        "year": album.year,
        "coverArt": album_cover_id(album),
        "size": playable_file_size(file, path, remote),
        "suffix": suffix,
        "contentType": mime,
        "bitDepth": file.bit_depth,
        "samplingRate": file.sample_rate,
        "transcodedContentType": None,
        "transcodedSuffix": None,
    }


def album_created_at(songs: list[Track]) -> datetime | None:
    values = [
        file.scanned_at
        for song in songs
        for file in song.files
        if file.scanned_at is not None
    ]
    return min(values) if values else None


def album_dto(album: Album, songs: list[Track]) -> dict:
    return {
        "id": album_id(album),
        "parent": artist_id(album.artist),
        "name": album.title,
        "title": album.title,
        "artist": album.artist.name,
        "artistId": artist_id(album.artist),
        "coverArt": album_cover_id(album),
        "songCount": len(songs),
        "duration": sum(max(0, round((song.duration_ms or 0) / 1000)) for song in songs),
        "created": changed(album_created_at(songs)),
        "year": album.year,
        "isDir": True,
    }


def artist_dto(artist: Artist, albums: list[Album]) -> dict:
    return {
        "id": artist_id(artist),
        "name": artist.name,
        "albumCount": len(albums),
    }
