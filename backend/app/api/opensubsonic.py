from __future__ import annotations

import mimetypes
import random
from collections import defaultdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.background import BackgroundTask

from app.db import get_db
from app.models import Album, Match, MatchStatus, Playlist, PlaylistItem, Track
from app.opensubsonic.auth import authenticate
from app.opensubsonic.catalog import (
    album_dto,
    album_created_at,
    album_id,
    artist_dto,
    artist_id,
    changed,
    find_album,
    find_artist,
    find_song,
    playlist_id,
    playlist_tracks,
    song_dto,
    song_id,
    visible_playlist,
    visible_tracks,
)
from app.opensubsonic.protocol import (
    OpenSubsonicError,
    error_payload,
    payload,
    response as protocol_response,
    validate_version,
)
from app.services.delivery import (
    DeliveryFileUnavailable,
    DeliveryNotReady,
    DeliveryResourceNotFound,
    open_remote_download,
    playlist_item_entry,
)

router = APIRouter()


def _integer(request: Request, name: str, default: int, *, minimum=0, maximum=None) -> int:
    raw = request.query_params.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise OpenSubsonicError(0, f"Invalid {name}")
    if value < minimum or (maximum is not None and value > maximum):
        raise OpenSubsonicError(0, f"Invalid {name}")
    return value


def _required(request: Request, name: str) -> str:
    value = request.query_params.get(name)
    if not value:
        raise OpenSubsonicError(10, "Required parameter is missing")
    return value


def _albums_for_tracks(tracks: list[Track]) -> dict[int, tuple[Album, list[Track]]]:
    result: dict[int, tuple[Album, list[Track]]] = {}
    for track in tracks:
        result.setdefault(track.album.id, (track.album, []))[1].append(track)
    return result


def _playlist_dto(db: Session, playlist: Playlist) -> dict:
    rows = playlist_tracks(db, playlist)
    return {
        "id": playlist_id(playlist),
        "name": playlist.name,
        "songCount": len(rows),
        "duration": sum(max(0, round((track.duration_ms or 0) / 1000)) for _, track in rows),
        "created": changed(playlist.created_at),
        "changed": changed(playlist.sync_changed_at),
        "public": False,
        # Playlist mutations are intentionally outside this read-only adapter. Clients such as
        # Symfonium use this flag to keep their imported copy server-owned and auto-synced.
        "readonly": True,
    }


