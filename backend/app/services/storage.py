"""Google Drive account lifecycle, placement and verified replication."""

from __future__ import annotations

import hashlib
import secrets
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
    existing = db.scalar(
        select(DriveFileLocation).where(
            DriveFileLocation.file_id == file.id,
            DriveFileLocation.state == "healthy",
        )
    )
    if existing is not None:
        return existing
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
    progress_callback: Callable[[dict[str, int]], None] | None = None,
) -> dict[str, int]:
    files = list(db.scalars(select(LibraryFile).order_by(LibraryFile.id)))
    summary = {
        "total": len(files),
        "processed": 0,
        "uploaded": 0,
        "already_remote": 0,
        "skipped_local_missing": 0,
        "failed": 0,
    }
    for file in files:
        summary["processed"] += 1
        if any(location.state == "healthy" for location in file.drive_locations):
            summary["already_remote"] += 1
        elif not file.path or not Path(file.path).is_file():
            summary["skipped_local_missing"] += 1
        else:
            try:
                upload_catalog_file(db, file)
                db.commit()
                summary["uploaded"] += 1
            except StorageError:
                db.rollback()
                summary["failed"] += 1
        if progress_callback is not None:
            progress_callback(dict(summary))
    return summary


def replicate_imported_files(
    db: Session,
    import_report: dict[str, Any],
) -> dict[str, int | str]:
    """Copy newly imported catalog files to Drive without removing local data."""

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
        if any(location.state == "healthy" for location in file.drive_locations):
            summary["already_remote"] = int(summary["already_remote"]) + 1
            continue
        try:
            upload_catalog_file(db, file)
            db.commit()
            summary["uploaded"] = int(summary["uploaded"]) + 1
        except StorageError:
            db.rollback()
            summary["failed"] = int(summary["failed"]) + 1
    if int(summary["failed"]) or int(summary["missing_catalog"]):
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
