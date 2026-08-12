"""Cross-user authorization, session, CSRF, and credential-isolation tests."""

from __future__ import annotations

import json
from datetime import timedelta

from app.models import (
    Album,
    Artist,
    File,
    Job,
    JobScope,
    JobStatus,
    Match,
    MatchStatus,
    Playlist,
    PlaylistItem,
    PlaylistSource,
    ServiceEnum,
    SessionKind,
    Track,
    User,
    UserRole,
    UserSession,
    UserState,
    utcnow,
)
from app.services.authentication import (
    create_session,
    revoke_session,
    session_for_token,
)
from app.services.user_credentials import (
    get_user_credential_payload,
    save_user_credential,
)


def _user(db, suffix: str, *, role: UserRole = UserRole.user) -> User:
    now = utcnow()
    user = User(
        email=f"{suffix}@example.test",
        email_key=f"{suffix}@example.test",
        role=role,
        state=UserState.active,
        is_bootstrap_owner=False,
        created_at=now,
        activated_at=now,
    )
    db.add(user)
    db.flush()
    return user


def _headers(db, user: User) -> dict[str, str]:
    raw, csrf, _ = create_session(db, user)
    db.commit()
    return {
        "Cookie": f"audiofeel_session={raw}",
        "Origin": "http://testserver",
        "X-CSRF-Token": csrf,
    }


def _playlist(db, user: User, suffix: str, service=ServiceEnum.manual):
    source = PlaylistSource(user_id=user.id, service=service)
    db.add(source)
    db.flush()
    playlist = Playlist(
        source_id=source.id,
        user_id=user.id,
        external_id=f"playlist-{suffix}",
        name=f"Playlist {suffix}",
        track_count=1,
        updated_at=utcnow(),
    )
    db.add(playlist)
    db.flush()
    item = PlaylistItem(
        playlist_id=playlist.id,
        position=0,
        artist_raw="Artist",
        title_raw=f"Track {suffix}",
        artist_norm="artist",
        title_norm=f"track {suffix}",
    )
    db.add(item)
    db.flush()
    return source, playlist, item


def test_session_token_is_hashed_and_revocation_absolute_and_idle_expiry(
    db, monkeypatch
):
    from app.config import settings

    user = _user(db, "session-user")
    raw, _, record = create_session(db, user)
    db.commit()

    assert isinstance(record.token_hash, bytes)
    assert raw.encode() not in record.token_hash
    assert session_for_token(db, raw, kind=SessionKind.google) is not None

    revoke_session(db, record)
    db.commit()
    assert session_for_token(db, raw, kind=SessionKind.google) is None

    expired_raw, _, expired = create_session(db, user)
    expired.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()
    assert session_for_token(db, expired_raw, kind=SessionKind.google) is None

    monkeypatch.setattr(settings, "auth_session_idle_seconds", 10)
    idle_raw, _, idle = create_session(db, user)
    idle.last_seen_at = utcnow() - timedelta(seconds=11)
    db.commit()
    assert session_for_token(db, idle_raw, kind=SessionKind.google) is None


def test_csrf_requires_same_origin_and_session_bound_header(api_client, db):
    user = _user(db, "csrf-user")
    headers = _headers(db, user)

    missing_origin = dict(headers)
    missing_origin.pop("Origin")
    assert api_client.post("/api/auth/logout", headers=missing_origin).status_code == 403

    wrong_origin = {**headers, "Origin": "https://attacker.invalid"}
    assert api_client.post("/api/auth/logout", headers=wrong_origin).status_code == 403

    conflicting_source = {
        **headers,
        "Origin": "https://attacker.invalid",
        "Referer": "http://testserver/#/playlists",
    }
    assert api_client.post("/api/auth/logout", headers=conflicting_source).status_code == 403

    missing_token = dict(headers)
    missing_token.pop("X-CSRF-Token")
    assert api_client.post("/api/auth/logout", headers=missing_token).status_code == 403

    wrong_token = {**headers, "X-CSRF-Token": "wrong-csrf-token"}
    assert api_client.post("/api/auth/logout", headers=wrong_token).status_code == 403

    cross_site = {**headers, "Sec-Fetch-Site": "cross-site"}
    assert api_client.post("/api/auth/logout", headers=cross_site).status_code == 403

    referer_only = dict(headers)
    referer_only.pop("Origin")
    referer_only["Referer"] = "http://testserver/#/playlists"
    assert api_client.post("/api/auth/logout", headers=referer_only).status_code == 204
    assert api_client.get("/api/playlists", headers=headers).status_code == 401


def test_owner_endpoints_are_forbidden_to_user_role(api_client, db):
    user = _user(db, "ordinary-user")
    headers = _headers(db, user)

    assert api_client.get("/api/admin/users", headers=headers).status_code == 403
    assert api_client.get("/api/providers/health", headers=headers).status_code == 403
    assert api_client.get("/api/storage", headers=headers).status_code == 403
    assert api_client.get("/api/library/stats", headers=headers).status_code == 403