def _catalog_response(method: str, request: Request, db: Session, user_id: int) -> dict:
    if method == "ping":
        return payload()
    if method == "getLicense":
        return payload({"license": {"valid": True}})
    if method == "getMusicFolders":
        return payload(
            {"musicFolders": {"musicFolder": [{"id": "mf:audiofeel", "name": "Audiofeel"}]}}
        )
    if method == "getStarred2":
        return payload({"starred2": {"artist": [], "album": [], "song": []}})
    if method == "getBookmarks":
        return payload({"bookmarks": {"bookmark": []}})
    if method == "getGenres":
        return payload({"genres": {"genre": []}})
    tracks = visible_tracks(db, user_id)
    albums = _albums_for_tracks(tracks)
    artists: dict[int, tuple[object, list[Album]]] = {}
    for album, _songs in albums.values():
        artists.setdefault(album.artist.id, (album.artist, []))[1].append(album)
    if method == "getArtists":
        indexes: dict[str, list[dict]] = defaultdict(list)
        for artist, artist_albums in sorted(
            artists.values(), key=lambda row: (row[0].name.casefold(), row[0].id)
        ):
            first = artist.name[:1].upper() if artist.name else "#"
            if not first.isalpha():
                first = "#"
            indexes[first].append(artist_dto(artist, artist_albums))
        return payload(
            {
                "artists": {
                    "ignoredArticles": "",
                    "index": [
                        {"name": name, "artist": rows}
                        for name, rows in sorted(indexes.items())
                    ],
                }
            }
        )
    if method == "getArtist":
        artist = find_artist(db, user_id, _required(request, "id"))
        if artist is None:
            raise OpenSubsonicError(70, "Resource not found")
        artist_albums = [
            (album, songs) for album, songs in albums.values() if album.artist_id == artist.id
        ]
        body = artist_dto(artist, [album for album, _ in artist_albums])
        body["album"] = [album_dto(album, songs) for album, songs in artist_albums]
        return payload({"artist": body})
    if method == "getAlbum":
        album = find_album(db, user_id, _required(request, "id"))
        if album is None:
            raise OpenSubsonicError(70, "Resource not found")
        songs = albums[album.id][1]
        body = album_dto(album, songs)
        body["song"] = [song_dto(song) for song in songs]
        return payload({"album": body})
    if method == "getSong":
        song = find_song(db, user_id, _required(request, "id"))
        if song is None:
            raise OpenSubsonicError(70, "Resource not found")
        return payload({"song": song_dto(song)})
    if method == "getAlbumList2":
        size = _integer(request, "size", 10, minimum=1, maximum=500)
        offset = _integer(request, "offset", 0)
        kind = _required(request, "type")
        rows = list(albums.values())
        if kind == "random":
            random.SystemRandom().shuffle(rows)
        elif kind == "newest":
            rows.sort(
                key=lambda row: (album_created_at(row[1]) is not None, album_created_at(row[1])),
                reverse=True,
            )
        elif kind == "alphabeticalByName":
            rows.sort(key=lambda row: (row[0].title.casefold(), row[0].id))
        elif kind == "alphabeticalByArtist":
            rows.sort(
                key=lambda row: (
                    row[0].artist.name.casefold(),
                    row[0].title.casefold(),
                    row[0].id,
                )
            )
        elif kind == "byYear":
            _required(request, "fromYear")
            _required(request, "toYear")
            from_year = _integer(request, "fromYear", 0)
            to_year = _integer(request, "toYear", 0)
            lower, upper = sorted((from_year, to_year))
            rows = [
                row
                for row in rows
                if row[0].year is not None and lower <= row[0].year <= upper
            ]
            rows.sort(
                key=lambda row: (row[0].year or 0, row[0].title.casefold(), row[0].id),
                reverse=from_year > to_year,
            )
        elif kind == "byGenre":
            _required(request, "genre")
            rows = []
        elif kind in {"highest", "frequent", "recent", "starred"}:
            # The catalog has no rating, playback-history, or starred state.
            rows = []
        else:
            raise OpenSubsonicError(0, "Invalid album list type")
        return payload(
            {"albumList2": {"album": [album_dto(a, s) for a, s in rows[offset : offset + size]]}}
        )
    if method == "search3":
        query = request.query_params.get("query", "").casefold().strip()
        if query == '""':
            query = ""
        artist_count = _integer(request, "artistCount", 20, maximum=500)
        artist_offset = _integer(request, "artistOffset", 0)
        album_count = _integer(request, "albumCount", 20, maximum=500)
        album_offset = _integer(request, "albumOffset", 0)
        song_count = _integer(request, "songCount", 20, maximum=500)
        song_offset = _integer(request, "songOffset", 0)
        artist_rows = [
            artist_dto(artist, artist_albums)
            for artist, artist_albums in sorted(
                artists.values(), key=lambda row: (row[0].name.casefold(), row[0].id)
            )
            if not query or query in artist.name.casefold()
        ]
        album_rows = [
            album_dto(album, songs)
            for album, songs in sorted(
                albums.values(), key=lambda row: (row[0].title.casefold(), row[0].id)
            )
            if not query
            or query in album.title.casefold()
            or query in album.artist.name.casefold()
        ]
        song_rows = [
            song_dto(song)
            for song in sorted(tracks, key=lambda row: (row.title.casefold(), row.id))
            if not query
            or query in song.title.casefold()
            or query in song.album.title.casefold()
            or query in song.album.artist.name.casefold()
        ]
        return payload(
            {
                "searchResult3": {
                    "artist": artist_rows[artist_offset : artist_offset + artist_count],
                    "album": album_rows[album_offset : album_offset + album_count],
                    "song": song_rows[song_offset : song_offset + song_count],
                }
            }
        )
    if method == "getPlaylists":
        if request.query_params.get("username"):
            raise OpenSubsonicError(50, "Not authorized")
        rows = db.scalars(
            select(Playlist)
            .where(Playlist.user_id == user_id)
            .order_by(Playlist.name, Playlist.id)
        ).all()
        return payload({"playlists": {"playlist": [_playlist_dto(db, row) for row in rows]}})
    if method == "getPlaylist":
        playlist = visible_playlist(db, user_id, _required(request, "id"))
        if playlist is None:
            raise OpenSubsonicError(70, "Resource not found")
        body = _playlist_dto(db, playlist)
        body["entry"] = [song_dto(track) for _, track in playlist_tracks(db, playlist)]
        return payload({"playlist": body})
    raise OpenSubsonicError(0, "Method is not implemented")


