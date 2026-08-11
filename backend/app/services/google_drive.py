"""Minimal, secret-safe Google Drive v3 and OAuth web-server client."""

from __future__ import annotations

import hashlib
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import settings

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
DRIVE_API = "https://www.googleapis.com/drive/v3"
DRIVE_UPLOAD_API = "https://www.googleapis.com/upload/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.file"
ROOT_FOLDER_NAME = "Audiofeel Library"


class GoogleDriveError(RuntimeError):
    pass


class GoogleDriveAuthError(GoogleDriveError):
    pass


class GoogleDriveRateLimited(GoogleDriveError):
    def __init__(self, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class GoogleDriveUnavailable(GoogleDriveError):
    pass


class GoogleDriveIntegrityError(GoogleDriveError):
    pass


@dataclass(frozen=True, slots=True)
class OAuthClientConfig:
    client_id: str
    client_secret: str
    redirect_uri: str


@dataclass(slots=True)
class DriveDownload:
    client: httpx.Client
    response: httpx.Response

    def close(self) -> None:
        self.response.close()
        self.client.close()


def code_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    import base64

    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def authorization_url(config: OAuthClientConfig, state: str, verifier: str) -> str:
    query = urlencode(
        {
            "client_id": config.client_id,
            "redirect_uri": config.redirect_uri,
            "response_type": "code",
            "scope": DRIVE_SCOPE,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent select_account",
            "state": state,
            "code_challenge": code_challenge(verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{GOOGLE_AUTH_URL}?{query}"


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.google_drive_connect_timeout_seconds,
        read=settings.google_drive_read_timeout_seconds,
        write=settings.google_drive_read_timeout_seconds,
        pool=settings.google_drive_connect_timeout_seconds,
    )


def _safe_retry_after(response: httpx.Response) -> int | None:
    try:
        value = int(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None
    return value if 0 <= value <= 86400 else None


def _is_rate_limited(response: httpx.Response) -> bool:
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    try:
        errors = ((response.json().get("error") or {}).get("errors") or [])
    except (TypeError, ValueError):
        return False
    rate_reasons = {
        "rateLimitExceeded",
        "userRateLimitExceeded",
        "sharingRateLimitExceeded",
        "downloadQuotaExceeded",
    }
    return any(str(item.get("reason") or "") in rate_reasons for item in errors)


def _raise_response(response: httpx.Response, *, operation: str) -> None:
    if _is_rate_limited(response):
        raise GoogleDriveRateLimited(
            f"Google rate limit during {operation}", _safe_retry_after(response)
        )
    if response.status_code in {401, 403}:
        raise GoogleDriveAuthError(f"Google authorization failed during {operation}")
    if response.status_code >= 500:
        raise GoogleDriveUnavailable(f"Google unavailable during {operation}")
    raise GoogleDriveError(f"Google request failed during {operation}")


def exchange_authorization_code(
    config: OAuthClientConfig,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    try:
        response = httpx.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": config.redirect_uri,
            },
            timeout=_timeout(),
        )
    except httpx.HTTPError as exc:
        raise GoogleDriveUnavailable("Google token service is unavailable") from exc
    if response.status_code != 200:
        if response.status_code in {400, 401}:
            raise GoogleDriveAuthError("Google rejected the authorization code")
        _raise_response(response, operation="OAuth exchange")
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("access_token"):
        raise GoogleDriveAuthError("Google returned no access token")
    return payload


class GoogleDriveClient:
    def __init__(
        self,
        config: OAuthClientConfig,
        refresh_token: str,
        *,
        access_token: str | None = None,
    ):
        self.config = config
        self.refresh_token = str(refresh_token or "")
        self._access_token = access_token

    def _token(self) -> str:
        if self._access_token:
            return self._access_token
        if not self.refresh_token:
            raise GoogleDriveAuthError("Google refresh token is unavailable")
        try:
            response = httpx.post(
                GOOGLE_TOKEN_URL,
                data={
                    "client_id": self.config.client_id,
                    "client_secret": self.config.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                },
                timeout=_timeout(),
            )
        except httpx.HTTPError as exc:
            raise GoogleDriveUnavailable("Google token service is unavailable") from exc
        if response.status_code != 200:
            if response.status_code in {400, 401}:
                raise GoogleDriveAuthError("Google refresh token was rejected")
            _raise_response(response, operation="token refresh")
        payload = response.json()
        token = str(payload.get("access_token") or "")
        if not token:
            raise GoogleDriveAuthError("Google returned no access token")
        self._access_token = token
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}"}

    def _request(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        attempts: int = 3,
        **kwargs,
    ) -> httpx.Response:
        extra_headers = kwargs.pop("headers", {})
        for attempt in range(attempts):
            try:
                response = httpx.request(
                    method,
                    url,
                    headers={**self._headers(), **extra_headers},
                    timeout=_timeout(),
                    **kwargs,
                )
            except httpx.HTTPError as exc:
                if attempt + 1 >= attempts:
                    raise GoogleDriveUnavailable(
                        f"Google unavailable during {operation}"
                    ) from exc
                time.sleep(min(2**attempt, 2))
                continue
            if response.status_code < 400:
                return response
            if response.status_code == 401 and attempt == 0:
                self._access_token = None
                continue
            if _is_rate_limited(response) or response.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(min(_safe_retry_after(response) or 2**attempt, 2))
                    continue
            _raise_response(response, operation=operation)
        raise GoogleDriveUnavailable(f"Google unavailable during {operation}")

    def about(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{DRIVE_API}/about",
            operation="account check",
            params={"fields": "user(displayName,emailAddress,permissionId),storageQuota"},
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleDriveError("Google account response is invalid")
        return payload

    def get_file_metadata(self, remote_file_id: str) -> dict[str, Any]:
        response = self._request(
            "GET",
            f"{DRIVE_API}/files/{remote_file_id}",
            operation="file metadata",
            params={
                "fields": (
                    "id,name,mimeType,size,sha1Checksum,md5Checksum,trashed,"
                    "capabilities(canDownload)"
                )
            },
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GoogleDriveError("Google file metadata is invalid")
        return payload

    def ensure_root_folder(self) -> str:
        query = (
            "name = 'Audiofeel Library' and "
            "mimeType = 'application/vnd.google-apps.folder' and trashed = false"
        )
        response = self._request(
            "GET",
            f"{DRIVE_API}/files",
            operation="root folder lookup",
            params={
                "q": query,
                "spaces": "drive",
                "pageSize": 10,
                "fields": "files(id,name,appProperties)",
            },
        )
        files = response.json().get("files") or []
        managed = next(
            (
                item
                for item in files
                if (item.get("appProperties") or {}).get("audiofeel_role") == "root"
            ),
            None,
        )
        if managed is None and files:
            managed = files[0]
        if managed is not None and managed.get("id"):
            return str(managed["id"])
        response = self._request(
            "POST",
            f"{DRIVE_API}/files",
            operation="root folder creation",
            params={"fields": "id"},
            json={
                "name": ROOT_FOLDER_NAME,
                "mimeType": "application/vnd.google-apps.folder",
                "appProperties": {"audiofeel_role": "root"},
            },
        )
        folder_id = str(response.json().get("id") or "")
        if not folder_id:
            raise GoogleDriveError("Google returned no root folder ID")
        return folder_id

    def find_by_sha1(self, sha1: str) -> dict[str, Any] | None:
        safe_sha1 = str(sha1 or "").casefold()
        if len(safe_sha1) != 40 or any(ch not in "0123456789abcdef" for ch in safe_sha1):
            raise GoogleDriveIntegrityError("Invalid SHA-1")
        response = self._request(
            "GET",
            f"{DRIVE_API}/files",
            operation="duplicate lookup",
            params={
                "q": (
                    "appProperties has { key='audiofeel_sha1' and "
                    f"value='{safe_sha1}' }} and trashed = false"
                ),
                "spaces": "drive",
                "pageSize": 10,
                "fields": "files(id,name,size,sha1Checksum,appProperties)",
            },
        )
        files = response.json().get("files") or []
        return files[0] if files else None

    def upload_file(
        self,
        path: str | Path,
        *,
        root_folder_id: str,
        sha1: str,
        catalog_file_id: int,
    ) -> dict[str, Any]:
        source = Path(path).expanduser().resolve(strict=True)
        if not source.is_file():
            raise GoogleDriveIntegrityError("Upload source is unavailable")
        total = source.stat().st_size
        existing = self.find_by_sha1(sha1)
        if existing is not None:
            self._verify_uploaded(existing, expected_size=total, expected_sha1=sha1)
            return existing
        mime_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"
        response = self._request(
            "POST",
            f"{DRIVE_UPLOAD_API}/files",
            operation="resumable upload initialization",
            params={
                "uploadType": "resumable",
                "fields": "id,name,size,sha1Checksum,md5Checksum,appProperties",
            },
            headers={
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(total),
            },
            json={
                "name": source.name,
                "parents": [root_folder_id],
                "mimeType": mime_type,
                "appProperties": {
                    "audiofeel_sha1": sha1.casefold(),
                    "audiofeel_catalog_file_id": str(catalog_file_id),
                },
            },
        )
        session_uri = response.headers.get("Location")
        if not session_uri:
            raise GoogleDriveError("Google returned no resumable upload session")
        chunk_size = settings.google_drive_upload_chunk_bytes
        offset = 0
        final: dict[str, Any] | None = None
        with source.open("rb") as stream:
            while offset < total:
                stream.seek(offset)
                chunk = stream.read(min(chunk_size, total - offset))
                if not chunk:
                    raise GoogleDriveIntegrityError("Upload source ended unexpectedly")
                end = offset + len(chunk) - 1
                for attempt in range(settings.google_drive_upload_retry_attempts):
                    try:
                        upload = httpx.put(
                            session_uri,
                            content=chunk,
                            headers={
                                **self._headers(),
                                "Content-Length": str(len(chunk)),
                                "Content-Range": f"bytes {offset}-{end}/{total}",
                            },
                            timeout=_timeout(),
                        )
                    except httpx.HTTPError as exc:
                        if attempt + 1 >= settings.google_drive_upload_retry_attempts:
                            raise GoogleDriveUnavailable(
                                "Google upload was interrupted"
                            ) from exc
                        time.sleep(min(2**attempt, 4))
                        status = self._resumable_status(session_uri, total)
                        if isinstance(status, dict):
                            final = status
                            offset = total
                            break
                        offset = status
                        break

                    if upload.status_code in {200, 201}:
                        final = upload.json()
                        offset = total
                        break
                    if upload.status_code == 308:
                        offset = self._next_upload_offset(upload, end + 1)
                        break
                    if upload.status_code == 401 and attempt == 0:
                        self._access_token = None
                        continue
                    if _is_rate_limited(upload) or upload.status_code >= 500:
                        if attempt + 1 < settings.google_drive_upload_retry_attempts:
                            time.sleep(min(_safe_retry_after(upload) or 2**attempt, 4))
                            status = self._resumable_status(session_uri, total)
                            if isinstance(status, dict):
                                final = status
                                offset = total
                                break
                            offset = status
                            break
                    _raise_response(upload, operation="resumable upload")
                else:
                    raise GoogleDriveUnavailable("Google upload retry limit reached")
        if final is None:
            raise GoogleDriveError("Google upload did not complete")
        self._verify_uploaded(final, expected_size=total, expected_sha1=sha1)
        return final

    @staticmethod
    def _next_upload_offset(response: httpx.Response, fallback: int) -> int:
        received = response.headers.get("Range")
        if not received:
            return fallback
        try:
            return int(received.rsplit("-", 1)[1]) + 1
        except (IndexError, ValueError) as exc:
            raise GoogleDriveError("Google returned an invalid upload range") from exc

    def _resumable_status(
        self,
        session_uri: str,
        total: int,
    ) -> int | dict[str, Any]:
        try:
            response = httpx.put(
                session_uri,
                content=b"",
                headers={
                    **self._headers(),
                    "Content-Length": "0",
                    "Content-Range": f"bytes */{total}",
                },
                timeout=_timeout(),
            )
        except httpx.HTTPError as exc:
            raise GoogleDriveUnavailable("Google upload status is unavailable") from exc
        if response.status_code in {200, 201}:
            payload = response.json()
            if not isinstance(payload, dict):
                raise GoogleDriveError("Google upload status is invalid")
            return payload
        if response.status_code == 308:
            return self._next_upload_offset(response, 0)
        _raise_response(response, operation="resumable upload status")
        raise GoogleDriveUnavailable("Google upload status is unavailable")

    @staticmethod
    def _verify_uploaded(
        payload: dict[str, Any], *, expected_size: int, expected_sha1: str
    ) -> None:
        try:
            actual_size = int(payload.get("size"))
        except (TypeError, ValueError) as exc:
            raise GoogleDriveIntegrityError("Google returned no file size") from exc
        actual_sha1 = str(payload.get("sha1Checksum") or "").casefold()
        if actual_size != expected_size or actual_sha1 != expected_sha1.casefold():
            raise GoogleDriveIntegrityError("Google upload checksum validation failed")
        if not payload.get("id"):
            raise GoogleDriveIntegrityError("Google returned no file ID")

    def open_download(
        self, remote_file_id: str, *, range_header: str | None = None
    ) -> DriveDownload:
        client = httpx.Client(timeout=_timeout(), follow_redirects=True)
        headers = self._headers()
        if range_header:
            headers["Range"] = range_header
        request = client.build_request(
            "GET",
            f"{DRIVE_API}/files/{remote_file_id}",
            params={"alt": "media"},
            headers=headers,
        )
        try:
            response = client.send(request, stream=True)
        except httpx.HTTPError as exc:
            client.close()
            raise GoogleDriveUnavailable("Google download is unavailable") from exc
        if response.status_code not in {200, 206}:
            try:
                _raise_response(response, operation="download")
            finally:
                response.close()
                client.close()
        return DriveDownload(client, response)

    def revoke(self) -> None:
        try:
            response = httpx.post(
                GOOGLE_REVOKE_URL,
                data={"token": self.refresh_token},
                timeout=_timeout(),
            )
        except httpx.HTTPError as exc:
            raise GoogleDriveUnavailable("Google revoke endpoint is unavailable") from exc
        if response.status_code not in {200, 400}:
            _raise_response(response, operation="token revocation")
