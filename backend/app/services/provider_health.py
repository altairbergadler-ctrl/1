"""Non-sensitive component health for Qobuz and Yandex."""

from __future__ import annotations

import time
from datetime import timedelta
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    ProviderHealth,
    ProviderHealthComponent,
    ProviderHealthState,
    utcnow,
)
from app.services.credentials import (
    CredentialError,
    get_credential_payload,
    get_credential_record,
)
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    QobuzRateLimitedError,
    QobuzServiceError,
    QobuzSidecarClient,
    create_qobuz_client,
    is_qobuz_configured,
)
from app.services.yandex import create_yandex_client

PROVIDERS = ("qobuz", "yandex")
COMPONENTS = tuple(ProviderHealthComponent)
_PROVIDER_LOCK_NAMESPACE = 2026081001


def _upsert(
    db: Session,
    provider: str,
    component: ProviderHealthComponent,
    state: ProviderHealthState,
    *,
    detail_code: str | None = None,
    latency_ms: int | None = None,
    credential_version: int | None = None,
    retry_at=None,
) -> ProviderHealth:
    row = db.scalar(
        select(ProviderHealth).where(
            ProviderHealth.provider == provider,
            ProviderHealth.component == component,
        )
    )
    if row is None:
        row = ProviderHealth(provider=provider, component=component)
        db.add(row)
    row.state = state
    row.detail_code = detail_code
    row.latency_ms = latency_ms
    row.credential_version = credential_version
    row.checked_at = utcnow()
    row.retry_at = retry_at
    return row


def record_rotation_success(
    db: Session,
    provider: str,
    *,
    credential_version: int,
    sidecar_healthy: bool,
) -> None:
    for component in (
        ProviderHealthComponent.account,
        ProviderHealthComponent.provider_api,
    ):
        _upsert(
            db,
            provider,
            component,
            ProviderHealthState.healthy,
            detail_code="credential_validated",
            credential_version=credential_version,
        )
    if sidecar_healthy:
        _upsert(
            db,
            provider,
            ProviderHealthComponent.sidecar,
            ProviderHealthState.healthy,
            detail_code="sidecar_ready",
            credential_version=credential_version,
        )