def _item_id_for_song(db: Session, user_id: int, public_id: str) -> int | None:
    if not public_id.startswith("so:"):
        return None
    return db.scalar(
        select(PlaylistItem.id)
        .join(Match, Match.playlist_item_id == PlaylistItem.id)
        .join(Track, Track.id == Match.track_id)
        .join(Playlist, Playlist.id == PlaylistItem.playlist_id)
        .where(
            Playlist.user_id == user_id,
            Match.status == MatchStatus.ready,
            Track.opensubsonic_id == public_id.removeprefix("so:"),
        )
        .order_by(PlaylistItem.id)
        .limit(1)
    )


def _binary(method: str, request: Request, db: Session, user_id: int) -> Response:
    if method == "getCoverArt":
        from app.opensubsonic.artwork import cover_art_response

        return cover_art_response(request, db, user_id, _required(request, "id"))
    public_id = _required(request, "id")
    if find_song(db, user_id, public_id) is None:
        raise OpenSubsonicError(70, "Resource not found")
    requested_format = request.query_params.get("format")
    max_bit_rate = request.query_params.get("maxBitRate")
    if (requested_format and requested_format.casefold() not in {"raw"}) or (
        max_bit_rate and max_bit_rate != "0"
    ):
        raise OpenSubsonicError(0, "Transcoding is not supported")
    item_id = _item_id_for_song(db, user_id, public_id)
    if item_id is None:
        raise OpenSubsonicError(70, "Resource not found")
    try:
        entry = playlist_item_entry(db, item_id, user_id)
    except (DeliveryResourceNotFound, DeliveryNotReady, DeliveryFileUnavailable) as exc:
        raise OpenSubsonicError(70, "Resource not found") from exc
    media_type = mimetypes.guess_type(entry.filename)[0] or "application/octet-stream"
    disposition = "attachment" if method == "download" else "inline"
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"{disposition}; filename*=UTF-8''{quote(entry.filename)}",
        "Cache-Control": "private, no-store",
    }
    if entry.path is not None:
        return FileResponse(
            entry.path,
            media_type=media_type,
            filename=entry.filename if method == "download" else None,
            headers=headers,
        )
    try:
        download = open_remote_download(db, entry, range_header=request.headers.get("range"))
    except DeliveryFileUnavailable as exc:
        raise OpenSubsonicError(70, "Resource not found") from exc
    for name in ("Content-Length", "Content-Range", "ETag", "Last-Modified"):
        value = download.response.headers.get(name)
        if value:
            headers[name] = value
    if request.method == "HEAD":
        download.close()
        return Response(
            status_code=download.response.status_code,
            media_type=media_type,
            headers=headers,
        )
    return StreamingResponse(
        download.response.iter_bytes(chunk_size=1024 * 1024),
        status_code=download.response.status_code,
        media_type=media_type,
        headers=headers,
        background=BackgroundTask(download.close),
    )


@router.api_route("/{raw_method}", methods=["GET", "HEAD"])
def opensubsonic_method(
    raw_method: str, request: Request, db: Session = Depends(get_db)
):
    method = raw_method[:-5] if raw_method.endswith(".view") else raw_method
    try:
        if method == "getOpenSubsonicExtensions":
            validate_version(request, optional=True)
            return protocol_response(
                request,
                payload(
                    {
                        "openSubsonicExtensions": [
                            {"name": "apiKeyAuthentication", "versions": [1]}
                        ]
                    }
                ),
            )
        credential = authenticate(request, db)
        if method in {"stream", "download", "getCoverArt"}:
            return _binary(method, request, db, credential.user_id)
        return protocol_response(
            request, _catalog_response(method, request, db, credential.user_id)
        )
    except OpenSubsonicError as exc:
        headers = {"Retry-After": "60"} if exc.http_status == 429 else None
        result = protocol_response(
            request,
            error_payload(exc.code, exc.message),
            status_code=exc.http_status,
        )
        if headers:
            result.headers.update(headers)
        return result
