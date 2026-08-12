"""Transaction-bound playlist revision reconciliation."""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import event, inspect, select, text
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
    StorageAccount,
    Track,
    utcnow,
)
from app.services.delivery import (
    DeliveryFileUnavailable,
    best_playable_file,
    playable_file_size,
    playable_file_suffix,
)

WATCHED = (
    Playlist,
    PlaylistItem,
    Match,
    Track,
    Album,
    Artist,
    File,
    DriveFileLocation,
    StorageAccount,
)
_FLAG = "opensubsonic_reconcile"
_ROWS = "opensubsonic_reconcile_rows"
_PLAYLIST_IDS = "opensubsonic_reconcile_playlist_ids"
_RELATED_IDS = "opensubsonic_reconcile_related_ids"


def _fingerprint(session: Session, playlist: Playlist) -> bytes:
    digest = hashlib.sha256()
    digest.update(b"audiofeel-playlist-sync-v1\0")
    digest.update((playlist.name or "").encode("utf-8"))
    rows = session.scalars(
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
    for item in rows:
        if (
            item.match is None
            or item.match.status != MatchStatus.ready
            or item.match.track is None
        ):
            continue
        track = item.match.track
        try:
            file, path, remote = best_playable_file(track)
            size = playable_file_size(file, path, remote)
            suffix = playable_file_suffix(file, path, remote)
        except DeliveryFileUnavailable:
            continue
        digest.update(
            (
                f"\0{item.position}\0{track.opensubsonic_id}\0{track.title or ''}\0"
                f"{track.album.title or ''}\0{track.album.artist.name or ''}\0"
                f"{file.sha1 or ''}\0{suffix}\0{size or 0}\0"
                f"{file.bit_depth or 0}\0{file.sample_rate or 0}"
            ).encode("utf-8")
        )
    return digest.digest()


def _attribute_ids(row, name: str) -> set[int]:
    attribute = inspect(row).attrs[name]
    values = (*attribute.history.added, *attribute.history.unchanged, *attribute.history.deleted)
    current = getattr(row, name, None)
    return {
        int(value)
        for value in (*values, current)
        if value is not None
    }


@event.listens_for(Session, "before_flush")
def _mark_reconciliation(session: Session, _flush_context, _instances) -> None:
    rows = {
        row
        for row in session.new | session.dirty | session.deleted
        if isinstance(row, WATCHED)
    }
    if not rows:
        return
    session.info[_FLAG] = True
    session.info.setdefault(_ROWS, set()).update(rows)
    playlist_ids = session.info.setdefault(_PLAYLIST_IDS, set())
    related = session.info.setdefault(
        _RELATED_IDS,
        {
            "item_ids": set(),
            "track_ids": set(),
            "file_ids": set(),
            "album_ids": set(),
            "artist_ids": set(),
            "account_ids": set(),
        },
    )
    for row in rows:
        if isinstance(row, Playlist) and row.id is not None:
            playlist_ids.add(int(row.id))
        elif isinstance(row, PlaylistItem):
            playlist_ids.update(_attribute_ids(row, "playlist_id"))
            if row.playlist is not None and row.playlist.id is not None:
                playlist_ids.add(int(row.playlist.id))
        elif isinstance(row, Match):
            related["item_ids"].update(_attribute_ids(row, "playlist_item_id"))
            related["track_ids"].update(_attribute_ids(row, "track_id"))
            if row.playlist_item is not None:
                item = row.playlist_item
                if item.playlist_id is not None:
                    playlist_ids.add(int(item.playlist_id))
                elif item.playlist is not None and item.playlist.id is not None:
                    playlist_ids.add(int(item.playlist.id))
        elif isinstance(row, Track):
            if row.id is not None:
                related["track_ids"].add(int(row.id))
            related["album_ids"].update(_attribute_ids(row, "album_id"))
        elif isinstance(row, Album):
            if row.id is not None:
                related["album_ids"].add(int(row.id))
            related["artist_ids"].update(_attribute_ids(row, "artist_id"))
        elif isinstance(row, Artist) and row.id is not None:
            related["artist_ids"].add(int(row.id))
        elif isinstance(row, File):
            if row.id is not None:
                related["file_ids"].add(int(row.id))
            related["track_ids"].update(_attribute_ids(row, "track_id"))
        elif isinstance(row, DriveFileLocation):
            related["file_ids"].update(_attribute_ids(row, "file_id"))
        elif isinstance(row, StorageAccount) and row.id is not None:
            related["account_ids"].add(int(row.id))


def _affected_playlist_ids(session: Session, rows: set[object]) -> set[int]:
    playlist_ids = set(session.info.pop(_PLAYLIST_IDS, set()))
    related = session.info.pop(_RELATED_IDS, {})
    item_ids: set[int] = set(related.get("item_ids", set()))
    track_ids: set[int] = set(related.get("track_ids", set()))
    file_ids: set[int] = set(related.get("file_ids", set()))
    album_ids: set[int] = set(related.get("album_ids", set()))
    artist_ids: set[int] = set(related.get("artist_ids", set()))
    account_ids: set[int] = set(related.get("account_ids", set()))
    for row in rows:
        if isinstance(row, Playlist):
            if row.id is not None:
                playlist_ids.add(int(row.id))
        elif isinstance(row, PlaylistItem):
            playlist_ids.update(_attribute_ids(row, "playlist_id"))
        elif isinstance(row, Match):
            item_ids.update(_attribute_ids(row, "playlist_item_id"))
            track_ids.update(_attribute_ids(row, "track_id"))
            if row.playlist_item is not None and row.playlist_item.playlist_id is not None:
                playlist_ids.add(int(row.playlist_item.playlist_id))
        elif isinstance(row, Track):
            if row.id is not None:
                track_ids.add(int(row.id))
            album_ids.update(_attribute_ids(row, "album_id"))
        elif isinstance(row, Album):
            if row.id is not None:
                album_ids.add(int(row.id))
            artist_ids.update(_attribute_ids(row, "artist_id"))
        elif isinstance(row, Artist) and row.id is not None:
            artist_ids.add(int(row.id))
        elif isinstance(row, File):
            if row.id is not None:
                file_ids.add(int(row.id))
            track_ids.update(_attribute_ids(row, "track_id"))
        elif isinstance(row, DriveFileLocation):
            file_ids.update(_attribute_ids(row, "file_id"))
        elif isinstance(row, StorageAccount) and row.id is not None:
            account_ids.add(int(row.id))

    if item_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id).where(PlaylistItem.id.in_(item_ids))
            ).all()
        )
    if track_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id)
                .join(Match, Match.playlist_item_id == PlaylistItem.id)
                .where(Match.track_id.in_(track_ids))
            ).all()
        )
    if file_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id)
                .join(Match, Match.playlist_item_id == PlaylistItem.id)
                .join(File, File.track_id == Match.track_id)
                .where(File.id.in_(file_ids))
            ).all()
        )
    if album_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id)
                .join(Match, Match.playlist_item_id == PlaylistItem.id)
                .join(Track, Track.id == Match.track_id)
                .where(Track.album_id.in_(album_ids))
            ).all()
        )
    if artist_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id)
                .join(Match, Match.playlist_item_id == PlaylistItem.id)
                .join(Track, Track.id == Match.track_id)
                .join(Album, Album.id == Track.album_id)
                .where(Album.artist_id.in_(artist_ids))
            ).all()
        )
    if account_ids:
        playlist_ids.update(
            session.scalars(
                select(PlaylistItem.playlist_id)
                .join(Match, Match.playlist_item_id == PlaylistItem.id)
                .join(File, File.track_id == Match.track_id)
                .join(DriveFileLocation, DriveFileLocation.file_id == File.id)
                .where(DriveFileLocation.account_id.in_(account_ids))
            ).all()
        )
    return {int(value) for value in playlist_ids if value is not None}


