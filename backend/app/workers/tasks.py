from app.workers.celery_app import celery


@celery.task(name="scan_library")
def scan_library_task():
    """TODO (Этап 2): вызвать services.scanner.scan_library()."""
    return {"status": "not_implemented"}


@celery.task(name="import_playlists")
def import_playlists_task(source_id: int):
    """TODO (Этап 3): вызвать services.spotify / services.yandex."""
    return {"status": "not_implemented", "source_id": source_id}


@celery.task(name="run_matching")
def run_matching_task(playlist_id: int | None = None):
    """TODO (Этап 4): вызвать services.matcher.run()."""
    return {"status": "not_implemented", "playlist_id": playlist_id}
