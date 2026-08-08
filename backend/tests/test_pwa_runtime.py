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
