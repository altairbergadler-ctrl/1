"""Isolated qobuz-dl adapter with a private JSON control API.

This process receives only an encrypted Qobuz envelope, its dedicated key, an
internal control token and a staging mount.  It has no database, Redis, Docker
socket or library mount.
Outbound HTTPS is forced through the allowlisting CONNECT proxy from Compose.
"""

from __future__ import annotations

import hmac
import base64
import binascii
import hashlib
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from mutagen import File as MutagenFile

_STAGING = Path(os.environ.get("QOBUZ_STAGING_PATH", "/music/staging")).resolve()
_INTERNAL_TOKEN = os.environ.get("QOBUZ_INTERNAL_TOKEN", "").strip()
_CREDENTIAL_KEY_FILE = os.environ.get(
    "QOBUZ_CREDENTIAL_KEY_FILE", "/run/secrets/qobuz_credential_key"
).strip()
_CONNECT_TIMEOUT = float(os.environ.get("QOBUZ_CONNECT_TIMEOUT_SECONDS", "10"))
_READ_TIMEOUT = float(os.environ.get("QOBUZ_READ_TIMEOUT_SECONDS", "120"))
_MAX_FILE_BYTES = int(os.environ.get("QOBUZ_MAX_FILE_BYTES", str(4 * 1024**3)))
_BUNDLE_TTL_SECONDS = int(os.environ.get("QOBUZ_BUNDLE_TTL_SECONDS", str(7 * 86400)))
_ALLOWED_HOSTS = frozenset(
    {
        "play.qobuz.com",
        "open.qobuz.com",
        "www.qobuz.com",
        "static.qobuz.com",
        "streaming-qobuz-std.akamaized.net",
        "streaming-qobuz-sec.akamaized.net",
    }
)
_DOWNLOAD_URL_RE = re.compile(r"^/(album|track)/([A-Za-z0-9]+)/*$")
_AUDIO_EXTENSIONS = frozenset({".flac"})
_bundle_cache: tuple[float, str, list[str]] | None = None
_bundle_lock = threading.Lock()
_download_lock = threading.Lock()


class SidecarError(RuntimeError):
    status = 502


class SidecarConfigurationError(SidecarError):
    status = 503


class SidecarAuthError(SidecarError):
    status = 400


class SidecarBusyError(SidecarError):
    status = 409


class SidecarLimitError(SidecarError):
    status = 413


class SidecarRateLimitedError(SidecarError):
    status = 429


def _validate_https_url(url: str) -> str:
    parsed = urlparse(str(url))
    hostname = (parsed.hostname or "").casefold()
    if parsed.scheme != "https" or hostname not in _ALLOWED_HOSTS:
        raise SidecarError("Outbound URL is outside the Qobuz allowlist")
    if parsed.port not in (None, 443):
        raise SidecarError("Outbound URL uses a blocked port")
    return hostname


class HardenedSession(requests.Session):
    """Requests session with mandatory allowlist and finite timeouts."""

    def request(self, method, url, **kwargs):
        _validate_https_url(url)
        kwargs.setdefault("timeout", (_CONNECT_TIMEOUT, _READ_TIMEOUT))
        return super().request(method, url, **kwargs)


def _safe_download(url: str, filename: str, _description: str) -> None:
    _validate_https_url(url)
    path = Path(filename)
    path.parent.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    try:
        with HardenedSession().get(url, allow_redirects=True, stream=True) as response:
            for hop in [*response.history, response]:
                _validate_https_url(hop.url)
            response.raise_for_status()
            announced = int(response.headers.get("content-length") or 0)
            if announced > _MAX_FILE_BYTES:
                raise SidecarLimitError("Qobuz file exceeds QOBUZ_MAX_FILE_BYTES")
            with path.open("wb") as output:
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > _MAX_FILE_BYTES:
                        raise SidecarLimitError("Qobuz file exceeds QOBUZ_MAX_FILE_BYTES")
                    output.write(chunk)
            if announced and downloaded != announced:
                raise SidecarError("Qobuz file download was incomplete")
    except Exception:
        path.unlink(missing_ok=True)
        raise


