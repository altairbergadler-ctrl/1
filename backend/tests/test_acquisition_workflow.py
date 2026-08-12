import json
from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import func, select
from app.models import Job, JobScope, JobStatus, Match, MatchStatus, Playlist, PlaylistItem, PlaylistSource, ServiceEnum, utcnow
from app.services.acquisition import process_acquisition_batch
from app.services.acquisition_queue import queue_acquisition_job
from app.workers.tasks import acquisition_dispatch_task
from tests.helpers import ensure_user


def _playlist(db, *, email="queue@example.test", count=1, status=MatchStatus.missing):
    user = ensure_user(db, email=email, bootstrap=False)
    source = db.scalar(
        select(PlaylistSource).where(
            PlaylistSource.user_id == user.id,
            PlaylistSource.service == ServiceEnum.manual,
        )
    )
    if source is None:
        source = PlaylistSource(user_id=user.id, service=ServiceEnum.manual)
        db.add(source)
        db.flush()
    playlist_number = db.scalar(
        select(func.count(Playlist.id)).where(Playlist.user_id == user.id)
    )
    playlist = Playlist(source_id=source.id, user_id=user.id, external_id=f"manual:{email}:{playlist_number}", name="Queue", snapshot_hash=f"{email}:{playlist_number}")
    db.add(playlist)
    db.flush()
    for position in range(count):
        item = PlaylistItem(playlist_id=playlist.id, position=position, artist_raw="Artist", title_raw=f"Track {position}", artist_norm="artist", title_norm=f"track {position}")
        db.add(item)
        db.flush()
        db.add(Match(playlist_item_id=item.id, status=status))
    db.commit()
    return user, playlist


def _matching(count, *, ready=0, missing=0):
    return SimpleNamespace(to_dict=lambda: {"processed": count}, ready=ready, missing=missing, needs_review=0)


def test_queue_is_idempotent_and_preserves_quality_mode(db, monkeypatch):
    user, playlist = _playlist(db)
    monkeypatch.setattr("app.services.acquisition_queue.kick_acquisition_dispatcher", lambda **_: None)
    first = queue_acquisition_job(db, user_id=user.id, playlist_id=playlist.id, update_quality=True)
    second = queue_acquisition_job(db, user_id=user.id, playlist_id=playlist.id, update_quality=False)
    assert first.id == second.id
    assert json.loads(first.payload)["update_quality"] is True
    assert first.status == JobStatus.pending


def test_one_queue_turn_processes_25_positions_and_drains_before_requeue(db, tmp_path, monkeypatch):
    user, playlist = _playlist(db, count=26)
    monkeypatch.setattr("app.services.acquisition_queue.kick_acquisition_dispatcher", lambda **_: None)
    job = queue_acquisition_job(db, user_id=user.id, playlist_id=playlist.id)
    job.status = JobStatus.running
    db.commit()
    monkeypatch.setattr("app.services.acquisition.run_matching", lambda *_args, **_kwargs: _matching(26, missing=26))
    monkeypatch.setattr("app.services.acquisition.settings.qobuz_staging_path", str(tmp_path))
    monkeypatch.setattr("app.services.acquisition.settings.acquisition_batch_size", 25)
    monkeypatch.setattr("app.services.acquisition.settings.qobuz_batch_delay_seconds", 0)
    monkeypatch.setattr("app.services.acquisition.settings.yandex_batch_delay_seconds", 0)
    monkeypatch.setattr("app.services.acquisition.settings.qobuz_request_delay_seconds", 0)
    monkeypatch.setattr("app.services.acquisition.settings.yandex_request_delay_seconds", 0)
    notifications = []
    monkeypatch.setattr("app.services.acquisition.send_workflow_completed", lambda *_args, **_kwargs: notifications.append(_kwargs) or {"sent": 1})
    payload, delay = process_acquisition_batch(db, job)
    assert payload["processed_positions"] == 25
    assert payload["total_positions"] == 26
    assert payload["downloaded_files"] == 0
    assert payload["current_stage"] == "batch_pause"
    assert delay == 0
    assert job.status == JobStatus.pending
    assert job.next_run_at is not None
    assert notifications == []
    job.status = JobStatus.running
    db.commit()
    payload, delay = process_acquisition_batch(db, job)
    assert delay is None
    assert job.status == JobStatus.done
    assert len(notifications) == 1


