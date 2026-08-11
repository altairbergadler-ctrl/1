#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "bootstrap-runtime.sh must run as root" >&2
  exit 1
fi

APP_ROOT=/opt/audiofeel/app
RUNTIME_ROOT=/etc/audiofeel
SECRET_ROOT=${RUNTIME_ROOT}/secrets
LIBRARY_ROOT=/srv/audiofeel/library
STAGING_ROOT=/srv/audiofeel/staging
CACHE_ROOT=/srv/audiofeel/cache
ENV_FILE=${RUNTIME_ROOT}/music-service.env

install -d -m 0750 "${APP_ROOT}" "${RUNTIME_ROOT}" "${LIBRARY_ROOT}"
install -d -m 0700 "${SECRET_ROOT}"
install -d -m 0750 -o 10001 -g 10001 "${STAGING_ROOT}"
install -d -m 0750 "${CACHE_ROOT}"

umask 077

ensure_key() {
  local path=$1
  local owner=$2
  if [[ ! -e "${path}" ]]; then
    openssl rand 32 >"${path}"
  fi
  chown "${owner}" "${path}"
  chmod 0400 "${path}"
}

ensure_key "${SECRET_ROOT}/provider-credentials.key" root:root
ensure_key "${SECRET_ROOT}/qobuz-credentials.key" 10001:10001

if [[ ! -e "${ENV_FILE}" ]]; then
  postgres_password=$(openssl rand -hex 32)
  app_auth_token=$(openssl rand -hex 32)
  qobuz_internal_token=$(openssl rand -hex 32)
  yandex_internal_token=$(openssl rand -hex 32)

  install -m 0600 /dev/null "${ENV_FILE}"
  {
    printf 'MUSIC_SERVICE_ENV_FILE=%s\n' "${ENV_FILE}"
    printf 'DATABASE_URL=postgresql+psycopg2://music:%s@db:5432/music\n' "${postgres_password}"
    printf 'REDIS_URL=redis://redis:6379/0\n'
    printf 'POSTGRES_USER=music\n'
    printf 'POSTGRES_PASSWORD=%s\n' "${postgres_password}"
    printf 'POSTGRES_DB=music\n'
    printf 'APP_AUTH_TOKEN=%s\n' "${app_auth_token}"
    printf 'AUTH_COOKIE_SECURE=true\n'
    printf 'AUTH_COOKIE_MAX_AGE_SECONDS=2592000\n'
    printf 'TZ=Europe/Moscow\n'
    printf 'MUSIC_LIBRARY_PATH=/music/library\n'
    printf 'MUSIC_LIBRARY_HOST_PATH=%s\n' "${LIBRARY_ROOT}"
    printf 'QOBUZ_STAGING_HOST_PATH=%s\n' "${STAGING_ROOT}"
    printf 'STORAGE_CACHE_HOST_PATH=%s\n' "${CACHE_ROOT}"
    printf 'STORAGE_CACHE_PATH=/music/cache\n'
    printf 'STORAGE_CACHE_TTL_SECONDS=86400\n'
    printf 'STORAGE_RECONCILE_INTERVAL_SECONDS=900\n'
    printf 'STORAGE_PRIMARY_BACKEND=google_drive\n'
    printf 'GOOGLE_DRIVE_REDIRECT_URI=https://audiofeel.su/api/storage/google/callback\n'
    printf 'PROVIDER_CREDENTIAL_KEY_HOST_FILE=%s\n' "${SECRET_ROOT}/provider-credentials.key"
    printf 'QOBUZ_CREDENTIAL_KEY_HOST_FILE=%s\n' "${SECRET_ROOT}/qobuz-credentials.key"
    printf 'QOBUZ_INTERNAL_TOKEN=%s\n' "${qobuz_internal_token}"
    printf 'YANDEX_INTERNAL_TOKEN=%s\n' "${yandex_internal_token}"
    printf 'QOBUZ_ENABLED=false\n'
    printf 'YANDEX_DOWNLOAD_ENABLED=false\n'
    printf 'PROVIDER_HEALTH_INTERVAL_SECONDS=1800\n'
    printf 'PROVIDER_HEALTH_STALE_SECONDS=5400\n'
    printf 'PROVIDER_HEALTH_MANUAL_COOLDOWN_SECONDS=60\n'
    printf 'MUSICBRAINZ_ENABLED=false\n'
    printf 'CELERY_TASK_ALWAYS_EAGER=false\n'
    printf 'SPOTIFY_REDIRECT_URI=https://audiofeel.su/api/sources/spotify/callback\n'
    printf 'BACKEND_HOST_PORT=18000\n'
    printf 'FRONTEND_HOST_PORT=18080\n'
  } >"${ENV_FILE}"
  chmod 0600 "${ENV_FILE}"

  unset postgres_password app_auth_token qobuz_internal_token yandex_internal_token
fi

ensure_env_default() {
  local name=$1
  local value=$2
  if ! grep -q "^${name}=" "${ENV_FILE}"; then
    printf '%s=%s\n' "${name}" "${value}" >>"${ENV_FILE}"
  fi
}

ensure_env_default STORAGE_CACHE_HOST_PATH "${CACHE_ROOT}"
ensure_env_default STORAGE_CACHE_PATH /music/cache
ensure_env_default STORAGE_CACHE_TTL_SECONDS 86400
ensure_env_default STORAGE_RECONCILE_INTERVAL_SECONDS 900
ensure_env_default STORAGE_PRIMARY_BACKEND google_drive
ensure_env_default GOOGLE_DRIVE_REDIRECT_URI \
  https://audiofeel.su/api/storage/google/callback
chmod 0600 "${ENV_FILE}"

printf 'audiofeel runtime: ready (credentials not displayed)\n'