def _modules():
    try:
        from qobuz_dl import bundle, downloader, qopy
        from qobuz_dl.exceptions import (
            AuthenticationError,
            IneligibleError,
            InvalidAppIdError,
            InvalidAppSecretError,
        )
    except ImportError as exc:
        raise SidecarConfigurationError("qobuz-dl is unavailable") from exc
    bundle.Session = HardenedSession
    downloader.tqdm_download = _safe_download
    return {
        "bundle": bundle,
        "downloader": downloader,
        "qopy": qopy,
        "AuthenticationError": AuthenticationError,
        "IneligibleError": IneligibleError,
        "InvalidAppIdError": InvalidAppIdError,
        "InvalidAppSecretError": InvalidAppSecretError,
    }


def _configured() -> bool:
    if len(_INTERNAL_TOKEN) < 32:
        return False
    try:
        _load_credential_key()
    except SidecarConfigurationError:
        return False
    return True


def _load_credential_key() -> bytes:
    try:
        data = Path(_CREDENTIAL_KEY_FILE).read_bytes()
    except OSError as exc:
        raise SidecarConfigurationError("Qobuz credential key is unavailable") from exc
    if len(data) == 32:
        return data
    raw = data.strip()
    try:
        decoded = base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))
    except (binascii.Error, TypeError, ValueError) as exc:
        raise SidecarConfigurationError("Qobuz credential key is invalid") from exc
    if len(decoded) != 32:
        raise SidecarConfigurationError("Qobuz credential key must contain 32 bytes")
    return decoded


def _credential(envelope: Any) -> tuple[str, str, int]:
    if (
        not isinstance(envelope, dict)
        or envelope.get("schema") != 1
        or envelope.get("provider") != "qobuz"
    ):
        raise SidecarConfigurationError("Qobuz credential is not configured")
    try:
        version = int(envelope.get("version"))
        nonce_text = str(envelope.get("nonce") or "")
        ciphertext_text = str(envelope.get("ciphertext") or "")
        nonce = base64.urlsafe_b64decode(
            nonce_text.encode() + b"=" * (-len(nonce_text) % 4)
        )
        ciphertext = base64.urlsafe_b64decode(
            ciphertext_text.encode() + b"=" * (-len(ciphertext_text) % 4)
        )
    except (binascii.Error, TypeError, ValueError) as exc:
        raise SidecarAuthError("Qobuz credential envelope is invalid") from exc
    if version < 1:
        raise SidecarAuthError("Qobuz credential envelope is invalid")
    key = _load_credential_key()
    expected_key_id = hashlib.sha256(key).hexdigest()[:16]
    if str(envelope.get("key_id") or "") != expected_key_id:
        raise SidecarAuthError("Qobuz credential key does not match")
    aad = f"music-service:provider-credential:v1:qobuz:{version}".encode()
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, aad)
        payload = json.loads(plaintext)
    except Exception as exc:
        raise SidecarAuthError("Qobuz credential integrity validation failed") from exc
    if not isinstance(payload, dict):
        raise SidecarAuthError("Qobuz credential payload is invalid")
    token = str(payload.get("token") or "").strip()
    user_id = str(payload.get("user_id") or "").strip()
    if not token or not user_id:
        raise SidecarConfigurationError("Qobuz credential is not configured")
    return token, user_id, version