def test_default_mode_skips_ready_but_quality_mode_checks_it(db, tmp_path, monkeypatch):
    user, playlist = _playlist(db, email="quality@example.test", status=MatchStatus.ready)
    monkeypatch.setattr("app.services.acquisition_queue.kick_acquisition_dispatcher", lambda **_: None)
    monkeypatch.setattr("app.services.acquisition.run_matching", lambda *_args, **_kwargs: _matching(1, ready=1))
    monkeypatch.setattr("app.services.acquisition.settings.qobuz_staging_path", str(tmp_path))
    monkeypatch.setattr("app.services.acquisition.settings.qobuz_request_delay_seconds", 0)
    monkeypatch.setattr("app.services.acquisition.settings.yandex_request_delay_seconds", 0)
    default = queue_acquisition_job(db, user_id=user.id, playlist_id=playlist.id)
    default.status = JobStatus.running
    db.commit()
    default_payload, _ = process_acquisition_batch(db, default)
    assert default_payload["total_positions"] == 0
    quality = Job(type="acquisition_workflow", playlist_id=playlist.id, user_id=user.id, scope=JobScope.user, status=JobStatus.running, payload=json.dumps({"update_quality": True}))
    db.add(quality)
    db.commit()
    quality_payload, _ = process_acquisition_batch(db, quality)
    assert quality_payload["total_positions"] == 1
    assert quality_payload["providers"]["qobuz"]["checked"] == 1
    assert quality_payload["providers"]["yandex"]["checked"] == 1



def test_dispatcher_rotates_users_even_when_one_user_has_many_playlists(
    session_factory, monkeypatch
):
    setup = session_factory()
    user_a, playlist_a1 = _playlist(setup, email="fair-a@example.test", count=0)
    _, playlist_a2 = _playlist(setup, email="fair-a@example.test", count=0)
    user_b, playlist_b = _playlist(setup, email="fair-b@example.test", count=0)
    monkeypatch.setattr("app.services.acquisition_queue.kick_acquisition_dispatcher", lambda **_: None)
    jobs = [
        queue_acquisition_job(setup, user_id=user_a.id, playlist_id=playlist_a1.id),
        queue_acquisition_job(setup, user_id=user_a.id, playlist_id=playlist_a2.id),
        queue_acquisition_job(setup, user_id=user_b.id, playlist_id=playlist_b.id),
    ]
    baseline = utcnow() - timedelta(minutes=10)
    jobs[0].heartbeat_at = baseline
    jobs[1].heartbeat_at = baseline + timedelta(seconds=1)
    jobs[2].heartbeat_at = baseline + timedelta(seconds=2)
    setup.commit()
    setup.close()

    dispatched = []
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks.settings.acquisition_enabled", True)
    monkeypatch.setattr(
        "app.workers.tasks.acquisition_batch_task.apply_async",
        lambda args, queue: dispatched.append(args[0]),
    )
    acquisition_dispatch_task.run()
    middle = session_factory()
    first = middle.get(Job, dispatched[0])
    first.status = JobStatus.pending
    first.next_run_at = utcnow()
    middle.commit()
    middle.close()
    acquisition_dispatch_task.run()

    assert dispatched == [jobs[0].id, jobs[2].id]


def test_dispatcher_recovers_stale_running_lease(session_factory, monkeypatch):
    setup = session_factory()
    user, playlist = _playlist(setup, email="stale@example.test", count=0)
    monkeypatch.setattr(
        "app.services.acquisition_queue.kick_acquisition_dispatcher",
        lambda **_: None,
    )
    job = queue_acquisition_job(setup, user_id=user.id, playlist_id=playlist.id)
    job.status = JobStatus.running
    job.lock_owner = "lost-task"
    job.heartbeat_at = utcnow() - timedelta(hours=1)
    setup.commit()
    job_id = job.id
    setup.close()

    dispatched = []
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks.settings.acquisition_enabled", True)
    monkeypatch.setattr(
        "app.workers.tasks.settings.acquisition_job_stale_seconds",
        300,
    )
    monkeypatch.setattr(
        "app.workers.tasks.acquisition_batch_task.apply_async",
        lambda args, queue: dispatched.append(args[0]),
    )
    acquisition_dispatch_task.run()

    verify = session_factory()

    recovered = verify.get(Job, job_id)
    assert dispatched == [job_id]
    assert recovered.status == JobStatus.running
    assert recovered.lock_owner is None
    verify.close()