def test_owner_can_invite_disable_and_revoke_all_sessions(
    api_client, db, owner_user, auth_headers
):
    invited = api_client.post(
        "/api/admin/users",
        json={"email": "second-user@example.test", "role": "user"},
        headers=auth_headers,
    )

    assert invited.status_code == 201
    body = invited.json()
    assert body["state"] == "pending"
    assert "google_sub" not in body
    assert "token" not in body

    target = db.get(User, body["id"])
    target.state = UserState.active
    target.activated_at = utcnow()
    target_headers = _headers(db, target)
    revoked = api_client.post(
        f"/api/admin/users/{target.id}/sessions/revoke", headers=auth_headers
    )

    assert revoked.status_code == 200
    assert revoked.json()["active_sessions"] == 0
    assert api_client.get("/api/playlists", headers=target_headers).status_code == 401

    target_headers = _headers(db, target)
    disabled = api_client.post(
        f"/api/admin/users/{target.id}/disable", headers=auth_headers
    )
    db.expire_all()

    assert disabled.status_code == 200
    assert disabled.json()["state"] == "disabled"
    assert db.get(User, target.id).state == UserState.disabled
    assert api_client.get("/api/playlists", headers=target_headers).status_code == 401
    assert (
        api_client.post(
            f"/api/admin/users/{owner_user.id}/disable", headers=auth_headers
        ).status_code
        == 409
    )


def test_cross_user_ids_are_404_and_shared_file_is_not_duplicated(
    api_client, db, tmp_path, monkeypatch
):
    from app.config import settings

    library = tmp_path / "library"
    library.mkdir()
    shared_path = library / "shared.flac"
    shared_bytes = b"shared-lossless-bytes"
    shared_path.write_bytes(shared_bytes)
    private_path = library / "private.flac"
    private_path.write_bytes(b"private-lossless-bytes")
    monkeypatch.setattr(settings, "music_library_path", str(library))

    user_a = _user(db, "user-a")
    user_b = _user(db, "user-b")
    source_a, playlist_a, item_a = _playlist(db, user_a, "a")
    source_b, playlist_b, item_b = _playlist(db, user_b, "b")

    artist = Artist(name="Shared Artist", name_norm="shared artist")
    db.add(artist)
    db.flush()
    shared_album = Album(
        artist_id=artist.id, title="Shared Album", title_norm="shared album", year=2026
    )
    private_album = Album(
        artist_id=artist.id, title="Private Album", title_norm="private album", year=2025
    )
    db.add_all([shared_album, private_album])
    db.flush()
    shared_track = Track(
        album_id=shared_album.id,
        title="Shared Track",
        title_norm="shared track",
        track_no=1,
        disc_no=1,
        duration_ms=120000,
    )
    private_track = Track(
        album_id=private_album.id,
        title="Private Track",
        title_norm="private track",
        track_no=1,
        disc_no=1,
        duration_ms=90000,
    )
    db.add_all([shared_track, private_track])
    db.flush()
    db.add_all(
        [
            File(
                track_id=shared_track.id,
                path=str(shared_path),
                format="flac",
                size_bytes=len(shared_bytes),
                sha1="1" * 40,
                scanned_at=utcnow(),
            ),
            File(
                track_id=private_track.id,
                path=str(private_path),
                format="flac",
                size_bytes=22,
                sha1="2" * 40,
                scanned_at=utcnow(),
            ),
        ]
    )
    db.add_all(
        [
            Match(
                playlist_item_id=item_a.id,
                track_id=shared_track.id,
                confidence=1.0,
                method="exact",
                status=MatchStatus.ready,
            ),
            Match(
                playlist_item_id=item_b.id,
                track_id=shared_track.id,
                confidence=1.0,
                method="exact",
                status=MatchStatus.ready,
            ),
        ]
    )
    _, playlist_b_private, item_b_private = _playlist(
        db, user_b, "b-private", service=ServiceEnum.spotify
    )
    db.add(
        Match(
            playlist_item_id=item_b_private.id,
            track_id=private_track.id,
            confidence=1.0,
            method="exact",
            status=MatchStatus.ready,
        )
    )
    job_a = Job(
        type="run_matching",
        playlist_id=playlist_a.id,
        user_id=user_a.id,
        scope=JobScope.user,
        status=JobStatus.done,
        payload=json.dumps({"playlist_id": playlist_a.id}),
        heartbeat_at=utcnow(),
    )
    job_b = Job(
        type="run_matching",
        playlist_id=playlist_b.id,
        user_id=user_b.id,
        scope=JobScope.user,
        status=JobStatus.done,
        payload=json.dumps(
            {
                "playlist_id": playlist_b.id,
                "path": "/must/not/leak",
                "authorization_url": "https://must-not-leak.invalid",
                "reason": "safe-code",
            }
        ),
        error="credential-like internal failure must not leak",
        heartbeat_at=utcnow(),
    )
    db.add_all([job_a, job_b])
    db.commit()
    headers_a = _headers(db, user_a)
    headers_b = _headers(db, user_b)

    playlists_a = api_client.get("/api/playlists", headers=headers_a).json()
    sources_a = api_client.get("/api/sources", headers=headers_a).json()
    assert {item["id"] for item in playlists_a["items"]} == {playlist_a.id}
    assert {item["id"] for item in sources_a["items"]} == {source_a.id}
    assert playlist_b.id not in {item["id"] for item in playlists_a["items"]}
    assert source_b.id not in {item["id"] for item in sources_a["items"]}

    # Every direct foreign identifier is intentionally indistinguishable from
    # a missing resource while both users may still stream the shared File.
    foreign_gets = [
        f"/api/playlists/{playlist_b.id}",
        f"/api/playlists/{playlist_b.id}/items",
        f"/api/jobs/{job_b.id}",
        f"/api/download/track/{item_b.id}",
        f"/api/download/playlist/{playlist_b.id}",
        f"/api/download/playlist/{playlist_b.id}/m3u8",
        f"/api/download/album/{private_album.id}",
        f"/api/qobuz/download-status/{playlist_b.id}",
        f"/api/yandex-download/download-status/{playlist_b.id}",
    ]
    for path in foreign_gets:
        assert api_client.get(path, headers=headers_a).status_code == 404

    assert (
        api_client.post(
            f"/api/playlists/{playlist_b.id}/refresh", headers=headers_a
        ).status_code
        == 404
    )
    assert (
        api_client.post(
            "/api/playlists/import",
            json={"source_id": source_b.id},
            headers=headers_a,
        ).status_code
        == 404
    )

    own_job = api_client.get(f"/api/jobs/{job_a.id}", headers=headers_a)
    assert own_job.status_code == 200
    sanitized_foreign_owner_job = api_client.get(
        f"/api/jobs/{job_b.id}", headers=headers_b
    )
    assert sanitized_foreign_owner_job.status_code == 200
    assert sanitized_foreign_owner_job.json()["error"] == "Job failed"
    assert "path" not in sanitized_foreign_owner_job.json()["payload"]
    assert "authorization_url" not in sanitized_foreign_owner_job.json()["payload"]
    assert "must-not-leak" not in sanitized_foreign_owner_job.text

    download_a = api_client.get(f"/api/download/track/{item_a.id}", headers=headers_a)
    download_b = api_client.get(f"/api/download/track/{item_b.id}", headers=headers_b)
    assert download_a.status_code == 200
    assert download_b.status_code == 200
    assert download_a.content == download_b.content == shared_bytes
    assert db.query(File).filter(File.track_id == shared_track.id).count() == 1


