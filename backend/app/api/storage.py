"""Non-sensitive storage management and Google OAuth endpoints."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import require_owner, require_owner_csrf
from app.config import settings
from app.db import get_db
from app.models import Job, JobScope, JobStatus, StorageAccount, User, utcnow
from app.schemas import (
    GoogleOAuthConfigIn,
    GoogleOAuthStartOut,
    JobOut,
    StorageAccountOut,
    StorageAccountUpdateIn,
    StorageOverviewOut,
)
from app.services.google_drive import GoogleDriveAuthError, GoogleDriveError
from app.services.storage import (
    StorageError,
    begin_google_oauth,
    complete_google_oauth,
    discard_google_oauth_state,
    oauth_config_status,
    save_pending_oauth_config,
    storage_account_payload,
)
from app.services.storage_secrets import StorageSecretError
from app.workers.tasks import storage_health_check_task, storage_migration_task

router = APIRouter(dependencies=[Depends(require_owner)])


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _overview(db: Session) -> dict:
    accounts = list(
        db.scalars(
            select(StorageAccount).order_by(
                StorageAccount.priority.desc(), StorageAccount.id
            )
        )
    )
    oauth = oauth_config_status(db)
    limits = [account.quota_limit_bytes for account in accounts]
    total_limit = sum(int(value) for value in limits if value is not None)
    total_usage = sum(int(account.quota_usage_bytes or 0) for account in accounts)
    all_limited = all(value is not None for value in limits)
    return {
        "primary_backend": settings.storage_primary_backend,
        "configured": bool(oauth["configured"] and any(account.enabled for account in accounts)),
        "oauth": oauth,
        "accounts": [storage_account_payload(account) for account in accounts],
        "total_limit_bytes": total_limit if all_limited else None,
        "total_usage_bytes": total_usage,
        "total_free_bytes": max(0, total_limit - total_usage) if all_limited else None,
    }


@router.get(
    "",
    response_model=StorageOverviewOut,
)
def storage_overview(response: Response, db: Session = Depends(get_db)):
    _no_store(response)
    return _overview(db)


@router.put(
    "/google/oauth-config",
    response_model=GoogleOAuthStartOut,
    dependencies=[Depends(require_owner_csrf)],
)
def configure_google_oauth(
    payload: GoogleOAuthConfigIn,
    response: Response,
    current_user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    _no_store(response)
    try:
        save_pending_oauth_config(
            db,
            payload.client_id,
            payload.client_secret.get_secret_value(),
        )
        url = begin_google_oauth(db, current_user.id)
        db.commit()
    except (StorageError, StorageSecretError) as exc:
        db.rollback()
        raise HTTPException(
            status_code=400,
            detail="Google OAuth configuration is invalid",
        ) from exc
    return {"authorization_url": url}


@router.post(
    "/google/connect",
    response_model=GoogleOAuthStartOut,
    dependencies=[Depends(require_owner_csrf)],
)
def connect_google(
    response: Response,
    current_user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    _no_store(response)
    try:
        url = begin_google_oauth(db, current_user.id)
        db.commit()
    except (StorageError, StorageSecretError) as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="Google OAuth is not configured") from exc
    return {"authorization_url": url}


def _callback_redirect(result: str, value: str = "1") -> RedirectResponse:
    query = urlencode({result: value})
    response = RedirectResponse(url=f"/?{query}#/storage", status_code=303)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/google/callback", include_in_schema=False)
def google_callback(
    state: str | None = Query(default=None),
    code: str | None = Query(default=None),
    error: str | None = Query(default=None),
    current_user: User = Depends(require_owner),
    db: Session = Depends(get_db),
):
    if (
        not state
        or len(state) < 16
        or len(state) > 512
        or (code is not None and (not code or len(code) > 4096))
        or (error is not None and len(error) > 256)
    ):
        return _callback_redirect("storage_error", "oauth_invalid")
    if error or not code:
        try:
            discard_google_oauth_state(db, state, current_user.id)
            db.commit()
        except (StorageError, StorageSecretError):
            db.rollback()
        return _callback_redirect("storage_error", "oauth_denied")
    try:
        complete_google_oauth(db, state, code, current_user.id)
    except GoogleDriveAuthError:
        return _callback_redirect("storage_error", "credential_rejected")
    except (GoogleDriveError, StorageError, StorageSecretError):
        return _callback_redirect("storage_error", "provider_unavailable")
    return _callback_redirect("storage_connected")


@router.patch(
    "/accounts/{account_id}",
    response_model=StorageAccountOut,
    dependencies=[Depends(require_owner_csrf)],
)
def update_storage_account(
    account_id: int,
    payload: StorageAccountUpdateIn,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    account = db.get(StorageAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Storage account not found")
    if payload.enabled is not None:
        account.enabled = payload.enabled
    if payload.priority is not None:
        account.priority = payload.priority
    account.updated_at = utcnow()
    db.commit()
    db.refresh(account)
    return storage_account_payload(account)


@router.post(
    "/accounts/{account_id}/health-check",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_owner_csrf)],
)
def run_storage_health_check(
    account_id: int,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    account = db.get(StorageAccount, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="Storage account not found")
    job = Job(
        type="storage_health_check",
        scope=JobScope.system,
        user_id=None,
        status=JobStatus.pending,
        payload=json.dumps({"account_id": account.id}, separators=(",", ":")),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        storage_health_check_task.delay(job.id, account.id)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue storage health check ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(status_code=503, detail="Storage health queue is unavailable") from exc
    return job


@router.post(
    "/migrate-local",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_owner_csrf)],
)
def migrate_local_storage(response: Response, db: Session = Depends(get_db)):
    _no_store(response)
    active = db.scalar(
        select(Job).where(
            Job.type == "storage_migration",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
    )
    if active is not None:
        return active
    job = Job(
        type="storage_migration",
        scope=JobScope.system,
        user_id=None,
        status=JobStatus.pending,
        payload=json.dumps({"phase": "queued"}, separators=(",", ":")),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        storage_migration_task.delay(job.id)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue storage migration ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Storage migration queue is unavailable",
        ) from exc
    return job
