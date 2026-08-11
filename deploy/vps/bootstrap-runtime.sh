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
ensure_key "${SECRET_ROOT}/auth.key" root:root

# Compose always mounts the dedicated Google Login client-secret file.  It is
# intentionally empty until the operator creates the separate OIDC client in
# Google Cloud Console; bootstrap must never invent a value that looks valid.
if [[ ! -e "${SECRET_ROOT}/google-login-client-secret" ]]; then
  install -m 0400 /dev/null "${SECRET_ROOT}/google-login-client-secret"
fi
chown root:root "${SECRET_ROOT}/google-login-client-secret"
chmod 0400 "${SECRET_ROOT}/google-login-client-secret"

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
    printf 'RELEASE_SHA=bootstrap\n'
    printf 'PUBLIC_ORIGIN=https://audiofeel.su\n'
    printf 'AUTH_COOKIE_SECURE=true\n'
    printf 'AUTH_COOKIE_MAX_AGE_SECONDS=2592000\n'
    printf 'AUTH_SESSION_IDLE_SECONDS=604800\n'
    printf 'AUTH_SESSION_TOUCH_INTERVAL_SECONDS=300\n'
    printf 'AUTH_RECOVERY_MAX_AGE_SECONDS=900\n'
    printf 'AUTH_RECOVERY_IDLE_SECONDS=300\n'
    printf 'AUTH_KEY_HOST_FILE=%s\n' "${SECRET_ROOT}/auth.key"
    printf 'GOOGLE_LOGIN_CLIENT_ID=\n'
    printf 'GOOGLE_LOGIN_CLIENT_SECRET_HOST_FILE=%s\n' \
      "${SECRET_ROOT}/google-login-client-secret"
    printf 'GOOGLE_LOGIN_REDIRECT_URI=https://audiofeel.su/api/auth/google/callback\n'
    printf 'GOOGLE_LOGIN_STATE_TTL_SECONDS=600\n'
    printf 'GOOGLE_LOGIN_CLOCK_SKEW_SECONDS=60\n'
    printf 'GOOGLE_LOGIN_MAX_TOKEN_AGE_SECONDS=600\n'
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
ensure_env_default RELEASE_SHA bootstrap
ensure_env_default PUBLIC_ORIGIN https://audiofeel.su
ensure_env_default AUTH_COOKIE_SECURE true
ensure_env_default AUTH_COOKIE_MAX_AGE_SECONDS 2592000
ensure_env_default AUTH_SESSION_IDLE_SECONDS 604800
ensure_env_default AUTH_SESSION_TOUCH_INTERVAL_SECONDS 300
ensure_env_default AUTH_RECOVERY_MAX_AGE_SECONDS 900
ensure_env_default AUTH_RECOVERY_IDLE_SECONDS 300
ensure_env_default AUTH_KEY_HOST_FILE "${SECRET_ROOT}/auth.key"
ensure_env_default GOOGLE_LOGIN_CLIENT_ID ""
ensure_env_default GOOGLE_LOGIN_CLIENT_SECRET_HOST_FILE \
  "${SECRET_ROOT}/google-login-client-secret"
ensure_env_default GOOGLE_LOGIN_REDIRECT_URI \
  https://audiofeel.su/api/auth/google/callback
ensure_env_default GOOGLE_LOGIN_STATE_TTL_SECONDS 600
ensure_env_default GOOGLE_LOGIN_CLOCK_SKEW_SECONDS 60
ensure_env_default GOOGLE_LOGIN_MAX_TOKEN_AGE_SECONDS 600
chmod 0600 "${ENV_FILE}"

printf 'audiofeel runtime: ready (credentials not displayed)\n'
