"""Provider credential rotation and non-sensitive component health APIs."""

from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.auth import require_auth, require_same_origin
from app.config import settings
from app.db import get_db
from app.models import Job, JobStatus, PlaylistSource, ProviderHealth, ServiceEnum, utcnow
from app.schemas import (
    JobOut,
    ProviderCredentialOut,
    ProviderHealthListOut,
    ProviderHealthOut,
    QobuzCredentialIn,
    YandexCredentialIn,
)
from app.services.credentials import (
    CredentialError,
    encrypt_payload,
    get_credential_record,
    save_credential,
)
from app.services.provider_health import (
    PROVIDERS,
    provider_health_snapshot,
    record_rotation_success,
)
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    QobuzRateLimitedError,
    QobuzSidecarClient,
)
from app.services.yandex import create_yandex_client
from app.workers.tasks import provider_health_check_task

router = APIRouter(dependencies=[Depends(require_auth)])
_ROTATION_LOCK_NAMESPACE = 2026081001


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _provider_or_404(provider: str) -> str:
    provider = str(provider).casefold()
    if provider not in PROVIDERS:
        raise HTTPException(status_code=404, detail="Provider not found")
    return provider


def _lock_rotation(db: Session, provider: str) -> None:
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:provider))"),
            {"namespace": _ROTATION_LOCK_NAMESPACE, "provider": provider},
        )


def _credential_out(record, *, label: str | None = None) -> ProviderCredentialOut:
    return ProviderCredentialOut(
        provider=record.provider,
        configured=True,
        version=record.version,
        updated_at=record.updated_at,
        label=label,
    )


def _exception_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/health", response_model=ProviderHealthListOut)
def provider_health(response: Response, db: Session = Depends(get_db)):
    _no_store(response)
    return {"items": [provider_health_snapshot(db, item) for item in PROVIDERS]}


@router.get("/{provider}/health", response_model=ProviderHealthOut)
def provider_health_one(
    provider: str,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    return provider_health_snapshot(db, _provider_or_404(provider))


@router.put(
    "/qobuz/credentials",
    response_model=ProviderCredentialOut,
    dependencies=[Depends(require_same_origin)],
)
def rotate_qobuz_credential(
    payload: QobuzCredentialIn,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    _lock_rotation(db, "qobuz")
    current = get_credential_record(db, "qobuz")
    version = int(current.version) + 1 if current is not None else 1
    credential = {
        "token": payload.token.get_secret_value().strip(),
        "user_id": payload.user_id.get_secret_value().strip(),
    }
    try:
        envelope = encrypt_payload("qobuz", credential, version)
        client = QobuzSidecarClient(
            settings.qobuz_sidecar_url,
            settings.qobuz_internal_token,
        )
        result = client.validate_credential(envelope)
    except QobuzRateLimitedError as exc:
        db.rollback()
        raise HTTPException(status_code=429, detail="Provider rate limit is active") from exc
    except QobuzAuthError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="Credential validation failed") from exc
    except QobuzConfigurationError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Qobuz sidecar is not ready") from exc
    except QobuzProviderError as exc:
        db.rollback()
        raise HTTPException(status_code=502, detail="Qobuz validation is unavailable") from exc
    except CredentialError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Credential storage is unavailable") from exc

    try:
        record = save_credential(db, "qobuz", credential, validated_at=utcnow())
    except CredentialError as exc:
        db.rollback()
        raise HTTPException(
            status_code=503,
            detail="Credential storage is unavailable",
        ) from exc
    record_rotation_success(
        db,
        "qobuz",
        credential_version=record.version,
        sidecar_healthy=True,
    )
    db.commit()
    db.refresh(record)
    return _credential_out(record, label=str(result.get("label") or "") or None)


@router.put(
    "/yandex/credentials",
    response_model=ProviderCredentialOut,
    dependencies=[Depends(require_same_origin)],
)
def rotate_yandex_credential(
    payload: YandexCredentialIn,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    _lock_rotation(db, "yandex")
    token = payload.token.get_secret_value().strip()
    try:
        create_yandex_client(token)
    except Exception as exc:
        db.rollback()
        provider_status = _exception_status(exc)
        name = type(exc).__name__
        if provider_status == 429 or name == "TooManyRequestsError":
            raise HTTPException(status_code=429, detail="Provider rate limit is active") from exc
        if provider_status in {401, 403} or name in {
            "UnauthorizedError",
            "ForbiddenError",
        }:
            raise HTTPException(status_code=400, detail="Credential validation failed") from exc
        raise HTTPException(status_code=502, detail="Yandex validation is unavailable") from exc

    try:
        record = save_credential(
            db,
            "yandex",
            {"token": token},
            validated_at=utcnow(),
        )
    except CredentialError as exc:
        db.rollback()
        raise HTTPException(status_code=503, detail="Credential storage is unavailable") from exc
    source = db.scalar(
        select(PlaylistSource).where(PlaylistSource.service == ServiceEnum.yandex)
    )
    if source is None:
        source = PlaylistSource(service=ServiceEnum.yandex)
        db.add(source)
    source.access_token = None
    source.refresh_token = None
    record_rotation_success(
        db,
        "yandex",
        credential_version=record.version,
        sidecar_healthy=False,
    )
    db.commit()
    db.refresh(record)
    return _credential_out(record)


@router.post(
    "/{provider}/health-check",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_same_origin)],
)
def run_provider_health(
    provider: str,
    response: Response,
    db: Session = Depends(get_db),
):
    _no_store(response)
    provider = _provider_or_404(provider)
    cutoff = utcnow() - timedelta(seconds=settings.provider_health_manual_cooldown_seconds)
    recent = db.scalar(
        select(ProviderHealth)
        .where(
            ProviderHealth.provider == provider,
            ProviderHealth.checked_at >= cutoff,
        )
        .limit(1)
    )
    if recent is not None:
        raise HTTPException(status_code=429, detail="Health check cooldown is active")
    job = Job(
        type="provider_health_check",
        status=JobStatus.pending,
        payload=json.dumps({"provider": provider}, separators=(",", ":")),
        heartbeat_at=utcnow(),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        provider_health_check_task.delay(provider, job.id)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        job.status = JobStatus.failed
        job.error = f"Could not enqueue provider health check ({type(exc).__name__})"
        job.finished_at = utcnow()
        job.heartbeat_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=503,
            detail={"job_id": job.id, "message": "Health-check queue is unavailable"},
        ) from exc
    return job
