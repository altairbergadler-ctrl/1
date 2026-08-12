"""Provider-neutral acquisition batch with quality-aware selection."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    File,
    Job,
    JobStatus,
    MatchStatus,
    Playlist,
    PlaylistItem,
    ProviderAttempt,
    ServiceEnum,
    utcnow,
)
from app.services.matcher import run_matching
from app.services.qobuz import (
    QobuzServiceError,
    choose_track_candidate,
    create_qobuz_client,
    download_track_to_staging,
    import_files_to_library,
    mark_downloads_stored,
    provider_lookup_key,
    record_qobuz_download_attempts,
    search_tracks,
)
from app.services.scanner import ScanAlreadyRunning, scan_library
from app.services.storage import replicate_imported_files
from app.services.web_push import send_workflow_completed
from app.services.yandex_acquisition import (
    YandexAcquisitionError,
    create_yandex_acquisition_client,
    download_yandex_track_to_staging,
    get_yandex_lossless_info,
    record_yandex_download_attempts,
    search_yandex_tracks,
)


class AcquisitionPause(RuntimeError):
    """The current durable boundary requires an explicit or automatic resume."""


@dataclass(slots=True)
class ProviderChoice:
    provider: str
    candidate: Any
    selection: str
    quality_rank: tuple[int, int, int]


_LOSSLESS_FORMATS = {"flac", "alac", "wav", "wave", "aiff", "dsf", "dff"}


def _sampling_rate(value: int | None) -> int:
    rate = int(value or 0)
    return rate * 1000 if 0 < rate < 1000 else rate


def _file_quality(file: File) -> tuple[int, int, int]:
    return (
        int(str(file.format or "").casefold() in _LOSSLESS_FORMATS),
        int(file.bit_depth or 0),
        int(file.sample_rate or 0),
    )


def _current_quality(db: Session, item: PlaylistItem) -> tuple[int, int, int]:
    match = item.match
    if (
        match is None
        or match.status != MatchStatus.ready
        or match.track_id is None
    ):
        return (0, 0, 0)
    files = list(db.scalars(select(File).where(File.track_id == match.track_id)))
    return max((_file_quality(file) for file in files), default=(0, 0, 0, 0))


def _stored_by_workflow(db: Session, item: PlaylistItem, job_id: int) -> bool:
    return (
        db.scalar(
            select(ProviderAttempt.id).where(
                ProviderAttempt.lookup_key == provider_lookup_key(item),
                ProviderAttempt.job_id == job_id,
                ProviderAttempt.status.in_(["stored", "conflict"]),
            )
        )
        is not None
    )


def _record_attempt(
    db: Session,
    *,
    provider: str,
    item: PlaylistItem,
    job_id: int,
    status: str,
    provider_item_id: str | None = None,
    selection: str | None = None,
    error_code: str | None = None,
) -> None:
    key = provider_lookup_key(item)
    attempt = db.scalar(
        select(ProviderAttempt).where(
            ProviderAttempt.provider == provider,
            ProviderAttempt.lookup_key == key,
        )
    )
    if attempt is None:
        attempt = ProviderAttempt(provider=provider, lookup_key=key)
        db.add(attempt)
    attempt.playlist_item_id = item.id
    attempt.job_id = job_id
    attempt.status = status[:32]
    attempt.provider_item_id = provider_item_id
    attempt.selection_method = selection
    attempt.error_code = error_code
    attempt.attempted_at = utcnow()
    attempt.updated_at = utcnow()
    db.flush()


def _summary(payload: dict, provider: str) -> dict:
    providers = payload.setdefault("providers", {})
    return providers.setdefault(
        provider,
        {"checked": 0, "available": 0, "selected": 0, "failed": 0},
    )


def _probe_qobuz(
    db: Session,
    *,
    client: Any | None,
    item: PlaylistItem,
    job_id: int,
    payload: dict,
) -> ProviderChoice | None:
    stats = _summary(payload, "qobuz")
    stats["checked"] += 1
    if client is None:
        stats["failed"] += 1
        _record_attempt(
            db,
            provider="qobuz",
            item=item,
            job_id=job_id,
            status="unavailable",
            error_code="not_configured",
        )
        return None
    query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
    try:
        candidates = search_tracks(client, query, limit=10) if query else []
        best, method = choose_track_candidate(
            artist_raw=item.artist_raw,
            title_raw=item.title_raw,
            album_raw=item.album_raw,
            isrc=item.isrc,
            duration_ms=item.duration_ms,
            candidates=candidates,
        )
        if best is None:
            status = "ambiguous" if method == "ambiguous" else "not_found"
            _record_attempt(
                db,
                provider="qobuz",
                item=item,
                job_id=job_id,
                status=status,
                selection=method,
            )
            return None
        stats["available"] += 1
        _record_attempt(
            db,
            provider="qobuz",
            item=item,
            job_id=job_id,
            status="available",
            provider_item_id=best.qobuz_id,
            selection=method,
        )
        depth = int(best.maximum_bit_depth or 16)
        rate = _sampling_rate(best.maximum_sampling_rate or 44100)
        return ProviderChoice("qobuz", best, method, (1, depth, rate))
    except QobuzServiceError as exc:
        stats["failed"] += 1
        _record_attempt(
            db,
            provider="qobuz",
            item=item,
            job_id=job_id,
            status="failed",
            error_code=type(exc).__name__,
        )
        return None


def _probe_yandex(
    db: Session,
    *,
    client: Any | None,
    item: PlaylistItem,
    job_id: int,
    payload: dict,
) -> ProviderChoice | None:
    stats = _summary(payload, "yandex")
    stats["checked"] += 1
    if client is None:
        stats["failed"] += 1
        _record_attempt(
            db,
            provider="yandex",
            item=item,
            job_id=job_id,
            status="unavailable",
            error_code="not_configured",
        )
        return None
    query = f"{item.artist_raw or ''} {item.title_raw or ''}".strip()
    try:
        candidates = search_yandex_tracks(client, query, limit=10) if query else []
        best, method = choose_track_candidate(
            artist_raw=item.artist_raw,
            title_raw=item.title_raw,
            album_raw=item.album_raw,
            isrc=item.isrc,
            duration_ms=item.duration_ms,
            candidates=candidates,
        )
        if best is None:
            status = "ambiguous" if method == "ambiguous" else "not_found"
            _record_attempt(
                db,
                provider="yandex",
                item=item,
                job_id=job_id,
                status=status,
                selection=method,
            )
            return None
        info = get_yandex_lossless_info(client, best.yandex_id)
        stats["available"] += 1
        _record_attempt(
            db,
            provider="yandex",
            item=item,
            job_id=job_id,
            status="available",
            provider_item_id=best.yandex_id,
            selection=method,
        )
        lossless = int(info.codec.casefold().startswith("flac"))
        return ProviderChoice(
            "yandex",
            best,
            method,
            (lossless, 16 if lossless else 0, 44100 if lossless else 0),
        )
    except YandexAcquisitionError as exc:
        stats["failed"] += 1
        _record_attempt(
            db,
            provider="yandex",
            item=item,
            job_id=job_id,
            status="failed",
            error_code=type(exc).__name__,
        )
        return None


def _merge_import_reports(*reports: dict) -> dict:
    result = {"imported": [], "conflicts": [], "rejected": []}
    for report in reports:
        for key in result:
            result[key].extend(report.get(key, []))
    return result


def _phase(db: Session, job: Job, payload: dict, stage: str) -> None:
    payload["phase"] = stage
    payload["current_stage"] = stage
    job.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    job.heartbeat_at = utcnow()
    db.commit()
    db.refresh(job)


def _prepare_workflow(db: Session, job: Job, playlist: Playlist, payload: dict) -> None:
    matching = run_matching(db, job.user_id, playlist_id=playlist.id)
    statement = (
        select(PlaylistItem)
        .where(PlaylistItem.playlist_id == playlist.id)
        .order_by(PlaylistItem.position, PlaylistItem.id)
    )
    items = list(db.scalars(statement))
    if not payload.get("update_quality"):
        items = [
            item
            for item in items
            if item.match is not None and item.match.status == MatchStatus.missing
        ]
    payload["item_ids"] = [item.id for item in items]
    payload["total_positions"] = len(items)
    payload["processed_positions"] = 0
    payload["batch_count"] = (
        len(items) + settings.acquisition_batch_size - 1
    ) // settings.acquisition_batch_size
    payload["matching_before"] = matching.to_dict()
    _phase(db, job, payload, "queued")


def _finish_workflow(db: Session, job: Job, playlist: Playlist, payload: dict) -> dict:
    _phase(db, job, payload, "final_matching")
    matching = run_matching(db, job.user_id, playlist_id=playlist.id)
    payload["matching"] = matching.to_dict()
    payload["phase"] = "completed"
    payload["current_stage"] = "completed"
    payload["free_disk_bytes"] = shutil.disk_usage(
        settings.qobuz_staging_path
    ).free
    notification = send_workflow_completed(
        db,
        user_id=job.user_id,
        workflow_id=job.id,
        ready=matching.ready,
        missing=matching.missing,
        needs_review=matching.needs_review,
    )
    payload["notification"] = notification
    job.status = JobStatus.done
    job.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    job.error = None
    job.finished_at = utcnow()
    job.heartbeat_at = utcnow()
    job.next_run_at = None
    job.lock_owner = None
    db.commit()
    return payload


def process_acquisition_batch(db: Session, job: Job) -> tuple[dict, float | None]:
    """Process exactly one batch and return the delay before the next dispatch."""

    playlist = db.get(Playlist, job.playlist_id)
    if playlist is None or playlist.user_id != job.user_id:
        raise ValueError("Acquisition playlist ownership does not match")
    try:
        payload = json.loads(job.payload or "{}")
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("Acquisition payload is invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("Acquisition payload is invalid")
    if not isinstance(payload.get("item_ids"), list):
        _prepare_workflow(db, job, playlist, payload)
    item_ids = [int(value) for value in payload.get("item_ids", [])]
    cursor = int(payload.get("processed_positions", 0) or 0)
    pending_paths = list(payload.get("pending_import_paths") or [])
    pending_file_ids = [
        int(value) for value in payload.get("pending_file_ids", []) if value is not None
    ]
    if pending_paths or pending_file_ids:
        if not pending_file_ids:
            _phase(db, job, payload, "scanning")
            try:
                scan = scan_library(db, settings.music_library_path)
            except ScanAlreadyRunning as exc:
                raise AcquisitionPause("scan_busy") from exc
            payload["scan"] = {"status": "completed", **scan.to_dict()}
            pending_file_ids = list(
                db.scalars(select(File.id).where(File.path.in_(pending_paths)))
            )
            payload["pending_file_ids"] = pending_file_ids
        _phase(db, job, payload, "drive_upload")
        storage = replicate_imported_files(
            db,
            {"imported": pending_paths, "file_ids": pending_file_ids},
        )
        payload["storage"] = storage
        if storage.get("status") == "degraded":
            raise AcquisitionPause("storage_degraded")
        payload["drive_uploaded_files"] = int(
            payload.get("drive_uploaded_files", 0)
        ) + int(storage.get("uploaded", 0) or 0)
        payload["local_evicted_files"] = int(
            payload.get("local_evicted_files", 0)
        ) + int(storage.get("evicted", 0) or 0)
        cursor += int(payload.pop("pending_batch_positions", 0) or 0)
        payload["processed_positions"] = cursor
        payload.pop("pending_import_paths", None)
        payload.pop("pending_file_ids", None)
        payload["free_disk_bytes"] = shutil.disk_usage(
            settings.qobuz_staging_path
        ).free
        if cursor >= len(item_ids):
            return _finish_workflow(db, job, playlist, payload), None
        job.status = JobStatus.pending
        job.lock_owner = None
        job.next_run_at = utcnow()
        _phase(db, job, payload, "batch_pause")
        return payload, 0.0
    if cursor >= len(item_ids):
        return _finish_workflow(db, job, playlist, payload), None
    if job.pause_requested_at is not None or job.paused_at is not None:
        raise AcquisitionPause("manual")
    free_bytes = shutil.disk_usage(settings.qobuz_staging_path).free
    payload["free_disk_bytes"] = free_bytes
    if free_bytes < settings.qobuz_min_free_bytes:
        raise AcquisitionPause("disk_guard")

    batch_ids = item_ids[cursor : cursor + settings.acquisition_batch_size]
    items_by_id = {
        item.id: item
        for item in db.scalars(
            select(PlaylistItem).where(PlaylistItem.id.in_(batch_ids))
        )
    }
    items = [items_by_id[item_id] for item_id in batch_ids if item_id in items_by_id]
    payload["current_batch"] = (cursor // settings.acquisition_batch_size) + 1
    payload["batch_state"] = "provider_check"
    _phase(db, job, payload, "provider_check")

    try:
        qobuz_client = create_qobuz_client(db)
    except QobuzServiceError:
        qobuz_client = None
    try:
        yandex_client = create_yandex_acquisition_client(db)
    except YandexAcquisitionError:
        yandex_client = None

    qobuz_files: list[Path] = []
    yandex_files: list[Path] = []
    qobuz_entries: list[dict[str, Any]] = []
    yandex_entries: list[dict[str, Any]] = []
    recent_items: list[dict[str, Any]] = []
    update_quality = bool(payload.get("update_quality"))

    for item in items:
        if _stored_by_workflow(db, item, job.id):
            recent_items.append(
                {
                    "item_id": item.id,
                    "status": "already_stored",
                }
            )
            continue
        current_quality = _current_quality(db, item)
        choices = [
            choice
            for choice in (
                _probe_qobuz(
                    db,
                    client=qobuz_client,
                    item=item,
                    job_id=job.id,
                    payload=payload,
                ),
                _probe_yandex(
                    db,
                    client=yandex_client,
                    item=item,
                    job_id=job.id,
                    payload=payload,
                ),
            )
            if choice is not None
        ]
        entry: dict[str, Any] = {
            "item_id": item.id,
            "artist": item.artist_raw,
            "title": item.title_raw,
        }
        if not choices:
            entry["status"] = "not_found"
            recent_items.append(entry)
            continue
        choice = max(
            choices,
            key=lambda candidate: (
                candidate.quality_rank,
                int(candidate.provider == "qobuz"),
            ),
        )
        if update_quality and choice.quality_rank <= current_quality:
            entry.update(status="quality_current", selected_provider=choice.provider)
            recent_items.append(entry)
            continue
        _summary(payload, choice.provider)["selected"] += 1
        entry["selected_provider"] = choice.provider
        entry["selection"] = choice.selection
        try:
            if choice.provider == "qobuz":
                files = download_track_to_staging(
                    qobuz_client,
                    choice.candidate.qobuz_id,
                    settings.qobuz_staging_path,
                    settings.qobuz_quality,
                    settings.qobuz_embed_art,
                )
                entry["qobuz_track_id"] = choice.candidate.qobuz_id
                qobuz_files.extend(files)
                qobuz_entries.append(entry)
            else:
                files, quality = download_yandex_track_to_staging(
                    yandex_client,
                    choice.candidate,
                    item,
                )
                entry["yandex_track_id"] = choice.candidate.yandex_id
                entry.update(quality)
                yandex_files.extend(files)
                yandex_entries.append(entry)
            entry["files"] = [str(path) for path in files]
            entry["status"] = "downloaded" if files else "failed"
            payload["downloaded_files"] = int(payload.get("downloaded_files", 0)) + len(files)
        except (QobuzServiceError, YandexAcquisitionError) as exc:
            entry["status"] = "failed"
            entry["error"] = type(exc).__name__
            _summary(payload, choice.provider)["failed"] += 1
            _record_attempt(
                db,
                provider=choice.provider,
                item=item,
                job_id=job.id,
                status="failed",
                provider_item_id=(
                    choice.candidate.qobuz_id
                    if choice.provider == "qobuz"
                    else choice.candidate.yandex_id
                ),
                selection=choice.selection,
                error_code=type(exc).__name__,
            )
        recent_items.append(entry)
        delay = max(
            settings.qobuz_request_delay_seconds,
            settings.yandex_request_delay_seconds,
        )
        if delay:
            time.sleep(delay)

    pause_started = time.monotonic()
    payload["batch_state"] = "draining"
    payload["items"] = recent_items
    _phase(db, job, payload, "importing")

    empty_report = {"imported": [], "conflicts": [], "rejected": []}
    qobuz_report = (
        import_files_to_library(
            qobuz_files,
            settings.qobuz_staging_path,
            settings.music_library_path,
        )
        if qobuz_files
        else empty_report
    )
    yandex_report = (
        import_files_to_library(
            yandex_files,
            settings.yandex_staging_path,
            settings.music_library_path,
        )
        if yandex_files
        else empty_report
    )
    import_report = _merge_import_reports(qobuz_report, yandex_report)
    payload["import"] = {
        key: len(import_report[key])
        for key in ("imported", "conflicts", "rejected")
    }

    qobuz_summary = {"items": qobuz_entries}
    yandex_summary = {"items": yandex_entries}
    if qobuz_entries:
        mark_downloads_stored(
            qobuz_summary,
            qobuz_report,
            settings.qobuz_staging_path,
            settings.music_library_path,
        )
        record_qobuz_download_attempts(
            db,
            qobuz_summary,
            {item.id: item for item in items},
            job.id,
        )
    if yandex_entries:
        mark_downloads_stored(
            yandex_summary,
            yandex_report,
            settings.yandex_staging_path,
            settings.music_library_path,
        )
        record_yandex_download_attempts(
            db,
            yandex_summary,
            {item.id: item for item in items},
            job.id,
        )

    if import_report["imported"]:
        payload["pending_import_paths"] = list(import_report["imported"])
        payload["pending_batch_positions"] = len(items)
        _phase(db, job, payload, "scanning")
        try:
            scan = scan_library(db, settings.music_library_path)
        except ScanAlreadyRunning as exc:
            raise AcquisitionPause("scan_busy") from exc
        payload["scan"] = {"status": "completed", **scan.to_dict()}
        file_ids = list(
            db.scalars(
                select(File.id).where(File.path.in_(import_report["imported"]))
            )
        )
        payload["pending_file_ids"] = file_ids
        import_report["file_ids"] = file_ids
        _phase(db, job, payload, "drive_upload")
        storage = replicate_imported_files(db, import_report)
        payload["storage"] = storage
        if storage.get("status") == "degraded":
            raise AcquisitionPause("storage_degraded")
        payload["drive_uploaded_files"] = int(
            payload.get("drive_uploaded_files", 0)
        ) + int(storage.get("uploaded", 0) or 0)
        payload["local_evicted_files"] = int(
            payload.get("local_evicted_files", 0)
        ) + int(storage.get("evicted", 0) or 0)
        payload.pop("pending_import_paths", None)
        payload.pop("pending_file_ids", None)
        payload.pop("pending_batch_positions", None)
    else:
        payload["scan"] = {"status": "skipped", "reason": "no_new_files"}
        payload["storage"] = {"status": "skipped", "reason": "no_new_files"}

    payload.pop("_batch_retry_count", None)
    payload["processed_positions"] = cursor + len(items)
    payload["free_disk_bytes"] = shutil.disk_usage(
        settings.qobuz_staging_path
    ).free
    if payload["processed_positions"] >= len(item_ids):
        return _finish_workflow(db, job, playlist, payload), None

    db.refresh(job)
    if job.pause_requested_at is not None:
        _phase(db, job, payload, "batch_complete")
        raise AcquisitionPause("manual")

    required_pause = max(
        settings.qobuz_batch_delay_seconds,
        settings.yandex_batch_delay_seconds,
    )
    remaining = max(0.0, required_pause - (time.monotonic() - pause_started))
    payload["phase"] = "batch_pause"
    payload["current_stage"] = "batch_pause"
    payload["batch_state"] = "paused"
    payload["batch_pause_seconds"] = round(remaining, 3)
    now = utcnow()
    job.status = JobStatus.pending
    job.payload = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    job.error = None
    job.lock_owner = None
    job.heartbeat_at = now
    job.next_run_at = now + timedelta(seconds=remaining)
    db.commit()
    return payload, remaining

