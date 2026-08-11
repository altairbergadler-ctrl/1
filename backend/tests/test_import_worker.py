import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import Job, JobScope, JobStatus, Playlist, PlaylistSource, ServiceEnum
from app.services.spotify import SpotifyImportSummary
from app.services.yandex import YandexImportSummary
from app.workers.celery_app import celery
from app.workers.tasks import PlaylistImportAllFailed, import_playlists_task
from tests.helpers import ensure_user


def _create_job_and_source(session_factory, service: ServiceEnum):
    session = session_factory()
    user = ensure_user(session)
    source = PlaylistSource(user_id=user.id, service=service)
    session.add(source)
    session.flush()
    job = Job(
        type="import_playlists",
        source_id=source.id,
        user_id=user.id,
        scope=JobScope.user,
        status=JobStatus.pending,
        payload=json.dumps({"source_id": source.id}),
    )
    session.add(job)
    session.commit()
    result = job.id, source.id
    session.close()
    return result


def test_import_task_is_registered():
    assert "import_playlists" in celery.tasks


def test_spotify_import_task_completes_job(session_factory, monkeypatch):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    calls: list[int] = []
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.import_spotify_playlists",
        lambda _db, source: calls.append(source.id)
        or SpotifyImportSummary(
            discovered=1,
            created=1,
            tracks_imported=2,
        ),
    )

    result = import_playlists_task.run(job_id, source_id)

    check = session_factory()
    job = check.get(Job, job_id)
    assert calls == [source_id]
    assert job.status == JobStatus.done
    assert job.lock_owner is None
    assert job.finished_at is not None
    assert json.loads(job.payload)["service"] == "spotify"
    assert json.loads(job.payload)["import"]["tracks_imported"] == 2
    assert result["result_status"] == "completed"
    check.close()


def test_public_spotify_url_task_imports_then_matches_one_playlist(
    session_factory, monkeypatch
):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    setup = session_factory()
    job = setup.get(Job, job_id)
    url = "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"
    job.payload = json.dumps({"source_id": source_id, "url": url})
    playlist = Playlist(
        source_id=source_id,
        user_id=job.user_id,
        external_id="37i9dQZF1DXcBWIGoYBM5M",
        name="Public playlist",
        snapshot_hash="snapshot-public",
    )
    setup.add(playlist)
    setup.commit()
    playlist_id = playlist.id
    setup.close()
    calls = []
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)

    def import_public_playlist(db, selected_url, source):
        assert source.id == source_id
        calls.append(("import", selected_url))
        return (
            SpotifyImportSummary(discovered=1, created=1),
            db.get(Playlist, playlist_id),
        )

    monkeypatch.setattr(
        "app.workers.tasks.import_spotify_playlist_url",
        import_public_playlist,
    )
    monkeypatch.setattr(
        "app.workers.tasks.run_matching",
        lambda _db, _user_id, *, playlist_id: calls.append(("matching", playlist_id))
        or SimpleNamespace(to_dict=lambda: {"processed": 1}),
    )

    result = import_playlists_task.run(job_id, source_id, None, url)

    assert calls == [("import", url), ("matching", playlist_id)]
    assert result["playlist_id"] == playlist_id
    assert result["matching"] == {"processed": 1}
    check = session_factory()
    assert check.get(Job, job_id).status == JobStatus.done
    check.close()


def test_public_spotify_url_task_rejects_a_missing_task_url(
    session_factory, monkeypatch
):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    setup = session_factory()
    job = setup.get(Job, job_id)
    job.payload = json.dumps(
        {
            "source_id": source_id,
            "url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M",
        }
    )
    setup.commit()
    setup.close()
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    imported = []
    monkeypatch.setattr(
        "app.workers.tasks.import_spotify_playlists",
        lambda *_args, **_kwargs: imported.append(True),
    )

    with pytest.raises(ValueError, match="URL does not match"):
        import_playlists_task.run(job_id, source_id)

    assert imported == []
    check = session_factory()
    assert check.get(Job, job_id).status == JobStatus.pending
    assert "ValueError" in check.get(Job, job_id).error
    check.close()


def test_provider_import_runs_inside_source_execution_lock(
    session_factory, monkeypatch
):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    sentinel = object()
    events: list[tuple[str, object]] = []

    def acquire(_db, selected_source_id):
        events.append(("acquire", selected_source_id))
        return sentinel

    def provider(_db, source):
        events.append(("provider", source.id))
        return SpotifyImportSummary(discovered=1, unchanged=1)

    def release(connection, selected_source_id):
        events.append(("release", (connection, selected_source_id)))

    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr("app.workers.tasks._acquire_import_source_lock", acquire)
    monkeypatch.setattr("app.workers.tasks._release_import_source_lock", release)
    monkeypatch.setattr("app.workers.tasks.import_spotify_playlists", provider)

    result = import_playlists_task.run(job_id, source_id)

    assert result["result_status"] == "completed"
    assert events == [
        ("acquire", source_id),
        ("provider", source_id),
        ("release", (sentinel, source_id)),
    ]


