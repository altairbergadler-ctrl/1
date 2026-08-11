import json

from sqlalchemy import select

from app.models import Job, JobStatus
from app.services.scanner import ScanSummary
from app.workers.celery_app import celery
from app.workers.tasks import scan_library_task


def test_scan_task_is_registered():
    assert "scan_library" in celery.tasks


def test_qobuz_download_task_is_registered():
    assert "qobuz_download" in celery.tasks


def test_yandex_download_task_is_registered():
    assert "yandex_download" in celery.tasks


def test_google_drive_tasks_are_registered():
    assert "storage_health_check" in celery.tasks
    assert "storage_migration" in celery.tasks


def test_scan_task_updates_job_lifecycle(session_factory, monkeypatch, tmp_path):
    session = session_factory()
    job = Job(type="scan_library", status=JobStatus.pending, payload="{}")
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)

    def fake_scan(_db, _path, progress_callback=None, lock_acquired_callback=None):
        lock_acquired_callback()
        summary = ScanSummary(discovered=3, added=3, album_ids=[1, 2])
        progress_callback(summary)
        return summary

    monkeypatch.setattr("app.workers.tasks.scan_library", fake_scan)
    monkeypatch.setattr("app.workers.tasks.settings.musicbrainz_enabled", False)
    monkeypatch.setattr("app.workers.tasks.settings.music_library_path", str(tmp_path))

    result = scan_library_task.run(job_id)

    check = session_factory()
    finished = check.scalar(select(Job).where(Job.id == job_id))
    assert finished.status == JobStatus.done
    assert finished.finished_at is not None
    assert finished.heartbeat_at is not None
    assert finished.lock_owner is None
    assert json.loads(finished.payload)["phase"] == "completed"
    assert json.loads(finished.payload)["scan"]["added"] == 3
    assert result["musicbrainz"]["status"] == "disabled"
    check.close()


def test_scan_task_stops_when_its_lease_is_revoked(
    session_factory, monkeypatch, tmp_path
):
    session = session_factory()
    job = Job(type="scan_library", status=JobStatus.pending, payload="{}")
    session.add(job)
    session.commit()
    job_id = job.id
    session.close()

    def revoke_during_scan(
        scan_db, _path, progress_callback=None, lock_acquired_callback=None
    ):
        lock_acquired_callback()
        revoked = scan_db.get(Job, job_id)
        revoked.status = JobStatus.failed
        revoked.error = "lease revoked by replacement"
        scan_db.commit()
        progress_callback(ScanSummary(discovered=1, added=1))
        raise AssertionError("lost worker must stop at the progress callback")

    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks.scan_library", revoke_during_scan)
    monkeypatch.setattr("app.workers.tasks.settings.musicbrainz_enabled", False)
    monkeypatch.setattr("app.workers.tasks.settings.music_library_path", str(tmp_path))

    result = scan_library_task.run(job_id)

    check = session_factory()
    failed = check.get(Job, job_id)
    assert result == {"status": "lease_lost", "job_id": job_id}
    assert failed.status == JobStatus.failed
    assert failed.error == "lease revoked by replacement"
    check.close()