def _bundle(force: bool = False) -> tuple[str, list[str]]:
    global _bundle_cache
    with _bundle_lock:
        now = time.monotonic()
        if not force and _bundle_cache and now - _bundle_cache[0] < _BUNDLE_TTL_SECONDS:
            return _bundle_cache[1], list(_bundle_cache[2])
        modules = _modules()
        try:
            fetched = modules["bundle"].Bundle()
            app_id = str(fetched.get_app_id())
            secrets = [str(value) for value in fetched.get_secrets().values() if value]
        except Exception as exc:
            raise SidecarError("Qobuz web bundle extraction failed") from exc
        if not app_id or not secrets:
            raise SidecarError("Qobuz web bundle contained no usable signing secrets")
        _bundle_cache = (now, app_id, secrets)
        return app_id, list(secrets)


def _token_client(
    modules: dict[str, Any],
    app_id: str,
    secrets: list[str],
    auth_token: str,
    user_id: str,
):
    client = modules["qopy"].Client.__new__(modules["qopy"].Client)
    client.secrets = secrets
    client.id = str(app_id)
    client.session = HardenedSession()
    client.session.headers.update(
        {
            "User-Agent": "Mozilla/5.0",
            "X-App-Id": str(app_id),
            "Content-Type": "application/json;charset=UTF-8",
            "X-User-Auth-Token": auth_token,
        }
    )
    client.base = "https://www.qobuz.com/api.json/0.2/"
    client.sec = None
    client.uat = auth_token
    client.label = "token"
    client.cfg_setup()
    data = client.api_call("user/get", user_id=user_id)
    if not isinstance(data, dict):
        raise SidecarAuthError("Qobuz returned an invalid account response")
    client.label = (
        data.get("credential", {}).get("parameters", {}).get("short_label")
        or "token"
    )
    return client


def _client(envelope: Any):
    if not _configured():
        raise SidecarConfigurationError("Qobuz sidecar credential key is not configured")
    auth_token, user_id, _version = _credential(envelope)
    modules = _modules()
    for attempt in range(2):
        app_id, secrets = _bundle(force=attempt == 1)
        try:
            return _token_client(modules, app_id, secrets, auth_token, user_id)
        except modules["InvalidAppSecretError"] as exc:
            if attempt == 0:
                continue
            raise SidecarError("Qobuz signing secret was rejected") from exc
        except (
            modules["AuthenticationError"],
            modules["IneligibleError"],
            modules["InvalidAppIdError"],
        ) as exc:
            raise SidecarAuthError("Qobuz rejected the configured credential") from exc
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 429:
                raise SidecarRateLimitedError("Qobuz rate limit is active") from exc
            if exc.response is not None and exc.response.status_code in (400, 401, 403):
                raise SidecarAuthError("Qobuz rejected the configured credential") from exc
            raise SidecarError("Qobuz API request failed") from exc
        except requests.RequestException as exc:
            raise SidecarError("Qobuz API is unavailable") from exc
    raise SidecarError("Qobuz client initialization failed")


def _snapshot() -> set[Path]:
    if not _STAGING.exists():
        return set()
    return {
        path.resolve()
        for path in _STAGING.rglob("*")
        if path.is_file() and not path.is_symlink()
    }


def _verified_new_files(before: set[Path]) -> list[str]:
    files: list[str] = []
    lossy_rejected = False
    for path in sorted(_snapshot() - before):
        if path.suffix.casefold() in {".mp3", ".aac"}:
            path.unlink(missing_ok=True)
            lossy_rejected = True
            continue
        if path.suffix.casefold() not in _AUDIO_EXTENSIONS:
            continue
        try:
            audio = MutagenFile(path)
            duration = getattr(getattr(audio, "info", None), "length", 0)
        except Exception:
            duration = 0
        if not duration or path.stat().st_size <= 0:
            continue
        try:
            relative = path.relative_to(_STAGING)
        except ValueError as exc:
            raise SidecarError("Downloader escaped the staging directory") from exc
        files.append(relative.as_posix())
    if lossy_rejected and not files:
        raise SidecarError("Qobuz returned no lossless file")
    return files


