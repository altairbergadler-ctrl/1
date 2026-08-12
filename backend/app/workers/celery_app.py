from celery import Celery

from app.config import settings
from app.services import playlist_sync_revision as _playlist_sync_revision  # noqa: F401

celery = Celery(
    "music_service",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.workers.tasks"],
)
celery.conf.update(
    task_track_started=True,
    timezone=settings.timezone,
    enable_utc=True,
    worker_prefetch_multiplier=1,
    task_always_eager=settings.celery_task_always_eager,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    broker_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout_seconds,
    },
    result_backend_transport_options={
        "visibility_timeout": settings.celery_visibility_timeout_seconds,
    },
    visibility_timeout=settings.celery_visibility_timeout_seconds,
    task_routes={
        "acquisition_batch": {"queue": "acquisition"},
    },
    beat_schedule={
        "acquisition-dispatch": {
            "task": "acquisition_dispatch",
            "schedule": settings.acquisition_dispatch_interval_seconds,
        },
        "qobuz-provider-health": {
            "task": "provider_health_check",
            "schedule": settings.provider_health_interval_seconds,
            "args": ("qobuz",),
        },
        "yandex-provider-health": {
            "task": "provider_health_check",
            "schedule": settings.provider_health_interval_seconds,
            "args": ("yandex",),
        },
        "google-drive-health": {
            "task": "storage_health_check",
            "schedule": settings.provider_health_interval_seconds,
        },
        "google-drive-reconcile": {
            "task": "storage_reconcile",
            "schedule": settings.storage_reconcile_interval_seconds,
        },
    },
)
