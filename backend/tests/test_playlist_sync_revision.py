from __future__ import annotations

from app.models import Playlist
from app.services import playlist_sync_revision
from tests.test_opensubsonic_api import _seed


def test_playlist_revision_changes_once_for_visible_mutation(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, _key, playlist, _artist, _album, track, _item, _content = _seed(db, tmp_path)
    db.refresh(playlist)
    initial_revision = playlist.sync_revision
    initial_fingerprint = playlist.sync_fingerprint
    track.title = "Intermediate visible title"
    db.flush()
    track.title = "Updated visible title"
    db.flush()
    db.commit()
    db.refresh(playlist)
    assert playlist.sync_revision == initial_revision + 1
    assert playlist.sync_fingerprint != initial_fingerprint


def test_playlist_revision_does_not_change_when_transaction_restores_fingerprint(
    db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, _key, playlist, _artist, _album, track, _item, _content = _seed(db, tmp_path)
    db.refresh(playlist)
    initial_revision = playlist.sync_revision
    original_title = track.title
    track.title = "Temporary title"
    db.flush()
    track.title = original_title
    db.flush()
    db.commit()
    db.refresh(playlist)
    assert playlist.sync_revision == initial_revision


def test_unrelated_player_key_does_not_change_playlist_revision(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    user, _key, playlist, *_ = _seed(db, tmp_path)
    db.refresh(playlist)
    initial_revision = playlist.sync_revision
    from app.services.player_credentials import create_player_credential

    create_player_credential(db, user.id, "Second device")
    db.commit()
    db.refresh(playlist)
    assert playlist.sync_revision == initial_revision


def test_reconciliation_is_limited_to_affected_playlists(
    db, tmp_path, monkeypatch
):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    _user, _key, playlist, _artist, _album, track, *_ = _seed(db, tmp_path)
    _other, _other_key, other_playlist, *_ = _seed(
        db, tmp_path, email="other-revision@example.test"
    )
    calls: list[int] = []
    original = playlist_sync_revision._fingerprint

    def record(session, candidate):
        calls.append(candidate.id)
        return original(session, candidate)

    monkeypatch.setattr(playlist_sync_revision, "_fingerprint", record)
    track.title = "Only this playlist changed"
    db.commit()
    assert calls == [playlist.id]
    assert other_playlist.id not in calls


def test_rollback_clears_pending_reconciliation(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.delivery.settings.music_library_path", str(tmp_path))
    user, _key, _playlist, _artist, _album, track, *_ = _seed(db, tmp_path)
    calls: list[int] = []
    original = playlist_sync_revision._fingerprint

    def record(session, candidate):
        calls.append(candidate.id)
        return original(session, candidate)

    monkeypatch.setattr(playlist_sync_revision, "_fingerprint", record)
    track.title = "Rolled back"
    db.flush()
    db.rollback()
    from app.services.player_credentials import create_player_credential

    create_player_credential(db, user.id, "After rollback")
    db.commit()
    assert calls == []