def _download_track(payload: dict[str, Any]) -> dict[str, Any]:
    track_id = str(payload.get("track_id") or "")
    if not re.fullmatch(r"[0-9]+", track_id):
        raise SidecarError("Invalid Qobuz track id")
    quality = int(payload.get("quality", 27))
    if quality not in (6, 7, 27):
        raise SidecarError("Invalid Qobuz quality")
    before = _snapshot()
    modules = _modules()
    modules["downloader"].Download(
        _client(payload.get("credential")),
        track_id,
        str(_STAGING),
        quality,
        embed_art=bool(payload.get("embed_art", True)),
        downgrade_quality=True,
    ).download_id_by_type(track=True)
    return {"files": _verified_new_files(before)}


def _download_url(payload: dict[str, Any]) -> dict[str, Any]:
    url = str(payload.get("url") or "")
    parsed = urlparse(url)
    _validate_https_url(url)
    if (parsed.hostname or "").casefold() not in {
        "play.qobuz.com",
        "open.qobuz.com",
        "www.qobuz.com",
    }:
        raise SidecarError("URL is not a Qobuz catalog link")
    match = _DOWNLOAD_URL_RE.fullmatch(parsed.path)
    if match is None:
        raise SidecarError("Only Qobuz album and track URLs are supported")
    kind, item_id = match.groups()
    quality = int(payload.get("quality", 27))
    if quality not in (6, 7, 27):
        raise SidecarError("Invalid Qobuz quality")
    before = _snapshot()
    modules = _modules()
    modules["downloader"].Download(
        _client(payload.get("credential")),
        item_id,
        str(_STAGING),
        quality,
        embed_art=bool(payload.get("embed_art", True)),
        downgrade_quality=True,
    ).download_id_by_type(track=kind == "track")
    return {"files": _verified_new_files(before)}


def _dispatch(method: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    if method == "GET" and path == "/status":
        return {"configured": _configured()}
    if method == "POST" and path in {"/connect", "/credentials/validate"}:
        client = _client(payload.get("credential"))
        return {"connected": True, "label": getattr(client, "label", None)}
    if method == "POST" and path == "/search":
        query = str(payload.get("query") or "").strip()
        kind = str(payload.get("kind") or "track")
        limit = max(1, min(50, int(payload.get("limit", 10))))
        if not query or len(query) > 256 or kind not in {"track", "album"}:
            raise SidecarError("Invalid Qobuz search request")
        client = _client(payload.get("credential"))
        result = (
            client.search_tracks(query, limit)
            if kind == "track"
            else client.search_albums(query, limit)
        )
        return {"result": result}
    if method == "POST" and path in {"/download/track", "/download/url"}:
        if not _download_lock.acquire(blocking=False):
            raise SidecarBusyError("Another Qobuz download is active")
        try:
            return _download_track(payload) if path.endswith("track") else _download_url(payload)
        finally:
            _download_lock.release()
    raise SidecarError("Unknown sidecar endpoint")


class Handler(BaseHTTPRequestHandler):
    server_version = "MusicServiceQobuzSidecar/1"

    def log_message(self, _format, *_args):
        return

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle(self) -> None:
        if self.path == "/health":
            self._send(200, {"status": "ok"})
            return
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {_INTERNAL_TOKEN}"
        if len(_INTERNAL_TOKEN) < 32 or not hmac.compare_digest(supplied, expected):
            self._send(401, {"error": "unauthorized"})
            return
        payload: dict[str, Any] = {}
        if self.command == "POST":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length < 0 or length > 16 * 1024:
                    raise SidecarLimitError("Control request is too large")
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict):
                    raise ValueError
            except (ValueError, json.JSONDecodeError):
                self._send(400, {"error": "invalid request"})
                return
        try:
            self._send(200, _dispatch(self.command, self.path, payload))
        except SidecarError as exc:
            self._send(exc.status, {"error": type(exc).__name__})
        except Exception:
            self._send(502, {"error": "SidecarProviderError"})

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()


if __name__ == "__main__":
    _STAGING.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer(("0.0.0.0", 8090), Handler).serve_forever()
