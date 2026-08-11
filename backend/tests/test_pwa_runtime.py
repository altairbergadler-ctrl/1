import json
import os
from urllib.request import urlopen

import pytest


PWA_ACCEPTANCE_BASE_URL = os.getenv("PWA_ACCEPTANCE_BASE_URL", "").rstrip("/")


@pytest.mark.skipif(
    not PWA_ACCEPTANCE_BASE_URL,
    reason="set PWA_ACCEPTANCE_BASE_URL to run the live PWA contract check",
)
def test_webmanifest_has_pwa_media_type_and_required_fields():
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/manifest.webmanifest",
        timeout=10,
    ) as response:
        media_type = response.headers.get_content_type()
        manifest = json.load(response)

    assert media_type == "application/manifest+json"
    assert manifest["name"]
    assert manifest["short_name"]
    assert manifest["display"] == "standalone"
    assert manifest["start_url"].startswith("/")
    assert manifest["icons"]


@pytest.mark.skipif(
    not PWA_ACCEPTANCE_BASE_URL,
    reason="set PWA_ACCEPTANCE_BASE_URL to run the live PWA contract check",
)
def test_service_worker_refreshes_app_shell_before_using_cached_copy():
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/service-worker.js",
        timeout=10,
    ) as response:
        cache_control = response.headers.get("Cache-Control", "")
        service_worker = response.read().decode("utf-8")

    assert "no-cache" in cache_control
    assert 'const CACHE_NAME = "lossless-archive-v8";' in service_worker
    assert "fetch(request).then" in service_worker
    assert ".catch(() => caches.match(request))" in service_worker


@pytest.mark.skipif(
    not PWA_ACCEPTANCE_BASE_URL,
    reason="set PWA_ACCEPTANCE_BASE_URL to run the live PWA contract check",
)
def test_frontend_retries_transient_job_poll_failures():
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/app.js",
        timeout=10,
    ) as response:
        app_script = response.read().decode("utf-8")

    assert "consecutiveFetchFailures" in app_script
    assert "exception instanceof TypeError" in app_script
    assert "Задание продолжает работу, переподключаемся" in app_script
    assert "consecutiveFetchFailures >= 12" in app_script


@pytest.mark.skipif(
    not PWA_ACCEPTANCE_BASE_URL,
    reason="set PWA_ACCEPTANCE_BASE_URL to run the live PWA contract check",
)
def test_playlist_renders_persisted_qobuz_track_progress():
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/app.js",
        timeout=10,
    ) as response:
        app_script = response.read().decode("utf-8")
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/styles.css",
        timeout=10,
    ) as response:
        styles = response.read().decode("utf-8")

    assert "/api/qobuz/download-status/${playlistId}" in app_script
    assert "/api/qobuz/download-eligibility/${playlistId}" in app_script
    assert "qobuzProgressPanel" in app_script
    assert "data-qobuz-item-status" in app_script
    assert "В хранилище" in app_script
    assert "Пачка ${currentBatch} из ${batchCount}" in app_script
    assert ".qobuz-progress-card" in styles
    assert "включён постоянно" in app_script
    assert "qobuz-connect" not in app_script
    assert "async function qobuzConnect" not in app_script


@pytest.mark.skipif(
    not PWA_ACCEPTANCE_BASE_URL,
    reason="set PWA_ACCEPTANCE_BASE_URL to run the live PWA contract check",
)
def test_playlist_renders_persisted_yandex_track_progress_and_quality():
    with urlopen(  # noqa: S310 - opt-in acceptance URL is supplied by the operator
        f"{PWA_ACCEPTANCE_BASE_URL}/app.js",
        timeout=10,
    ) as response:
        app_script = response.read().decode("utf-8")

    assert "/api/yandex-download/status" in app_script
    assert "/api/yandex-download/download-status/${playlistId}" in app_script
    assert "/api/yandex-download/download-eligibility/${playlistId}" in app_script
    assert "/api/yandex-download/fetch-missing" in app_script
    assert "yandexProgressPanel" in app_script
    assert "data-yandex-item-status" in app_script
    assert "bitrate_kbps" in app_script
    assert "FLAC предпочтительно, AAC/MP3 fallback" in app_script
    assert "лучший доступный AAC/MP3 без перекодирования" in app_script
    assert "entry.error_detail" in app_script