@event.listens_for(Session, "before_commit")
def _reconcile(session: Session) -> None:
    if any(isinstance(row, WATCHED) for row in session.new | session.dirty | session.deleted):
        session.info[_FLAG] = True
    if not session.info.get(_FLAG, False):
        return
    session.flush()
    session.info.pop(_FLAG, None)
    rows = set(session.info.pop(_ROWS, set()))
    # Expand/backfill commands intentionally use the current ORM against an
    # older schema. Stay dormant until migration 0010 has added the contract.
    columns = {
        column["name"]
        for column in inspect(session.connection()).get_columns("playlists")
    }
    if "sync_fingerprint" not in columns or "opensubsonic_id" not in columns:
        session.info.pop(_PLAYLIST_IDS, None)
        session.info.pop(_RELATED_IDS, None)
        return
    now = utcnow()
    playlist_ids = _affected_playlist_ids(session, rows)
    if not playlist_ids:
        return
    playlists = session.scalars(
        select(Playlist).where(Playlist.id.in_(playlist_ids)).order_by(Playlist.id)
    ).all()
    for playlist in playlists:
        current = _fingerprint(session, playlist)
        previous = bytes(playlist.sync_fingerprint or b"")
        if not previous:
            session.execute(
                text(
                    "UPDATE playlists SET sync_fingerprint = :fingerprint, "
                    "sync_changed_at = COALESCE(sync_changed_at, :changed), "
                    "sync_revision = COALESCE(sync_revision, 1) WHERE id = :id"
                ),
                {"fingerprint": current, "changed": now, "id": playlist.id},
            )
        elif not hmac.compare_digest(previous, current):
            session.execute(
                text(
                    "UPDATE playlists SET sync_fingerprint = :fingerprint, "
                    "sync_changed_at = :changed, sync_revision = sync_revision + 1 "
                    "WHERE id = :id"
                ),
                {"fingerprint": current, "changed": now, "id": playlist.id},
            )
    session.expire_all()


@event.listens_for(Session, "after_rollback")
def _clear_rollback_state(session: Session) -> None:
    session.info.pop(_FLAG, None)
    session.info.pop(_ROWS, None)
    session.info.pop(_PLAYLIST_IDS, None)
    session.info.pop(_RELATED_IDS, None)