def test_manual_import_is_owned_by_authenticated_user(
    api_client, db, monkeypatch
):
    from app.api import matching as matching_api

    user = _user(db, "manual-owner")
    headers = _headers(db, user)
    monkeypatch.setattr(matching_api.run_matching_task, "delay", lambda *args: None)

    response = api_client.post(
        "/api/playlists/import-content",
        headers=headers,
        json={
            "name": "Private manual list",
            "content": "Artist — First track\nArtist — Second track",
            "format": "auto",
        },
    )
    db.expire_all()

    assert response.status_code == 201
    playlist = db.get(Playlist, response.json()["playlist_id"])
    assert playlist.user_id == user.id
    assert playlist.source.user_id == user.id
    assert playlist.source.service == ServiceEnum.manual
    assert response.json()["matching_job"]["id"]


def test_spotify_credentials_are_encrypted_and_isolated_per_user(db):
    user_a = _user(db, "spotify-a")
    user_b = _user(db, "spotify-b")
    db.flush()

    credential_a = save_user_credential(
        db,
        user_a.id,
        "spotify",
        {"access_token": "spotify-token-a", "refresh_token": "refresh-a"},
    )
    credential_b = save_user_credential(
        db,
        user_b.id,
        "spotify",
        {"access_token": "spotify-token-b", "refresh_token": "refresh-b"},
    )
    db.commit()

    assert credential_a.id != credential_b.id
    assert get_user_credential_payload(db, user_a.id, "spotify")["access_token"] == "spotify-token-a"
    assert get_user_credential_payload(db, user_b.id, "spotify")["access_token"] == "spotify-token-b"
    assert "spotify-token-a" not in credential_a.ciphertext
    assert "spotify-token-b" not in credential_b.ciphertext
    assert not hasattr(PlaylistSource, "access_token")
    assert not hasattr(PlaylistSource, "refresh_token")


def test_recovery_login_rotates_recovery_session_id(api_client, db, owner_user):
    headers = {"Origin": "http://testserver"}
    first = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "test-auth-token-12345"},
        headers=headers,
    )
    second = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "test-auth-token-12345"},
        headers=headers,
    )
    db.expire_all()
    records = db.query(UserSession).filter(UserSession.kind == SessionKind.recovery).all()

    assert first.status_code == second.status_code == 200
    assert len(records) == 2
    assert sum(record.revoked_at is not None for record in records) == 1