def test_revoked_job_does_not_write_after_waiting_for_source_lock(
    session_factory, monkeypatch
):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    sentinel = object()
    provider_called = False
    released: list[tuple[object, int]] = []

    def revoke_while_waiting(worker_db, _source_id):
        job = worker_db.get(Job, job_id)
        job.status = JobStatus.failed
        job.error = "replaced after stale heartbeat"
        job.finished_at = None
        job.lock_owner = None
        worker_db.commit()
        return sentinel

    def provider(*_args):
        nonlocal provider_called
        provider_called = True
        return SpotifyImportSummary()

    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks._acquire_import_source_lock", revoke_while_waiting
    )
    monkeypatch.setattr(
        "app.workers.tasks._release_import_source_lock",
        lambda connection, selected_source_id: released.append(
            (connection, selected_source_id)
        ),
    )
    monkeypatch.setattr("app.workers.tasks.import_spotify_playlists", provider)

    result = import_playlists_task.run(job_id, source_id)

    check = session_factory()
    job = check.get(Job, job_id)
    assert result == {"status": "lease_lost", "job_id": job_id}
    assert provider_called is False
    assert released == [(sentinel, source_id)]
    assert job.status == JobStatus.failed
    assert job.error == "replaced after stale heartbeat"
    check.close()


def test_job_source_id_cannot_be_substituted(session_factory, monkeypatch):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.import_spotify_playlists",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = import_playlists_task.run(job_id, source_id + 1000)

    check = session_factory()
    job = check.get(Job, job_id)
    assert result == {"status": "already_running", "job_id": job_id}
    assert job.status == JobStatus.pending
    assert job.lock_owner is None
    check.close()


def test_yandex_refresh_task_targets_one_playlist(session_factory, monkeypatch):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.yandex)
    setup = session_factory()
    job = setup.get(Job, job_id)
    playlist = Playlist(
        source_id=source_id,
        user_id=job.user_id,
        external_id="42:7",
        name="Refresh me",
        snapshot_hash="old",
    )
    setup.add(playlist)
    setup.commit()
    playlist_id = playlist.id
    setup.close()
    calls: list[int] = []
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.refresh_yandex_playlist",
        lambda _db, selected: calls.append(selected.id)
        or YandexImportSummary(updated=1),
    )

    result = import_playlists_task.run(job_id, source_id, playlist_id)

    check = session_factory()
    job = check.scalar(select(Job).where(Job.id == job_id))
    assert calls == [playlist_id]
    assert job.status == JobStatus.done
    assert result["playlist_id"] == playlist_id
    assert result["import"]["updated"] == 1
    check.close()


def test_terminal_import_job_is_not_delivered_twice(session_factory, monkeypatch):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.spotify)
    session = session_factory()
    job = session.get(Job, job_id)
    job.status = JobStatus.done
    job.payload = '{"result_status":"completed"}'
    session.commit()
    session.close()
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.import_spotify_playlists",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")),
    )

    result = import_playlists_task.run(job_id, source_id)

    assert result == {"result_status": "completed"}


def test_import_task_retries_when_every_provider_playlist_fails(
    session_factory, monkeypatch
):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.yandex)
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.import_yandex_playlists",
        lambda _db, _source: YandexImportSummary(
            failed=1,
            errors=["42:7: RuntimeError"],
        ),
    )
    retry_call = {}

    class RetryScheduled(RuntimeError):
        pass

    def schedule_retry(**kwargs):
        retry_call.update(kwargs)
        raise RetryScheduled

    monkeypatch.setattr(import_playlists_task, "retry", schedule_retry)

    with pytest.raises(RetryScheduled):
        import_playlists_task.run(job_id, source_id)

    check = session_factory()
    job = check.get(Job, job_id)
    assert job.status == JobStatus.pending
    assert job.error == (
        "Import attempt 1 failed (PlaylistImportAllFailed); retry scheduled"
    )
    assert job.lock_owner is None
    assert job.finished_at is None
    assert retry_call["countdown"] == 1
    assert type(retry_call["exc"]).__name__ == "PlaylistImportAllFailed"
    check.close()


def test_import_task_fails_safely_after_retry_limit(session_factory, monkeypatch):
    job_id, source_id = _create_job_and_source(session_factory, ServiceEnum.yandex)
    monkeypatch.setattr("app.workers.tasks.SessionLocal", session_factory)
    monkeypatch.setattr(
        "app.workers.tasks.import_yandex_playlists",
        lambda _db, _source: YandexImportSummary(
            failed=1,
            errors=["42:7: RuntimeError"],
        ),
    )

    import_playlists_task.push_request(
        id="final-import-attempt",
        retries=import_playlists_task.max_retries,
    )
    try:
        with pytest.raises(PlaylistImportAllFailed):
            import_playlists_task.run(job_id, source_id)
    finally:
        import_playlists_task.pop_request()

    check = session_factory()
    job = check.get(Job, job_id)
    assert job.status == JobStatus.failed
    assert job.error == "Playlist import failed (PlaylistImportAllFailed)"
    assert job.lock_owner is None
    assert job.finished_at is not None
    check.close()
