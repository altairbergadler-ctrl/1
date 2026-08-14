import json
import os
from pathlib import Path
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
    assert 'const CACHE_NAME = "audiofeel-v19";' in service_worker
    assert "fetch(request).then" in service_worker
    assert ".catch(() => caches.match(request))" in service_worker


def test_google_drive_guide_maps_current_google_console_fields():
    app_script = (
        Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    ).read_text(encoding="utf-8")

    assert "Google Auth Platform → Audience" in app_script
    assert "Google Auth Platform → Clients" in app_script
    assert "Authorized JavaScript origins" in app_script
    assert "https://audiofeel.su/api/storage/google/callback" in app_script
    assert "Google показывает полный secret только при создании" in app_script
    assert "Additional information" in app_script
    assert "Add client secret" in app_script
    assert "Маска вида •••• или **** не подходит" in app_script
    assert "Сейчас ничего вводить не нужно" in app_script


def test_playlist_page_connects_spotify_before_importing_one_url():
    root = Path(__file__).resolve().parents[2] / "frontend"
    app_script = (root / "app.js").read_text(encoding="utf-8")
    styles = (root / "styles.css").read_text(encoding="utf-8")

    assert 'id="playlist-url-form"' in app_script
    assert "https://open.spotify.com/playlist/..." in app_script
    assert 'api("/api/playlists/import-url"' in app_script
    assert 'api("/api/sources")' in app_script
    assert 'data-action="spotify-connect"' in app_script
    assert 'data-action="spotify-import-all"' in app_script
    assert 'api("/api/playlists/import"' in app_script
    assert "spotify_not_allowed" in app_script
    assert "Development Mode" in app_script
    assert 'api("/api/sources/spotify/connect", { method: "POST" })' in app_script
    assert "window.location.assign(result.authorization_url)" in app_script
    assert "Client ID, Client Secret и пароль вводить в Audiofeel не нужно" in app_script
    assert ".playlist-import-card" in styles


def test_qobuz_progress_has_safe_pause_and_resume_controls():
    app_script = (
        Path(__file__).resolve().parents[2] / "frontend" / "app.js"
    ).read_text(encoding="utf-8")

    assert 'data-action="qobuz-pause"' in app_script
    assert 'data-action="qobuz-resume"' in app_script
    assert "/api/qobuz/downloads/" in app_script
    assert "Пауза после пачки" in app_script


def test_pwa_uses_public_google_registration_and_role_admin():
    root = Path(__file__).resolve().parents[2] / "frontend"
    app_script = (root / "app.js").read_text(encoding="utf-8")
    nginx = (root / "nginx.conf").read_text(encoding="utf-8")

    assert 'href="/api/auth/google/start"' in app_script
    assert 'api("/api/auth/me"' in app_script
    assert 'headers.set("X-CSRF-Token", csrfToken)' in app_script
    assert 'api("/api/auth/logout", { method: "POST" })' in app_script
    assert 'navLink("playlists", "Плейлисты")' in app_script
    assert 'api("/api/admin/users")' in app_script
    assert 'class="user-role-select"' in app_script
    assert 'method: "PATCH"' in app_script
    assert 'новый аккаунт будет зарегистрирован автоматически' in app_script
    assert 'id="user-invite-form"' not in app_script
    assert 'data-action="user-disable"' in app_script
    assert 'data-action="user-revoke-sessions"' in app_script
    assert 'id="recovery-login-form"' in app_script
    assert "localStorage" not in app_script
    assert "sessionStorage" not in app_script
    assert "access_log off;" in nginx


def test_playlist_page_imports_csv_m3u_or_text_without_provider_auth():
    root = Path(__file__).resolve().parents[2] / "frontend"
    app_script = (root / "app.js").read_text(encoding="utf-8")

    assert 'id="playlist-converter-form"' in app_script
    assert ".csv,.m3u,.m3u8,.txt" in app_script
    assert 'api("/api/playlists/import-content"' in app_script
    assert "await waitForJob(imported.matching_job.id" not in app_script
    assert "Автоматическая загрузка поставлена в очередь" in app_script
    assert "Исполнитель — Название трека" in app_script


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


def test_player_page_uses_one_time_memory_only_api_key():
    root = Path(__file__).resolve().parents[2] / "frontend"
    app_script = (root / "app.js").read_text(encoding="utf-8")
    assert 'navLink("players", "Плееры")' in app_script
    assert 'api("/api/player-credentials"' in app_script
    assert "state.playerSecret" in app_script
    assert "localStorage" not in app_script
    assert "sessionStorage" not in app_script
    assert "data-api-key" not in app_script


def test_service_worker_never_caches_opensubsonic_credentials():
    service_worker = (
        Path(__file__).resolve().parents[2] / "frontend" / "service-worker.js"
    ).read_text(encoding="utf-8")
    assert 'url.pathname.startsWith("/rest/")' in service_worker
    assert 'url.pathname === "/rest"' in service_worker
    assert 'url.searchParams.has(name)' in service_worker


def test_opensubsonic_query_secrets_are_excluded_from_access_logs():
    root = Path(__file__).resolve().parents[2]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    nginx = (root / "frontend" / "nginx.conf").read_text(encoding="utf-8")
    caddy = (root / "deploy" / "vps" / "Caddyfile").read_text(encoding="utf-8")
    assert "--no-access-log" in compose
    assert "access_log off;" in nginx
    assert "output discard" in caddy


def test_pwa_push_only_handles_final_workflow_completion():
    root = Path(__file__).resolve().parents[2] / "frontend"
    app_script = (root / "app.js").read_text(encoding="utf-8")
    service_worker = (root / "service-worker.js").read_text(encoding="utf-8")
    manifest = (root / "manifest.webmanifest").read_text(encoding="utf-8")

    assert 'navLink("notifications", "Уведомления")' in app_script
    assert "\nfunction base64UrlToBytes" in app_script
    assert "\nfunction acquisitionProgressPanel" in app_script
    assert 'Notification.requestPermission()' in app_script
    assert 'api("/api/push/subscriptions"' in app_script
    assert 'payload?.type !== "workflow_completed"' in service_worker
    assert 'self.addEventListener("notificationclick"' in service_worker
    assert '"id": "/"' in manifest


def test_audiofeel_v1_shell_is_local_and_offline_ready():
    root = Path(__file__).resolve().parents[2] / "frontend"
    index = (root / "index.html").read_text(encoding="utf-8")
    app_script = (root / "app.js").read_text(encoding="utf-8")
    styles = (root / "redesign.css").read_text(encoding="utf-8")
    service_worker = (root / "service-worker.js").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")

    assert '<link rel="stylesheet" href="/redesign.css">' in index
    assert 'const APP_VERSION = "1.0";' in app_script
    assert 'class="app app-shell"' in app_script
    assert "fonts.googleapis.com" not in index
    assert 'url("/fonts/inter-cyrillic.woff2")' in styles
    assert '"/redesign.css"' in service_worker
    assert '"/fonts/literata-latin.woff2"' in service_worker
    assert "COPY index.html app.js styles.css redesign.css" in dockerfile
    assert "COPY fonts /usr/share/nginx/html/fonts" in dockerfile
