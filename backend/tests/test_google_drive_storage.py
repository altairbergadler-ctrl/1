from __future__ import annotations

import base64
import hashlib
import os
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.models import (
    Album,
    Artist,
    DriveFileLocation,
    File,
    Job,
    JobStatus,
    StorageAccount,
    StorageSecret,
    Track,
)
from app.services.google_drive import (
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveRateLimited,
    GoogleDriveUnavailable,
    OAuthClientConfig,
)
from app.services.normalize import normalize_album, normalize_artist, normalize_title
from app.services.storage import (
    ACTIVE_OAUTH_CONFIG,
    StorageError,
    StorageNotConfigured,
    account_secret_name,
    cleanup_expired_storage_cache,
    client_for_account,
    health_check_account,
    migrate_local_library,
    move_catalog_file_to_drive,
    replicate_imported_files,
    placement_accounts,
    upload_catalog_file,
)
from app.services.storage_secrets import (
    StorageSecretIntegrityError,
    read_storage_secret,
    save_storage_secret,
)


def _response(status: int, *, json=None, headers=None, method="GET", url="https://google.invalid"):
    return httpx.Response(
        status,
        json=json,
        headers=headers,
        request=httpx.Request(method, url),
    )


def _add_catalog_file(db, path: Path) -> File:
    content = path.read_bytes()
    artist = Artist(name="Drive Artist", name_norm=normalize_artist("Drive Artist"))
    album = Album(
        artist=artist,
        title="Drive Album",
        title_norm=normalize_album("Drive Album"),
        year=2026,
    )
    track = Track(
        album=album,
        title="Drive Track",
        title_norm=normalize_title("Drive Track"),
        track_no=1,
        disc_no=1,
    )
    file = File(
        track=track,
        path=str(path.resolve()),
        format="flac",
        size_bytes=len(content),
        sha1=hashlib.sha1(content).hexdigest(),
    )
    db.add_all([artist, album, track, file])
    db.commit()
    return file


def _add_account(
    db,
    email: str,
    *,
    limit: int = 100_000,
    usage: int = 0,
    priority: int = 0,
    refresh_token: str | None = None,
) -> StorageAccount:
    account = StorageAccount(
        provider="google_drive",
        email=email,
        label=email,
        root_folder_id=f"root-{email}",
        enabled=True,
        priority=priority,
        state="healthy",
        detail_code="drive_ready",
        quota_limit_bytes=limit,
        quota_usage_bytes=usage,
        quota_trash_bytes=0,
        credential_version=1,
    )
    db.add(account)
    db.flush()
    save_storage_secret(
        db,
        account_secret_name(account.id),
        {"refresh_token": refresh_token or f"refresh-{email}"},
        account_id=account.id,
    )
    db.commit()
    return account


def test_storage_secret_is_encrypted_and_detects_tampering(db):
    record = save_storage_secret(
        db,
        "google_drive_test_secret",
        {"refresh_token": "synthetic-refresh-token"},
    )
    db.commit()

    assert "synthetic-refresh-token" not in record.ciphertext
    assert read_storage_secret(db, record.name) == {
        "refresh_token": "synthetic-refresh-token"
    }

    raw = bytearray(
        base64.urlsafe_b64decode(
            record.ciphertext + "=" * (-len(record.ciphertext) % 4)
        )
    )
    raw[-1] ^= 1
    record.ciphertext = base64.urlsafe_b64encode(bytes(raw)).decode().rstrip("=")
    db.commit()
    with pytest.raises(StorageSecretIntegrityError):
        read_storage_secret(db, record.name)