def _qobuz_check(db: Session, *, worker_healthy: bool) -> None:
    record = get_credential_record(db, "qobuz")
    version = record.version if record else None
    if not settings.qobuz_enabled or not is_qobuz_configured(settings):
        for component in (
            ProviderHealthComponent.account,
            ProviderHealthComponent.provider_api,
            ProviderHealthComponent.sidecar,
        ):
            _upsert(
                db,
                "qobuz",
                component,
                ProviderHealthState.not_configured,
                detail_code="integration_disabled",
                credential_version=version,
            )
        _upsert(
            db,
            "qobuz",
            ProviderHealthComponent.worker,
            (
                ProviderHealthState.healthy
                if worker_healthy
                else ProviderHealthState.provider_down
            ),
            detail_code="worker_check_executed" if worker_healthy else "worker_stale",
            credential_version=version,
        )
        return

    started = time.monotonic()
    sidecar_ok = False
    try:
        status = QobuzSidecarClient(
            settings.qobuz_sidecar_url, settings.qobuz_internal_token
        ).status()
        sidecar_ok = bool(status.get("configured"))
        _upsert(
            db,
            "qobuz",
            ProviderHealthComponent.sidecar,
            ProviderHealthState.healthy if sidecar_ok else ProviderHealthState.not_configured,
            detail_code="sidecar_ready" if sidecar_ok else "credential_key_missing",
            latency_ms=int((time.monotonic() - started) * 1000),
            credential_version=version,
        )
    except QobuzServiceError:
        _upsert(
            db,
            "qobuz",
            ProviderHealthComponent.sidecar,
            ProviderHealthState.provider_down,
            detail_code="sidecar_unreachable",
            latency_ms=int((time.monotonic() - started) * 1000),
            credential_version=version,
        )

    if record is None:
        for component in (
            ProviderHealthComponent.account,
            ProviderHealthComponent.provider_api,
        ):
            _upsert(
                db,
                "qobuz",
                component,
                ProviderHealthState.not_configured,
                detail_code="credential_missing",
            )
    elif not sidecar_ok:
        for component in (
            ProviderHealthComponent.account,
            ProviderHealthComponent.provider_api,
        ):
            _upsert(
                db,
                "qobuz",
                component,
                ProviderHealthState.provider_down,
                detail_code="sidecar_unavailable",
                credential_version=version,
            )
    else:
        started = time.monotonic()
        try:
            create_qobuz_client(db)
            state = ProviderHealthState.healthy
            detail = "provider_ok"
            account_state = state
        except QobuzRateLimitedError:
            state = ProviderHealthState.rate_limited
            account_state = state
            detail = "provider_rate_limited"
        except QobuzAuthError:
            state = ProviderHealthState.healthy
            account_state = ProviderHealthState.expired
            detail = "credential_rejected"
        except (QobuzConfigurationError, CredentialError):
            state = ProviderHealthState.not_configured
            account_state = state
            detail = "credential_unavailable"
        except QobuzProviderError:
            state = ProviderHealthState.provider_down
            account_state = state
            detail = "provider_unavailable"
        latency = int((time.monotonic() - started) * 1000)
        retry_at = (
            utcnow() + timedelta(minutes=15)
            if account_state == ProviderHealthState.rate_limited
            else None
        )
        _upsert(
            db,
            "qobuz",
            ProviderHealthComponent.account,
            account_state,
            detail_code=detail,
            latency_ms=latency,
            credential_version=version,
            retry_at=retry_at,
        )
        _upsert(
            db,
            "qobuz",
            ProviderHealthComponent.provider_api,
            state,
            detail_code=detail,
            latency_ms=latency,
            credential_version=version,
            retry_at=retry_at,
        )

    _upsert(
        db,
        "qobuz",
        ProviderHealthComponent.worker,
        (
            ProviderHealthState.healthy
            if worker_healthy
            else ProviderHealthState.provider_down
        ),
        detail_code="worker_check_executed" if worker_healthy else "worker_stale",
        credential_version=version,
    )


def _exception_status(exc: Exception) -> int | None:
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _yandex_check(db: Session, *, worker_healthy: bool) -> None:
    record = get_credential_record(db, "yandex")
    version = record.version if record else None
    if not settings.yandex_download_enabled:
        for component in (
            ProviderHealthComponent.account,
            ProviderHealthComponent.provider_api,
            ProviderHealthComponent.sidecar,
        ):
            _upsert(
                db,
                "yandex",
                component,
                ProviderHealthState.not_configured,
                detail_code="integration_disabled",
                credential_version=version,
            )
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.worker,
            (
                ProviderHealthState.healthy
                if worker_healthy
                else ProviderHealthState.provider_down
            ),
            detail_code="worker_check_executed" if worker_healthy else "worker_stale",
            credential_version=version,
        )
        return

    signer_url = settings.yandex_signer_url.rstrip("/")
    parsed = urlparse(signer_url)
    signer_allowed = (
        parsed.scheme == "http"
        and parsed.hostname in {"yandex-signer", "127.0.0.1", "localhost"}
        and len(settings.yandex_internal_token.strip()) >= 16
    )
    started = time.monotonic()
    try:
        if not signer_allowed:
            raise ValueError("signer not configured")
        response = httpx.get(
            f"{signer_url}/health",
            timeout=min(5.0, settings.yandex_connect_timeout_seconds),
        )
        response.raise_for_status()
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.sidecar,
            ProviderHealthState.healthy,
            detail_code="signer_ready",
            latency_ms=int((time.monotonic() - started) * 1000),
            credential_version=version,
        )
    except ValueError:
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.sidecar,
            ProviderHealthState.not_configured,
            detail_code="signer_not_configured",
            credential_version=version,
        )
    except httpx.HTTPError:
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.sidecar,
            ProviderHealthState.provider_down,
            detail_code="signer_unreachable",
            latency_ms=int((time.monotonic() - started) * 1000),
            credential_version=version,
        )

    if record is None:
        for component in (
            ProviderHealthComponent.account,
            ProviderHealthComponent.provider_api,
        ):
            _upsert(
                db,
                "yandex",
                component,
                ProviderHealthState.not_configured,
                detail_code="credential_missing",
            )
    else:
        started = time.monotonic()
        try:
            token = str(get_credential_payload(db, "yandex").get("token") or "")
            if not token:
                raise CredentialError("credential missing")
            create_yandex_client(token)
            api_state = ProviderHealthState.healthy
            account_state = ProviderHealthState.healthy
            detail = "provider_ok"
        except CredentialError:
            api_state = account_state = ProviderHealthState.not_configured
            detail = "credential_unavailable"
        except Exception as exc:
            status_code = _exception_status(exc)
            name = type(exc).__name__
            if status_code == 429 or name in {"TooManyRequestsError"}:
                api_state = account_state = ProviderHealthState.rate_limited
                detail = "provider_rate_limited"
            elif status_code in {401, 403} or name in {
                "UnauthorizedError",
                "ForbiddenError",
            }:
                api_state = ProviderHealthState.healthy
                account_state = ProviderHealthState.expired
                detail = "credential_rejected"
            else:
                api_state = account_state = ProviderHealthState.provider_down
                detail = "provider_unavailable"
        latency = int((time.monotonic() - started) * 1000)
        retry_at = (
            utcnow() + timedelta(minutes=15)
            if account_state == ProviderHealthState.rate_limited
            else None
        )
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.account,
            account_state,
            detail_code=detail,
            latency_ms=latency,
            credential_version=version,
            retry_at=retry_at,
        )
        _upsert(
            db,
            "yandex",
            ProviderHealthComponent.provider_api,
            api_state,
            detail_code=detail,
            latency_ms=latency,
            credential_version=version,
            retry_at=retry_at,
        )

    _upsert(
        db,
        "yandex",
        ProviderHealthComponent.worker,
        (
            ProviderHealthState.healthy
            if worker_healthy
            else ProviderHealthState.provider_down
        ),
        detail_code="worker_check_executed" if worker_healthy else "worker_stale",
        credential_version=version,
    )


