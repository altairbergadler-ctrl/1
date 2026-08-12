import json
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select, text, update
from sqlalchemy.orm import Session

from app.auth import require_auth, require_csrf, require_owner_csrf
from app.config import settings
from app.db import get_db
from app.models import Job, JobScope, JobStatus, Playlist, User, utcnow
from app.schemas import (
    JobOut,
    QobuzConnectOut,
    QobuzDownloadEligibilityOut,
    QobuzDownloadUrlIn,
    QobuzFetchMissingIn,
    QobuzSearchOut,
    QobuzStatusOut,
)
from app.services.credentials import has_credential
from app.services.qobuz import (
    QobuzAuthError,
    QobuzConfigurationError,
    QobuzProviderError,
    QobuzServiceError,
    QobuzSidecarClient,
    create_qobuz_client,
    is_qobuz_configured,
    qobuz_download_eligibility,
    search_albums,
    search_tracks,
)
from app.workers.tasks import qobuz_download_task

# ============================================================================
# API интеграции Qobuz. Все endpoints закрыты user-scoped Google session,
# как и остальные пользовательские роутеры сервиса.
#
# Общие правила маппинга ошибок (ограничения RESTRICT, assessment sections 3/7:
# API никогда не возвращает секреты/токены — в detail только нейтральные
# формулировки без email, токена и параметров запроса):
#   503 — интеграция выключена или encrypted credential не настроен;
#   400 — Qobuz отклонил креденшелы/токен (QobuzAuthError);
#   502 — Qobuz недоступен или ответил ошибкой (QobuzProviderError).
#
# Download-endpoints не выполняют работу сами, а ставят фоновое задание
# (202 + JobOut). Одновременно активно только ОДНО задание типа
# qobuz_download: повторный запрос возвращает уже идущее — это ограничение
# RESTRICT против параллельных сессий скачивания и бана аккаунта.
# ============================================================================
router = APIRouter(dependencies=[Depends(require_auth)])

# ID транзакционной advisory-блокировки PostgreSQL на время «погасить stale +
# найти активное + создать новое» — сериализует конкурирующие HTTP-запросы,
# чтобы гонка не создала два активных qobuz-job (тот же паттерн, что у
# scan/matching роутеров; значение уникально среди lock-id проекта).
_QOBUZ_QUEUE_LOCK_ID = 2026081005


# «Креденшелы заданы» для ответа /status: отличается от is_qobuz_configured
# тем, что не смотрит на флаг QOBUZ_ENABLED — фронтенд показывает оба факта
# раздельно («не настроен» vs «включён, но без креденшелов»). Сами значения
# секретов здесь (и anywhere else в API) не возвращаются.
def _sidecar_status() -> dict:
    if not is_qobuz_configured(settings):
        return {"configured": False}
    try:
        return QobuzSidecarClient(
            settings.qobuz_sidecar_url,
            settings.qobuz_internal_token,
        ).status()
    except QobuzServiceError:
        return {"configured": False}


# 503, если интеграция выключена или креденшелы не заданы: дальше идти
# бессмысленно, а создание обречённого задания только засорит очередь.
def _require_configured(db: Session) -> None:
    if not is_qobuz_configured(settings) or not has_credential(db, "qobuz"):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qobuz is not configured",
        )


# Создание клиента = одновременно проверка логина (connect/search). Ошибки
# маппятся в HTTP без утечки секретов: 503 не настроен, 400 креденшелы
# отклонены, 502 провайдер недоступен.
def _make_client(db: Session):
    try:
        return create_qobuz_client(db)
    except QobuzConfigurationError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Qobuz is not configured",
        ) from exc
    except QobuzAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Qobuz credentials were rejected",
        ) from exc
    except QobuzProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Qobuz is unavailable",
        ) from exc


@router.get("/status", response_model=QobuzStatusOut)
def qobuz_status(db: Session = Depends(get_db)):
    # Только нечувствительные флаги и лимиты: enabled (выключатель),
    # configured (креденшелы заданы?), качество и лимит треков за запуск.
    # Секреты/токены/API-ответы Qobuz здесь не возвращаются никогда.
    sidecar = _sidecar_status()
    return {
        "enabled": settings.qobuz_enabled,
        "configured": bool(
            sidecar.get("configured") and has_credential(db, "qobuz")
        ),
        "quality": settings.qobuz_quality,
        "max_tracks_per_run": settings.qobuz_max_tracks_per_run,
        "batch_delay_seconds": settings.qobuz_batch_delay_seconds,
    }


def _payload_playlist_id(payload: str | None) -> int | None:
    try:
        data = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("playlist_id")
    if value is None and isinstance(data.get("downloads"), dict):
        value = data["downloads"].get("playlist_id")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@router.get("/download-status/{playlist_id}", response_model=JobOut | None)
