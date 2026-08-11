"""Google Drive account lifecycle, placement and verified replication."""

from __future__ import annotations

import hashlib
import shutil
import secrets
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    DriveFileLocation,
    File as LibraryFile,
    StorageAccount,
    StorageOAuthState,
    StorageSecret,
    utcnow,
)
from app.services.google_drive import (
    GoogleDriveAuthError,
    GoogleDriveClient,
    GoogleDriveError,
    GoogleDriveRateLimited,
    GoogleDriveUnavailable,
    OAuthClientConfig,
    authorization_url,
    exchange_authorization_code,
)
from app.services.storage_secrets import (
    StorageSecretError,
    delete_storage_secret,
    get_storage_secret,
    has_storage_secret,
    read_storage_secret,
    save_storage_secret,
)

ACTIVE_OAUTH_CONFIG = "google_drive_oauth_active"
PENDING_OAUTH_CONFIG = "google_drive_oauth_pending"
OAUTH_STATE_PREFIX = "google_drive_oauth_state:"
ACCOUNT_SECRET_PREFIX = "google_drive_account:"


class StorageError(RuntimeError):
    pass


class StorageNotConfigured(StorageError):
    pass


class StorageCapacityError(StorageError):
    pass


class LocalEvictionError(StorageError):
    """A durable Drive object exists, but its temporary local source remains."""


