from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app


class FakeResponse:
    status_code = 200
    history = []
    url = "https://streaming-qobuz-sec.akamaized.net/file"
    headers = {"content-length": "6"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield b"abc"
        yield b"def"


class SidecarSecurityTests(unittest.TestCase):
    def test_configured_requires_internal_and_provider_tokens(self):
        with (
            patch.object(app, "_INTERNAL_TOKEN", "i" * 32),
            patch.object(app, "_AUTH_TOKEN", "provider"),
        ):
            self.assertTrue(app._configured())
        with (
            patch.object(app, "_INTERNAL_TOKEN", "internal"),
            patch.object(app, "_AUTH_TOKEN", ""),
        ):
            self.assertFalse(app._configured())
        with (
            patch.object(app, "_INTERNAL_TOKEN", "too-short"),
            patch.object(app, "_AUTH_TOKEN", "provider"),
        ):
            self.assertFalse(app._configured())

    def test_outbound_allowlist_accepts_only_https_qobuz_hosts(self):
        self.assertEqual(
            app._validate_https_url("https://www.qobuz.com/api.json/0.2/track/get"),
            "www.qobuz.com",
        )
        self.assertEqual(
            app._validate_https_url(
                "https://streaming-qobuz-sec.akamaized.net/file"
            ),
            "streaming-qobuz-sec.akamaized.net",
        )
        for blocked in (
            "http://www.qobuz.com/file",
            "https://www.qobuz.com.evil.example/file",
            "https://127.0.0.1/file",
            "https://www.qobuz.com:8443/file",
        ):
            with self.subTest(blocked=blocked), self.assertRaises(app.SidecarError):
                app._validate_https_url(blocked)

    def test_safe_download_enforces_content_length_and_writes_exact_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "track.tmp"
            with patch.object(app.HardenedSession, "get", return_value=FakeResponse()):
                app._safe_download(
                    "https://streaming-qobuz-sec.akamaized.net/file",
                    str(target),
                    "track",
                )
            self.assertEqual(target.read_bytes(), b"abcdef")

    def test_safe_download_removes_partial_file_when_limit_is_exceeded(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "track.tmp"
            with (
                patch.object(app, "_MAX_FILE_BYTES", 5),
                patch.object(app.HardenedSession, "get", return_value=FakeResponse()),
                self.assertRaises(app.SidecarLimitError),
            ):
                app._safe_download(
                    "https://streaming-qobuz-sec.akamaized.net/file",
                    str(target),
                    "track",
                )
            self.assertFalse(target.exists())

    def test_catalog_url_parser_rejects_non_catalog_and_non_qobuz_urls(self):
        for blocked in (
            "https://evil.example/track/1",
            "https://play.qobuz.com/playlist/1",
            "file:///etc/passwd",
        ):
            with self.subTest(blocked=blocked), self.assertRaises(app.SidecarError):
                app._download_url({"url": blocked, "quality": 27})

    def test_lossy_download_is_deleted_and_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            staging = Path(directory).resolve()
            lossy = staging / "track.mp3"
            lossy.write_bytes(b"lossy")
            with (
                patch.object(app, "_STAGING", staging),
                self.assertRaisesRegex(app.SidecarError, "no lossless file"),
            ):
                app._verified_new_files(set())
            self.assertFalse(lossy.exists())

    def test_status_contains_no_provider_credentials(self):
        with patch.object(app, "_INTERNAL_TOKEN", "i" * 32):
            result = app._dispatch("GET", "/status", {})
        self.assertEqual(set(result), {"configured"})


if __name__ == "__main__":
    unittest.main()