def qobuz_download_status(
    playlist_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    """Return persisted progress for the latest Qobuz run of a playlist."""

    if db.scalar(
        select(Playlist.id).where(
            Playlist.id == playlist_id,
            Playlist.user_id == current_user.id,
        )
    ) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    jobs = db.scalars(
        select(Job)
        .where(
            Job.type == "qobuz_download",
            Job.user_id == current_user.id,
        )
        .order_by(Job.created_at.desc(), Job.id.desc())
    )
    return next(
        (job for job in jobs if _payload_playlist_id(job.payload) == playlist_id),
        None,
    )


@router.post("/downloads/{job_id}/pause", response_model=JobOut)
def pause_qobuz_download(
    job_id: int,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Request a safe pause after the current batch is stored in Drive."""

    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.type == "qobuz_download",
            Job.user_id == current_user.id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Qobuz job not found")
    if job.status in (JobStatus.done, JobStatus.failed):
        raise HTTPException(status_code=409, detail="Qobuz job is already finished")
    now = utcnow()
    if job.status == JobStatus.pending and job.lock_owner is None:
        job.paused_at = now
        job.pause_requested_at = None
        job.error = None
    elif job.paused_at is None:
        job.pause_requested_at = now
    db.commit()
    db.refresh(job)
    return job


@router.post("/downloads/{job_id}/resume", response_model=JobOut)
def resume_qobuz_download(
    job_id: int,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    """Resume a cooperatively paused job from its persisted provider ledger."""

    job = db.scalar(
        select(Job).where(
            Job.id == job_id,
            Job.type == "qobuz_download",
            Job.user_id == current_user.id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Qobuz job not found")
    if job.paused_at is None:
        if job.pause_requested_at is not None:
            raise HTTPException(
                status_code=409,
                detail="Qobuz is still draining the current batch",
            )
        raise HTTPException(status_code=409, detail="Qobuz job is not paused")
    try:
        payload = json.loads(job.payload or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise HTTPException(
            status_code=409, detail="Qobuz job cannot be resumed"
        ) from exc
    mode = payload.get("mode")
    playlist_id = payload.get("playlist_id")
    url = payload.get("url")
    if mode not in {"fetch_missing", "url"}:
        raise HTTPException(status_code=409, detail="Qobuz job cannot be resumed")
    job.status = JobStatus.pending
    job.pause_requested_at = None
    job.paused_at = None
    job.error = None
    job.finished_at = None
    job.heartbeat_at = utcnow()
    job.lock_owner = None
    db.commit()
    db.refresh(job)
    try:
        qobuz_download_task.delay(job.id, mode, playlist_id, url)
    except Exception as exc:
        job.paused_at = utcnow()
        job.error = f"Could not resume Qobuz job ({type(exc).__name__})"
        db.commit()
        raise HTTPException(
            status_code=503,
            detail="Qobuz download queue is unavailable",
        ) from exc
    return job


@router.get(
    "/download-eligibility/{playlist_id}",
    response_model=QobuzDownloadEligibilityOut,
)
def qobuz_eligibility(
    playlist_id: int,
    current_user: User = Depends(require_auth),
    db: Session = Depends(get_db),
):
    playlist = db.scalar(
        select(Playlist).where(
            Playlist.id == playlist_id,
            Playlist.user_id == current_user.id,
        )
    )
    if playlist is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return qobuz_download_eligibility(db, playlist)


@router.post("/connect", response_model=QobuzConnectOut)
def qobuz_connect(
    _: User = Depends(require_owner_csrf),
    db: Session = Depends(get_db),
):
    # «Подключить» = попытка создать клиента: это и есть проверка логина.
    # При успехе отдаём только label тарифа (например Studio) — он не секрет
    # и нужен фронтенду как подтверждение активной платной подписки
    # (бесплатные аккаунты qobuz-dl отклоняет сам через IneligibleError,
    # assessment section 3). Коды: 503 не настроен, 400 креденшелы отклонены
    # (в т.ч. протухший активный credential), 502 Qobuz недоступен.
    _require_configured(db)
    client = _make_client(db)
    return {"connected": True, "label": getattr(client, "label", None)}


@router.get("/search", response_model=QobuzSearchOut)
def qobuz_search(
    q: str = Query(min_length=1, max_length=256),
    kind: str = Query(default="track", alias="type", pattern="^(track|album)$"),
    limit: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
):
    # Живой поиск по каталогу Qobuz (track|album). Параметр называется kind,
    # потому что имя type занято builtin; наружу — alias "type".
    # Ошибки маппятся так же, как в connect: 503/400/502.
    _require_configured(db)
    client = _make_client(db)
    try:
        candidates = (
            search_tracks(client, q, limit)
            if kind == "track"
            else search_albums(client, q, limit)
        )
    except QobuzAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Qobuz credentials were rejected",
        ) from exc
    except QobuzProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Qobuz is unavailable",
        ) from exc
    return {
        "items": [
            {
                "kind": kind,
                "qobuz_id": candidate.qobuz_id,
                "artist": candidate.artist,
                "title": candidate.title,
                "album": candidate.album,
                "duration_ms": candidate.duration_ms,
                "isrc": candidate.isrc,
                "hires": candidate.hires,
                "url": candidate.url,
            }
            for candidate in candidates
        ]
    }


def _queue_qobuz_job(
    db: Session,
    *,
    user_id: int,
    mode: str,
    playlist_id: int | None = None,
    url: str | None = None,
) -> Job:
    # Паттерн «одно активное задание» скопирован с api/library.py::scan.
    # Advisory-блокировка (только PostgreSQL) сериализует конкурирующие
    # запросы на создание job; на SQLite (тесты) гонки нет по построению.
    if db.get_bind().dialect.name == "postgresql":
        db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_id)"),
            {"lock_id": _QOBUZ_QUEUE_LOCK_ID},
        )

    # Stale-cutoff: задание, чей heartbeat молчит дольше
    # QOBUZ_DOWNLOAD_JOB_STALE_SECONDS, считается зависшим и переводится в
    # failed, чтобы не блокировать очередь навечно. Network timeout sidecar
    # остаётся первой линией защиты.
    now = utcnow()
    cutoff = now - timedelta(seconds=settings.qobuz_download_job_stale_seconds)
    stale_job_ids = db.scalars(
        update(Job)
        .where(
            Job.type == "qobuz_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
            Job.heartbeat_at < cutoff,
        )
        .values(
            status=JobStatus.failed,
            error="Qobuz download job expired before completion",
            finished_at=now,
            lock_owner=None,
        )
        .returning(Job.id)
        .execution_options(synchronize_session=False)
    ).all()

    active_job = db.scalar(
        select(Job)
        .where(
            Job.type == "qobuz_download",
            Job.status.in_([JobStatus.pending, JobStatus.running]),
        )
        .order_by(Job.created_at.desc())
    )
    if active_job is not None:
        # Ограничение RESTRICT (assessment section 7): один активный qobuz-job.
        # Повторный запрос НЕ создаёт параллельное скачивание, а возвращает
        # уже идущее задание — клиент просто продолжает его polling.
        active_payload = json.loads(active_job.payload or "{}")
        requested_payload = {"mode": mode, "playlist_id": playlist_id, "url": url}
        if (
            active_job.user_id == user_id
            and all(
                active_payload.get(key) == value
                for key, value in requested_payload.items()
            )
        ):
            if stale_job_ids:
                db.commit()
            return active_job
        if stale_job_ids:
            db.commit()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Another Qobuz download is already running",
        )

    job = Job(
        type="qobuz_download",
        playlist_id=playlist_id,
        user_id=user_id,
        scope=JobScope.user,
        status=JobStatus.pending,
        heartbeat_at=utcnow(),
        payload=json.dumps(
            {"mode": mode, "playlist_id": playlist_id, "url": url},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    try:
        # Постановка в очередь Celery. При celery_task_always_eager (локальный
        # режим без worker'а) задача выполнится синхронно прямо здесь — тогда
        # перечитываем job, чтобы вернуть уже финальное состояние.
        qobuz_download_task.delay(job.id, mode, playlist_id, url)
        if settings.celery_task_always_eager:
            db.refresh(job)
    except Exception as exc:
        # Redis/брокер недоступен: честно помечаем задание failed и отвечаем
        # 503, а не оставляем «вечно pending» job.
        job.status = JobStatus.failed
        job.error = f"Could not enqueue qobuz download job ({type(exc).__name__})"
        job.finished_at = utcnow()
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"job_id": job.id, "message": "Qobuz download queue is unavailable"},
        ) from exc
    return job


@router.post(
    "/download-url",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def qobuz_download_url(
    payload: QobuzDownloadUrlIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    # Скачивание по ссылке play.qobuz.com/... — ставит фоновое задание
    # (202). Поддерживаемые типы URL проверяет сервис внутри задания:
    # в MVP только album и track; playlist/artist/label завершат задание
    # ошибкой QobuzProviderError с понятным сообщением (ограничение MVP).
    _require_configured(db)
    return _queue_qobuz_job(
        db, user_id=current_user.id, mode="url", url=payload.url.strip()
    )


@router.post(
    "/fetch-missing",
    response_model=JobOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def qobuz_fetch_missing(
    payload: QobuzFetchMissingIn,
    current_user: User = Depends(require_csrf),
    db: Session = Depends(get_db),
):
    # Докачка MISSING-треков плейлиста из каталога Qobuz. 404, если плейлиста
    # нет (проверяем до создания задания, чтобы не ставить обречённый job);
    # 503 — интеграция не настроена. Иначе 202 + активное задание.
    _require_configured(db)
    if db.scalar(
        select(Playlist.id).where(
            Playlist.id == payload.playlist_id,
            Playlist.user_id == current_user.id,
        )
    ) is None:
        raise HTTPException(status_code=404, detail="Playlist not found")
    return _queue_qobuz_job(
        db,
        user_id=current_user.id,
        mode="fetch_missing",
        playlist_id=payload.playlist_id,
    )