def run_provider_health_check(
    db: Session, provider: str, *, worker_healthy: bool = True
) -> dict[str, Any]:
    provider = str(provider).casefold()
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider")
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:provider))"),
            {"namespace": _PROVIDER_LOCK_NAMESPACE, "provider": provider},
        )
    if provider == "qobuz":
        _qobuz_check(db, worker_healthy=worker_healthy)
    else:
        _yandex_check(db, worker_healthy=worker_healthy)
    db.commit()
    return provider_health_snapshot(db, provider)


def provider_health_snapshot(db: Session, provider: str) -> dict[str, Any]:
    provider = str(provider).casefold()
    if provider not in PROVIDERS:
        raise ValueError("Unsupported provider")
    credential = get_credential_record(db, provider)
    rows = {
        row.component: row
        for row in db.scalars(
            select(ProviderHealth).where(ProviderHealth.provider == provider)
        )
    }
    now = utcnow()
    configured = credential is not None
    components: dict[str, Any] = {}
    for component in COMPONENTS:
        row = rows.get(component)
        if row is None:
            state = (
                ProviderHealthState.provider_down
                if configured and component == ProviderHealthComponent.worker
                else ProviderHealthState.not_configured
            )
            components[component.value] = {
                "state": state.value,
                "detail_code": "check_not_run",
                "checked_at": None,
                "latency_ms": None,
                "credential_version": credential.version if credential else None,
                "retry_at": None,
                "stale": configured,
            }
            continue
        stale = (now - row.checked_at).total_seconds() > settings.provider_health_stale_seconds
        state = row.state
        detail = row.detail_code
        if stale and component == ProviderHealthComponent.worker:
            state = ProviderHealthState.provider_down
            detail = "worker_stale"
        components[component.value] = {
            "state": state.value,
            "detail_code": detail,
            "checked_at": row.checked_at,
            "latency_ms": row.latency_ms,
            "credential_version": row.credential_version,
            "retry_at": row.retry_at,
            "stale": stale,
        }
    return {
        "provider": provider,
        "configured": configured,
        "credential_version": credential.version if credential else None,
        "credential_updated_at": credential.updated_at if credential else None,
        **components,
    }