def test_empty_storage_pool_reports_zero_capacity(api_client, auth_headers):
    response = api_client.get("/api/storage", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["total_limit_bytes"] == 0
    assert response.json()["total_free_bytes"] == 0


def test_oauth_callback_validates_before_promoting_pending_config(
    api_client, auth_headers, db, monkeypatch
):
    save_storage_secret(
        db,
        ACTIVE_OAUTH_CONFIG,
        {
            "client_id": "old-client.apps.googleusercontent.com",
            "client_secret": "old-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
    )
    db.commit()

    start = api_client.put(
        "/api/storage/google/oauth-config",
        headers=auth_headers,
        json={
            "client_id": "new-client.apps.googleusercontent.com",
            "client_secret": "new-client-secret",
        },
    )
    assert start.status_code == 200
    assert start.headers["cache-control"] == "private, no-store"
    assert "new-client-secret" not in start.text
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]

    monkeypatch.setattr(
        "app.services.storage.exchange_authorization_code",
        lambda *_args: {
            "access_token": "short-lived-access",
            "refresh_token": "new-refresh-token",
            "scope": "drive.file",
        },
    )

    def reject(_self):
        from app.services.google_drive import GoogleDriveAuthError

        raise GoogleDriveAuthError("rejected")

    monkeypatch.setattr("app.services.storage.GoogleDriveClient.about", reject)
    callback = api_client.get(
        "/api/storage/google/callback",
        params={"state": state, "code": "one-time-code"},
        headers=auth_headers,
        follow_redirects=False,
    )

    assert callback.status_code == 303
    assert "credential_rejected" in callback.headers["location"]
    assert read_storage_secret(db, ACTIVE_OAUTH_CONFIG)["client_id"].startswith("old-")
    assert "new-refresh-token" not in callback.text
    assert db.query(StorageAccount).count() == 0
    overview = api_client.get("/api/storage", headers=auth_headers).json()
    assert overview["oauth"]["configured"] is True
    assert overview["oauth"]["pending"] is False


def test_oauth_state_is_single_use_and_success_response_has_no_secrets(
    api_client, auth_headers, db, monkeypatch
):
    start = api_client.put(
        "/api/storage/google/oauth-config",
        headers=auth_headers,
        json={
            "client_id": "valid-client.apps.googleusercontent.com",
            "client_secret": "valid-client-secret",
        },
    )
    state = parse_qs(urlparse(start.json()["authorization_url"]).query)["state"][0]
    monkeypatch.setattr(
        "app.services.storage.exchange_authorization_code",
        lambda *_args: {
            "access_token": "short-lived-access",
            "refresh_token": "durable-refresh-token",
            "scope": "drive.file",
        },
    )
    monkeypatch.setattr(
        "app.services.storage.GoogleDriveClient.about",
        lambda _self: {
            "user": {"emailAddress": "owner@example.test", "displayName": "Owner"},
            "storageQuota": {"limit": "100000", "usage": "1000"},
        },
    )
    monkeypatch.setattr(
        "app.services.storage.GoogleDriveClient.ensure_root_folder",
        lambda _self: "drive-root-id",
    )

    success = api_client.get(
        "/api/storage/google/callback",
        params={"state": state, "code": "one-time-code"},
        headers=auth_headers,
        follow_redirects=False,
    )
    replay = api_client.get(
        "/api/storage/google/callback",
        params={"state": state, "code": "one-time-code"},
        headers=auth_headers,
        follow_redirects=False,
    )
    overview = api_client.get("/api/storage", headers=auth_headers)

    assert success.status_code == 303
    assert "storage_connected" in success.headers["location"]
    assert replay.status_code == 303
    assert "storage_error" in replay.headers["location"]
    assert overview.status_code == 200
    assert overview.headers["cache-control"] == "private, no-store"
    assert "durable-refresh-token" not in overview.text
    assert "valid-client-secret" not in overview.text
    assert overview.json()["accounts"][0]["email"] == "owner@example.test"
    record = db.query(StorageSecret).filter(StorageSecret.account_id.isnot(None)).one()
    assert "durable-refresh-token" not in record.ciphertext


def test_placement_uses_capacity_and_upload_fails_over(db, tmp_path, monkeypatch):
    monkeypatch.setattr("app.services.storage.settings.google_drive_min_free_bytes", 0)
    save_storage_secret(
        db,
        ACTIVE_OAUTH_CONFIG,
        {
            "client_id": "valid-client.apps.googleusercontent.com",
            "client_secret": "valid-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
    )
    first = _add_account(
        db,
        "large@example.test",
        limit=1_000_000,
        refresh_token="first-refresh",
    )
    second = _add_account(
        db,
        "small@example.test",
        limit=500_000,
        refresh_token="second-refresh",
    )
    source = tmp_path / "track.flac"
    source.write_bytes(b"verified-drive-audio")
    file = _add_catalog_file(db, source)

    assert [item.id for item in placement_accounts(db, source.stat().st_size)] == [
        first.id,
        second.id,
    ]

    def upload(client, path, **_kwargs):
        if client.refresh_token == "first-refresh":
            raise GoogleDriveUnavailable("temporary")
        return {
            "id": "remote-file-id",
            "name": Path(path).name,
            "size": str(Path(path).stat().st_size),
            "sha1Checksum": file.sha1,
        }

    monkeypatch.setattr("app.services.storage.GoogleDriveClient.upload_file", upload)
    location = upload_catalog_file(db, file)
    db.commit()

    assert location.account_id == second.id
    assert first.state == "provider_down"
    assert second.state == "healthy"
    assert db.query(DriveFileLocation).count() == 1


def test_verified_upload_is_committed_before_local_source_is_evicted(
    db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    source = library / "track.flac"
    source.write_bytes(b"temporary-local-audio")
    monkeypatch.setattr("app.services.storage.settings.music_library_path", str(library))
    monkeypatch.setattr("app.services.storage.settings.google_drive_min_free_bytes", 0)
    save_storage_secret(
        db,
        ACTIVE_OAUTH_CONFIG,
        {
            "client_id": "valid-client.apps.googleusercontent.com",
            "client_secret": "valid-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
    )
    _add_account(db, "move@example.test")
    file = _add_catalog_file(db, source)

    monkeypatch.setattr(
        "app.services.storage.GoogleDriveClient.upload_file",
        lambda _client, path, **_kwargs: {
            "id": "remote-moved",
            "name": Path(path).name,
            "size": str(Path(path).stat().st_size),
            "sha1Checksum": file.sha1,
        },
    )

    uploaded, eviction = move_catalog_file_to_drive(db, file)
    db.refresh(file)

    assert uploaded is True
    assert eviction == "evicted"
    assert source.exists() is False
    assert file.path is None
    assert db.query(DriveFileLocation).count() == 1


def test_database_failure_before_remote_persistence_preserves_local_source(
    db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    source = library / "track.flac"
    source.write_bytes(b"must-not-be-lost")
    monkeypatch.setattr("app.services.storage.settings.music_library_path", str(library))
    monkeypatch.setattr("app.services.storage.settings.google_drive_min_free_bytes", 0)
    save_storage_secret(
        db,
        ACTIVE_OAUTH_CONFIG,
        {
            "client_id": "valid-client.apps.googleusercontent.com",
            "client_secret": "valid-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
    )
    _add_account(db, "commit-failure@example.test")
    file = _add_catalog_file(db, source)
    monkeypatch.setattr(
        "app.services.storage.GoogleDriveClient.upload_file",
        lambda _client, path, **_kwargs: {
            "id": "remote-before-failed-commit",
            "name": Path(path).name,
            "size": str(Path(path).stat().st_size),
            "sha1Checksum": file.sha1,
        },
    )
    monkeypatch.setattr(db, "commit", lambda: (_ for _ in ()).throw(RuntimeError("db")))

    with pytest.raises(StorageError):
        move_catalog_file_to_drive(db, file)

    assert source.is_file()
    assert db.query(DriveFileLocation).count() == 0


def test_migration_evicts_old_local_copy_that_is_already_remote(
    db, tmp_path, monkeypatch
):
    library = tmp_path / "library"
    library.mkdir()
    source = library / "legacy.flac"
    source.write_bytes(b"legacy-local-copy")
    monkeypatch.setattr("app.services.storage.settings.music_library_path", str(library))
    account = _add_account(db, "legacy@example.test")
    file = _add_catalog_file(db, source)
    db.add(
        DriveFileLocation(
            file_id=file.id,
            account_id=account.id,
            remote_file_id="remote-legacy",
            remote_name=source.name,
            size_bytes=file.size_bytes,
            sha1=file.sha1,
            state="healthy",
        )
    )
    db.commit()

    summary = migrate_local_library(db)
    db.refresh(file)

    assert summary["already_remote"] == 1
    assert summary["evicted"] == 1
    assert source.exists() is False
    assert file.path is None


def test_cache_sweeper_removes_only_expired_audiofeel_temporary_paths(
    tmp_path, monkeypatch
):
    stale = tmp_path / "audiofeel-stale"
    fresh = tmp_path / "audiofeel-fresh"
    unrelated = tmp_path / "keep-me"
    stale.mkdir()
    fresh.mkdir()
    unrelated.mkdir()
    (stale / "track.flac").write_bytes(b"stale")
    old = time.time() - 7200
    os.utime(stale, (old, old))
    monkeypatch.setattr("app.services.storage.settings.storage_cache_path", str(tmp_path))
    monkeypatch.setattr("app.services.storage.settings.storage_cache_ttl_seconds", 3600)

    summary = cleanup_expired_storage_cache()

    assert summary == {"removed": 1, "failed": 0}
    assert stale.exists() is False
    assert fresh.is_dir()
    assert unrelated.is_dir()


def test_each_account_keeps_the_oauth_client_that_issued_its_refresh_token(db):
    save_storage_secret(
        db,
        ACTIVE_OAUTH_CONFIG,
        {
            "client_id": "new-client.apps.googleusercontent.com",
            "client_secret": "new-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
    )
    account = _add_account(db, "old-client@example.test")
    save_storage_secret(
        db,
        account_secret_name(account.id),
        {
            "refresh_token": "old-refresh-token",
            "client_id": "old-client.apps.googleusercontent.com",
            "client_secret": "old-client-secret",
            "redirect_uri": "https://audiofeel.su/api/storage/google/callback",
        },
        account_id=account.id,
    )
    db.commit()

    client = client_for_account(db, account)

    assert client.config.client_id.startswith("old-client")
    assert client.refresh_token == "old-refresh-token"


def test_manual_storage_health_check_queues_only_safe_identifiers(
    api_client, auth_headers, db, monkeypatch
):
    account = _add_account(
        db,
        "health@example.test",
        refresh_token="hidden-health-refresh-token",
    )
    queued = []
    monkeypatch.setattr(
        "app.api.storage.storage_health_check_task.delay",
        lambda *args: queued.append(args),
    )

    response = api_client.post(
        f"/api/storage/accounts/{account.id}/health-check",
        headers=auth_headers,
    )

    assert response.status_code == 202
    job = db.get(Job, response.json()["id"])
    assert job.status == JobStatus.pending
    assert queued == [(job.id, account.id)]
    assert "hidden-health-refresh-token" not in response.text


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (GoogleDriveAuthError("rejected"), "expired"),
        (GoogleDriveRateLimited("slow down"), "rate_limited"),
        (GoogleDriveUnavailable("offline"), "provider_down"),
        (StorageNotConfigured("missing"), "not_configured"),
    ],
)
def test_storage_health_states_are_classified(db, monkeypatch, error, expected):
    account = _add_account(db, f"{expected}@example.test")

    def fail(_db, _account):
        raise error

    monkeypatch.setattr("app.services.storage.client_for_account", fail)
    checked = health_check_account(db, account)

    assert checked.state == expected


def test_resumable_upload_recovers_session_after_interruption(
    tmp_path, monkeypatch
):
    source = tmp_path / "resume.flac"
    source.write_bytes(b"resumable-content")
    sha1 = hashlib.sha1(source.read_bytes()).hexdigest()
    calls = []

    def request(method, url, **_kwargs):
        if method == "GET":
            return _response(200, json={"files": []}, method=method, url=url)
        return _response(
            200,
            json={},
            headers={"Location": "https://upload.invalid/session"},
            method=method,
            url=url,
        )

    def put(url, **kwargs):
        content_range = kwargs["headers"]["Content-Range"]
        calls.append(content_range)
        if len(calls) == 1:
            raise httpx.ConnectError("interrupted")
        if content_range.startswith("bytes */"):
            return _response(308, headers={}, method="PUT", url=url)
        return _response(
            200,
            json={
                "id": "remote-resumed",
                "name": source.name,
                "size": str(source.stat().st_size),
                "sha1Checksum": sha1,
            },
            method="PUT",
            url=url,
        )

    monkeypatch.setattr("app.services.google_drive.httpx.request", request)
    monkeypatch.setattr("app.services.google_drive.httpx.put", put)
    monkeypatch.setattr("app.services.google_drive.time.sleep", lambda _value: None)
    client = GoogleDriveClient(
        OAuthClientConfig(
            "valid-client.apps.googleusercontent.com",
            "valid-client-secret",
            "https://audiofeel.su/api/storage/google/callback",
        ),
        "refresh-token",
        access_token="access-token",
    )

    result = client.upload_file(
        source,
        root_folder_id="root-id",
        sha1=sha1,
        catalog_file_id=1,
    )

    assert result["id"] == "remote-resumed"
    assert any(value.startswith("bytes */") for value in calls)
    assert calls[-1].startswith("bytes 0-")


def test_rejected_refresh_token_is_classified_as_expired(monkeypatch):
    monkeypatch.setattr(
        "app.services.google_drive.httpx.post",
        lambda *_args, **_kwargs: _response(400, json={"error": "invalid_grant"}),
    )
    client = GoogleDriveClient(
        OAuthClientConfig(
            "valid-client.apps.googleusercontent.com",
            "valid-client-secret",
            "https://audiofeel.su/api/storage/google/callback",
        ),
        "expired-refresh-token",
    )

    with pytest.raises(GoogleDriveAuthError):
        client.about()


def test_replication_recovery_uses_file_id_after_local_eviction(
    db, tmp_path, monkeypatch
):
    source = tmp_path / "recovered.flac"
    source.write_bytes(b"already-on-drive")
    file = _add_catalog_file(db, source)
    file.path = None
    db.commit()
    monkeypatch.setattr(
        "app.services.storage.settings.storage_primary_backend",
        "google_drive",
    )
    monkeypatch.setattr(
        "app.services.storage.move_catalog_file_to_drive",
        lambda _db, _file: (False, "already_evicted"),
    )
    summary = replicate_imported_files(db, {"file_ids": [file.id]})
    assert summary["status"] == "completed"
    assert summary["already_remote"] == 1
    assert summary["already_evicted"] == 1
    assert summary["missing_catalog"] == 0