def _verified_location(
    db: Session, file: LibraryFile
) -> DriveFileLocation | None:
    locations = list(
        db.scalars(
            select(DriveFileLocation)
            .where(
                DriveFileLocation.file_id == file.id,
                DriveFileLocation.state == "healthy",
            )
            .order_by(DriveFileLocation.id)
        )
    )
    expected_sha1 = str(file.sha1 or "").casefold()
    expected_size = int(file.size_bytes) if file.size_bytes is not None else None
    for location in locations:
        if location.verified_at is None:
            continue
        if str(location.sha1 or "").casefold() != expected_sha1:
            continue
        if expected_size is not None and int(location.size_bytes) != expected_size:
            continue
        return location
    return None


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _temporary_library_source(path_value: str) -> tuple[Path, Path]:
    try:
        root = Path(settings.music_library_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise LocalEvictionError("Music library is unavailable") from exc
    candidate = Path(path_value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        source = candidate.resolve(strict=True)
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise LocalEvictionError("Temporary local source is unavailable") from exc
    if not source.is_file() or not source.is_relative_to(root):
        raise LocalEvictionError("Refusing to evict a path outside the music library")
    return root, source


def _remove_empty_library_parents(source: Path, root: Path) -> None:
    parent = source.parent
    while parent != root and parent.is_relative_to(root):
        try:
            parent.rmdir()
        except OSError:
            break
        parent = parent.parent


def evict_verified_local_source(
    db: Session,
    file: LibraryFile,
    location: DriveFileLocation,
) -> str:
    """Evict a temporary local source only after its Drive row is durable."""

    if not file.path:
        return "already_evicted"
    if (
        location.verified_at is None
        or location.state != "healthy"
        or str(location.sha1).casefold() != str(file.sha1).casefold()
        or (
            file.size_bytes is not None
            and int(location.size_bytes) != int(file.size_bytes)
        )
    ):
        raise LocalEvictionError("Drive location is not verified for this catalog file")

    try:
        root, source = _temporary_library_source(str(file.path))
    except FileNotFoundError:
        file.path = None
        file.scanned_at = utcnow()
        try:
            db.commit()
        except Exception as exc:
            db.rollback()
            raise LocalEvictionError("Could not clear a missing local path") from exc
        return "already_evicted"

    try:
        if source.stat().st_size != int(location.size_bytes):
            raise LocalEvictionError("Temporary local source size changed after upload")
        if _sha1_file(source).casefold() != str(location.sha1).casefold():
            raise LocalEvictionError("Temporary local source changed after upload")
        source.unlink()
    except LocalEvictionError:
        raise
    except OSError as exc:
        raise LocalEvictionError("Could not evict the temporary local source") from exc

    file.path = None
    file.scanned_at = utcnow()
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise LocalEvictionError("Local source was evicted but catalog cleanup failed") from exc
    _remove_empty_library_parents(source, root)
    return "evicted"


def move_catalog_file_to_drive(
    db: Session,
    file: LibraryFile,
) -> tuple[bool, str]:
    """Commit a verified Drive object, then evict its temporary local source."""

    locked = db.scalar(
        select(LibraryFile).where(LibraryFile.id == file.id).with_for_update()
    )
    if locked is None:
        raise StorageError("Catalog file no longer exists")
    location = _verified_location(db, locked)
    uploaded = False
    if location is None:
        try:
            location = upload_catalog_file(db, locked)
            # This commit is deliberately before unlink(). A database failure
            # must leave the only local copy intact.
            db.commit()
            uploaded = True
        except StorageError:
            db.rollback()
            raise
        except Exception as exc:
            db.rollback()
            raise StorageError("Could not persist the verified Drive location") from exc
    try:
        eviction = evict_verified_local_source(db, locked, location)
    except LocalEvictionError:
        db.rollback()
        return uploaded, "cleanup_failed"
    return uploaded, eviction


def cleanup_expired_storage_cache() -> dict[str, int]:
    """Remove only stale Audiofeel temporary directories from the cache root."""

    summary = {"removed": 0, "failed": 0}
    root = Path(settings.storage_cache_path).expanduser()
    if not root.exists():
        return summary
    try:
        root = root.resolve(strict=True)
        entries = list(root.iterdir())
    except OSError:
        summary["failed"] += 1
        return summary
    cutoff = time.time() - int(settings.storage_cache_ttl_seconds)
    for entry in entries:
        if not entry.name.startswith("audiofeel-"):
            continue
        try:
            if entry.lstat().st_mtime > cutoff:
                continue
            if entry.is_symlink() or entry.is_file():
                entry.unlink()
            elif entry.is_dir():
                shutil.rmtree(entry)
            else:
                continue
            summary["removed"] += 1
        except OSError:
            summary["failed"] += 1
    return summary


def account_secret_name(account_id: int) -> str:
    return f"{ACCOUNT_SECRET_PREFIX}{int(account_id)}"


def _oauth_config(payload: dict[str, Any]) -> OAuthClientConfig:
    client_id = str(payload.get("client_id") or "").strip()
    client_secret = str(payload.get("client_secret") or "").strip()
    redirect_uri = str(payload.get("redirect_uri") or "").strip()
    if (
        not client_id.endswith(".apps.googleusercontent.com")
        or len(client_id) > 512
        or len(client_secret) < 8
        or len(client_secret) > 4096
        or redirect_uri != settings.google_drive_redirect_uri
    ):
        raise StorageNotConfigured("Google OAuth application is invalid")
    return OAuthClientConfig(client_id, client_secret, redirect_uri)


def save_pending_oauth_config(
    db: Session, client_id: str, client_secret: str
) -> StorageSecret:
    config = _oauth_config(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": settings.google_drive_redirect_uri,
        }
    )
    return save_storage_secret(
        db,
        PENDING_OAUTH_CONFIG,
        {
            "client_id": config.client_id,
            "client_secret": config.client_secret,
            "redirect_uri": config.redirect_uri,
        },
    )


def oauth_config_status(db: Session) -> dict[str, Any]:
    active = get_storage_secret(db, ACTIVE_OAUTH_CONFIG)
    pending = get_storage_secret(db, PENDING_OAUTH_CONFIG)
    return {
        "configured": active is not None,
        "version": int(active.version) if active is not None else None,
        "updated_at": active.updated_at if active is not None else None,
        "pending": pending is not None,
    }


def _config_name_for_connect(db: Session) -> str:
    if has_storage_secret(db, PENDING_OAUTH_CONFIG):
        return PENDING_OAUTH_CONFIG
    if has_storage_secret(db, ACTIVE_OAUTH_CONFIG):
        return ACTIVE_OAUTH_CONFIG
    raise StorageNotConfigured("Google OAuth application is not configured")


def _hash_state(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _cleanup_oauth_states(db: Session) -> None:
    now = utcnow()
    rows = db.scalars(
        select(StorageOAuthState).where(StorageOAuthState.expires_at <= now)
    ).all()
    for row in rows:
        secret = db.get(StorageSecret, row.secret_id)
        db.delete(row)
        if secret is not None:
            db.delete(secret)
    db.flush()


def begin_google_oauth(db: Session) -> str:
    _cleanup_oauth_states(db)
    config_name = _config_name_for_connect(db)
    config = _oauth_config(read_storage_secret(db, config_name))
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    state_hash = _hash_state(state)
    secret_record = save_storage_secret(
        db,
        f"{OAUTH_STATE_PREFIX}{state_hash}",
        {"verifier": verifier, "config_name": config_name},
    )
    db.add(
        StorageOAuthState(
            state_hash=state_hash,
            secret_id=secret_record.id,
            expires_at=utcnow()
            + timedelta(seconds=settings.google_drive_oauth_state_ttl_seconds),
        )
    )
    db.flush()
    return authorization_url(config, state, verifier)


def _quota_value(quota: dict[str, Any], name: str) -> int | None:
    value = quota.get(name)
    if value in {None, ""}:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _apply_about(account: StorageAccount, about: dict[str, Any]) -> None:
    user = about.get("user") or {}
    quota = about.get("storageQuota") or {}
    email = str(user.get("emailAddress") or "").strip().casefold()
    if email:
        account.email = email
    account.label = str(user.get("displayName") or "").strip() or account.email
    account.quota_limit_bytes = _quota_value(quota, "limit")
    account.quota_usage_bytes = _quota_value(quota, "usage")
    account.quota_trash_bytes = _quota_value(quota, "usageInDriveTrash")
    account.state = "healthy"
    account.detail_code = "drive_ready"
    account.last_checked_at = utcnow()
    account.updated_at = utcnow()


def complete_google_oauth(db: Session, state: str, code: str) -> StorageAccount:
    state_hash = _hash_state(state)
    row = db.scalar(
        select(StorageOAuthState).where(StorageOAuthState.state_hash == state_hash)
    )
    now = utcnow()
    if (
        row is None
        or row.consumed_at is not None
        or row.expires_at <= now
    ):
        raise StorageError("Google OAuth state is invalid or expired")
    state_secret = db.get(StorageSecret, row.secret_id)
    if state_secret is None:
        raise StorageError("Google OAuth state is unavailable")
    state_payload = read_storage_secret(db, state_secret.name)
    config_name = str(state_payload.get("config_name") or "")
    verifier = str(state_payload.get("verifier") or "")
    config_payload = read_storage_secret(db, config_name)
    config = _oauth_config(config_payload)

    # Consume before the external exchange so a timeout or callback replay can
    # never reuse the same authorization code/state pair.
    row.consumed_at = now
    db.commit()
    try:
        token_payload = exchange_authorization_code(config, code, verifier)
        refresh_token = str(token_payload.get("refresh_token") or "")
        access_token = str(token_payload.get("access_token") or "")
        client = GoogleDriveClient(
            config,
            refresh_token,
            access_token=access_token,
        )
        about = client.about()
        email = str((about.get("user") or {}).get("emailAddress") or "").strip().casefold()
        if not email:
            raise GoogleDriveAuthError("Google returned no account email")
        account = db.scalar(
            select(StorageAccount).where(StorageAccount.email == email)
        )
        if account is None:
            if not refresh_token:
                raise GoogleDriveAuthError("Google returned no refresh token")
            account = StorageAccount(
                provider="google_drive",
                email=email,
                label=email,
                root_folder_id="pending",
                enabled=True,
                priority=0,
                state="healthy",
                detail_code="connecting",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            db.add(account)
            db.flush()
        elif not refresh_token:
            if config_name == PENDING_OAUTH_CONFIG:
                raise GoogleDriveAuthError(
                    "Google returned no refresh token for the new OAuth application"
                )
            existing = read_storage_secret(db, account_secret_name(account.id))
            refresh_token = str(existing.get("refresh_token") or "")
            client = GoogleDriveClient(config, refresh_token, access_token=access_token)

        root_folder_id = client.ensure_root_folder()
        account.root_folder_id = root_folder_id
        _apply_about(account, about)
        secret_record = save_storage_secret(
            db,
            account_secret_name(account.id),
            {
                "refresh_token": refresh_token,
                "scope": str(token_payload.get("scope") or ""),
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": config.redirect_uri,
            },
            account_id=account.id,
            validated_at=utcnow(),
        )
        account.credential_version = int(secret_record.version)

        if config_name == PENDING_OAUTH_CONFIG:
            active = save_storage_secret(
                db,
                ACTIVE_OAUTH_CONFIG,
                config_payload,
                validated_at=utcnow(),
            )
            del active
            delete_storage_secret(db, PENDING_OAUTH_CONFIG)
        delete_storage_secret(db, state_secret.name)
        db.commit()
        db.refresh(account)
        return account
    except Exception:
        db.rollback()
        if config_name == PENDING_OAUTH_CONFIG:
            delete_storage_secret(db, PENDING_OAUTH_CONFIG)
        stale = db.scalar(
            select(StorageOAuthState).where(StorageOAuthState.state_hash == state_hash)
        )
        if stale is not None:
            stale.consumed_at = stale.consumed_at or utcnow()
            secret = db.get(StorageSecret, stale.secret_id)
            if secret is not None:
                db.delete(stale)
                db.delete(secret)
        db.commit()
        raise


def discard_google_oauth_state(db: Session, state: str) -> None:
    row = db.scalar(
        select(StorageOAuthState).where(
            StorageOAuthState.state_hash == _hash_state(state)
        )
    )
    if row is None or row.consumed_at is not None:
        return
    row.consumed_at = utcnow()
    secret = db.get(StorageSecret, row.secret_id)
    pending_config = False
    if secret is not None:
        try:
            payload = read_storage_secret(db, secret.name)
            pending_config = payload.get("config_name") == PENDING_OAUTH_CONFIG
        except StorageSecretError:
            pending_config = False
    db.delete(row)
    if secret is not None:
        db.delete(secret)
    if pending_config:
        delete_storage_secret(db, PENDING_OAUTH_CONFIG)
    db.flush()


def _active_config(db: Session) -> OAuthClientConfig:
    try:
        return _oauth_config(read_storage_secret(db, ACTIVE_OAUTH_CONFIG))
    except StorageSecretError as exc:
        raise StorageNotConfigured("Google OAuth application is not configured") from exc


def client_for_account(db: Session, account: StorageAccount) -> GoogleDriveClient:
    try:
        payload = read_storage_secret(db, account_secret_name(account.id))
    except StorageSecretError as exc:
        raise StorageNotConfigured("Google account credential is unavailable") from exc
    refresh_token = str(payload.get("refresh_token") or "")
    if not refresh_token:
        raise StorageNotConfigured("Google account credential is unavailable")
    try:
        config = _oauth_config(payload)
    except StorageNotConfigured:
        # Compatibility path for credentials stored before per-account OAuth
        # snapshots. New writes always preserve the matching client config.
        config = _active_config(db)
    return GoogleDriveClient(config, refresh_token)


def account_free_bytes(account: StorageAccount) -> int | None:
    if account.quota_limit_bytes is None:
        return None
    return max(0, int(account.quota_limit_bytes) - int(account.quota_usage_bytes or 0))


def placement_accounts(db: Session, required_bytes: int) -> list[StorageAccount]:
    accounts = list(
        db.scalars(
            select(StorageAccount).where(
                StorageAccount.provider == "google_drive",
                StorageAccount.enabled.is_(True),
                StorageAccount.state == "healthy",
            )
        )
    )
    minimum = int(required_bytes) + settings.google_drive_min_free_bytes
    eligible = [
        account
        for account in accounts
        if account_free_bytes(account) is None
        or int(account_free_bytes(account) or 0) >= minimum
    ]
    eligible.sort(
        key=lambda account: (
            account_free_bytes(account) is None,
            int(account_free_bytes(account) or 0),
            int(account.priority),
            -int(account.id),
        ),
        reverse=True,
    )
    return eligible


def health_check_account(db: Session, account: StorageAccount) -> StorageAccount:
    try:
        client = client_for_account(db, account)
        about = client.about()
        root = client.get_file_metadata(account.root_folder_id)
        if root.get("trashed") or root.get("mimeType") != "application/vnd.google-apps.folder":
            raise GoogleDriveError("Google root folder is unavailable")
        _apply_about(account, about)
    except GoogleDriveAuthError:
        account.state = "expired"
        account.detail_code = "credential_rejected"
        account.last_checked_at = utcnow()
    except GoogleDriveRateLimited as exc:
        account.state = "rate_limited"
        account.detail_code = "drive_rate_limited"
        account.last_checked_at = utcnow()
        del exc
    except StorageNotConfigured:
        account.state = "not_configured"
        account.detail_code = "credential_unavailable"
        account.last_checked_at = utcnow()
    except (GoogleDriveUnavailable, GoogleDriveError):
        account.state = "provider_down"
        account.detail_code = "drive_unavailable"
        account.last_checked_at = utcnow()
    account.updated_at = utcnow()
    db.flush()
    return account


def upload_catalog_file(
    db: Session,
    file: LibraryFile,
    *,
    source_path: str | Path | None = None,
) -> DriveFileLocation:
    existing = _verified_location(db, file)
    if existing is not None:
        return existing
    mismatched = db.scalar(
        select(DriveFileLocation).where(
            DriveFileLocation.file_id == file.id,
            DriveFileLocation.state == "healthy",
        )
    )
    if mismatched is not None:
        raise StorageError("Existing Drive location metadata does not match the catalog")
    path_value = source_path or file.path
    if not path_value:
        raise StorageError("Catalog file has no local source")
    source = Path(path_value).expanduser().resolve(strict=True)
    failures: list[Exception] = []
    accounts = placement_accounts(db, source.stat().st_size)
    if not accounts:
        raise StorageCapacityError("No healthy Google Drive account has enough space")
    for account in accounts:
        try:
            payload = client_for_account(db, account).upload_file(
                source,
                root_folder_id=account.root_folder_id,
                sha1=file.sha1,
                catalog_file_id=file.id,
            )
            location = DriveFileLocation(
                file_id=file.id,
                account_id=account.id,
                remote_file_id=str(payload["id"]),
                remote_name=str(payload.get("name") or source.name),
                size_bytes=int(payload["size"]),
                sha1=str(payload["sha1Checksum"]).casefold(),
                state="healthy",
                created_at=utcnow(),
                verified_at=utcnow(),
            )
            db.add(location)
            if account.quota_usage_bytes is not None:
                account.quota_usage_bytes = int(account.quota_usage_bytes) + int(
                    location.size_bytes
                )
            account.updated_at = utcnow()
            db.flush()
            return location
        except GoogleDriveAuthError as exc:
            account.state = "expired"
            account.detail_code = "credential_rejected"
            failures.append(exc)
        except GoogleDriveRateLimited as exc:
            account.state = "rate_limited"
            account.detail_code = "drive_rate_limited"
            failures.append(exc)
        except (GoogleDriveUnavailable, GoogleDriveError) as exc:
            account.state = "provider_down"
            account.detail_code = "drive_unavailable"
            failures.append(exc)
        account.last_checked_at = utcnow()
        account.updated_at = utcnow()
        db.flush()
    if failures:
        raise StorageError("All Google Drive accounts rejected the upload") from failures[-1]
    raise StorageCapacityError("No Google Drive account accepted the upload")


def migrate_local_library(
    db: Session,
    progress_callback: Callable[[dict[str, int | str]], None] | None = None,
) -> dict[str, int | str]:
    files = list(db.scalars(select(LibraryFile).order_by(LibraryFile.id)))
    summary = {
        "status": "completed",
        "total": len(files),
        "processed": 0,
        "uploaded": 0,
        "already_remote": 0,
        "evicted": 0,
        "already_evicted": 0,
        "cleanup_failed": 0,
        "skipped_local_missing": 0,
        "failed": 0,
    }
    for file in files:
        summary["processed"] += 1
        has_verified_remote = _verified_location(db, file) is not None
        if not has_verified_remote and (not file.path or not Path(file.path).is_file()):
            summary["skipped_local_missing"] += 1
        else:
            try:
                uploaded, eviction = move_catalog_file_to_drive(db, file)
                if uploaded:
                    summary["uploaded"] += 1
                else:
                    summary["already_remote"] += 1
                if eviction == "evicted":
                    summary["evicted"] += 1
                elif eviction == "already_evicted":
                    summary["already_evicted"] += 1
                else:
                    summary["cleanup_failed"] += 1
            except StorageError:
                db.rollback()
                summary["failed"] += 1
        if progress_callback is not None:
            progress_callback(dict(summary))
    if summary["failed"] or summary["cleanup_failed"] or summary["skipped_local_missing"]:
        summary["status"] = "degraded"
    return summary


def replicate_imported_files(
    db: Session,
    import_report: dict[str, Any],
) -> dict[str, int | str]:
    """Move newly imported files to Drive and evict verified local sources."""

    imported_paths = {
        str(Path(value).expanduser().resolve())
        for value in import_report.get("imported", [])
        if value
    }
    summary: dict[str, int | str] = {
        "status": "completed",
        "total": len(imported_paths),
        "uploaded": 0,
        "already_remote": 0,
        "evicted": 0,
        "already_evicted": 0,
        "cleanup_failed": 0,
        "missing_catalog": 0,
        "failed": 0,
    }
    if settings.storage_primary_backend != "google_drive":
        summary["status"] = "local_primary"
        return summary
    if not imported_paths:
        return summary

    catalog_files = list(
        db.scalars(select(LibraryFile).where(LibraryFile.path.in_(imported_paths)))
    )
    summary["missing_catalog"] = len(imported_paths) - len(catalog_files)
    for file in catalog_files:
        try:
            uploaded, eviction = move_catalog_file_to_drive(db, file)
            key = "uploaded" if uploaded else "already_remote"
            summary[key] = int(summary[key]) + 1
            if eviction in {"evicted", "already_evicted"}:
                summary[eviction] = int(summary[eviction]) + 1
            else:
                summary["cleanup_failed"] = int(summary["cleanup_failed"]) + 1
        except StorageError:
            db.rollback()
            summary["failed"] = int(summary["failed"]) + 1
    if (
        int(summary["failed"])
        or int(summary["missing_catalog"])
        or int(summary["cleanup_failed"])
    ):
        summary["status"] = "degraded"
    return summary


def storage_account_payload(account: StorageAccount) -> dict[str, Any]:
    return {
        "id": account.id,
        "provider": account.provider,
        "email": account.email,
        "label": account.label,
        "enabled": bool(account.enabled),
        "priority": int(account.priority),
        "state": account.state,
        "detail_code": account.detail_code,
        "quota_limit_bytes": account.quota_limit_bytes,
        "quota_usage_bytes": account.quota_usage_bytes,
        "quota_trash_bytes": account.quota_trash_bytes,
        "free_bytes": account_free_bytes(account),
        "credential_version": int(account.credential_version),
        "last_checked_at": account.last_checked_at,
        "created_at": account.created_at,
        "updated_at": account.updated_at,
    }
