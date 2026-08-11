from __future__ import annotations

import base64

import pytest
from sqlalchemy import text

from app.models import Job, JobScope, JobStatus, PlaylistSource, ServiceEnum
from app.commands import migrate_credentials as credential_migration
from app.services.credentials import (
    CredentialIntegrityError,
    decrypt_envelope,
    encrypt_payload,
    get_credential_payload,
    get_credential_record,
    load_key,
    save_credential,
)
from app.services.provider_health import run_provider_health_check
from app.services.user_credentials import (
    get_user_credential_payload,
    save_user_credential,
)
from app.services.qobuz import (
    QobuzAuthError,
    QobuzProviderError,
    QobuzRateLimitedError,
)
from tests.helpers import ensure_user


def test_credential_envelope_round_trip_and_tamper_detection():
    envelope = encrypt_payload(
        "qobuz",
        {"token": "synthetic-qobuz-token", "user_id": "42"},
        1,
    )

    assert decrypt_envelope(envelope, expected_provider="qobuz") == {
        "token": "synthetic-qobuz-token",
        "user_id": "42",
    }
    changed = dict(envelope)
    encoded = changed["ciphertext"]
    raw = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
    raw[-1] ^= 1
    changed["ciphertext"] = base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(CredentialIntegrityError):
        decrypt_envelope(changed, expected_provider="qobuz")


def test_raw_credential_key_preserves_whitespace_bytes(tmp_path):
    key = b"\n" + (b"k" * 30) + b" "
    path = tmp_path / "raw.key"
    path.write_bytes(key)

    assert load_key(path) == key


def test_legacy_source_credentials_migrate_and_are_cleared(
    session_factory, monkeypatch
):
    db = session_factory()
    user = ensure_user(db)
    yandex_source = PlaylistSource(user_id=user.id, service=ServiceEnum.yandex)
    spotify_source = PlaylistSource(user_id=user.id, service=ServiceEnum.spotify)
    db.add_all([yandex_source, spotify_source])
    db.commit()
    db.execute(text("ALTER TABLE playlist_sources ADD COLUMN access_token TEXT"))
    db.execute(text("ALTER TABLE playlist_sources ADD COLUMN refresh_token TEXT"))
    db.execute(
        text(
            "UPDATE playlist_sources SET access_token = :token "
            "WHERE id = :source_id"
        ),
        {"token": "legacy-yandex-token", "source_id": yandex_source.id},
    )
    db.execute(
        text(
            "UPDATE playlist_sources SET access_token = :access, "
            "refresh_token = :refresh WHERE id = :source_id"
        ),
        {
            "access": "legacy-spotify-access",
            "refresh": "legacy-spotify-refresh",
            "source_id": spotify_source.id,
        },
    )
    db.commit()
    db.close()
    monkeypatch.setattr(credential_migration, "SessionLocal", session_factory)

    assert credential_migration.migrate() == 2

    db = session_factory()
    remaining = db.execute(
        text(
            "SELECT count(*) FROM playlist_sources WHERE access_token IS NOT NULL "
            "OR refresh_token IS NOT NULL"
        )
    ).scalar_one()
    assert remaining == 0
    assert get_credential_payload(db, "yandex") == {"token": "legacy-yandex-token"}
    assert get_credential_payload(db, "spotify") == {
        "access_token": "legacy-spotify-access",
        "refresh_token": "legacy-spotify-refresh",
    }
    db.close()


def test_qobuz_rotation_validates_before_activation(
    api_client, auth_headers, db, monkeypatch
):
    monkeypatch.setattr(
        "app.api.providers.QobuzSidecarClient.validate_credential",
        lambda _self, _envelope: {"connected": True, "label": "Studio"},
    )

    response = api_client.put(
        "/api/providers/qobuz/credentials",
        json={"token": "new-synthetic-token", "user_id": "123"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store"
    assert response.json() == {
        "provider": "qobuz",
        "configured": True,
        "version": 1,
        "updated_at": response.json()["updated_at"],
        "label": "Studio",
    }
    assert "new-synthetic-token" not in response.text
    assert get_credential_payload(db, "qobuz") == {
        "token": "new-synthetic-token",
        "user_id": "123",
    }
    record = get_credential_record(db, "qobuz")
    assert "new-synthetic-token" not in record.ciphertext


def test_failed_qobuz_rotation_preserves_old_credential(
    api_client, auth_headers, db, monkeypatch
):
    save_credential(
        db,
        "qobuz",
        {"token": "old-synthetic-token", "user_id": "7"},
    )
    db.commit()

    def reject(_self, _envelope):
        raise QobuzAuthError("rejected")

    monkeypatch.setattr(
        "app.api.providers.QobuzSidecarClient.validate_credential", reject
    )
    response = api_client.put(
        "/api/providers/qobuz/credentials",
        json={"token": "bad-synthetic-token", "user_id": "8"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert "bad-synthetic-token" not in response.text
    assert get_credential_payload(db, "qobuz") == {
        "token": "old-synthetic-token",
        "user_id": "7",
    }
    assert get_credential_record(db, "qobuz").version == 1


def test_system_yandex_rotation_does_not_overwrite_user_playlist_credential(
    api_client, auth_headers, owner_user, db, monkeypatch
):
    source = PlaylistSource(user_id=owner_user.id, service=ServiceEnum.yandex)
    db.add(source)
    db.flush()
    save_user_credential(
        db,
        owner_user.id,
        "yandex",
        {"token": "user-playlist-token"},
    )
    db.commit()
    monkeypatch.setattr(
        "app.api.providers.create_yandex_client", lambda _token: object()
    )

    response = api_client.put(
        "/api/providers/yandex/credentials",
        json={"token": "new-yandex-synthetic-token"},
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert "new-yandex-synthetic-token" not in response.text
    assert get_credential_payload(db, "yandex") == {
        "token": "new-yandex-synthetic-token"
    }
    assert get_user_credential_payload(db, owner_user.id, "yandex") == {
        "token": "user-playlist-token"
    }


def test_failed_yandex_rotation_preserves_old_credential(
    api_client, auth_headers, db, monkeypatch
):
    save_credential(db, "yandex", {"token": "old-yandex-synthetic-token"})
    db.commit()

    class UnauthorizedError(RuntimeError):
        pass

    def reject(_token):
        raise UnauthorizedError("rejected")

    monkeypatch.setattr("app.api.providers.create_yandex_client", reject)
    response = api_client.put(
        "/api/providers/yandex/credentials",
        json={"token": "bad-yandex-synthetic-token"},
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert "bad-yandex-synthetic-token" not in response.text
    assert get_credential_payload(db, "yandex") == {
        "token": "old-yandex-synthetic-token"
    }
    assert get_credential_record(db, "yandex").version == 1


def test_provider_rotation_rejects_cross_origin(api_client, auth_headers):
    response = api_client.put(
        "/api/providers/yandex/credentials",
        json={"token": "new-yandex-synthetic-token"},
        headers={**auth_headers, "Origin": "https://attacker.invalid"},
    )

    assert response.status_code == 403


def test_provider_rotation_accepts_same_origin(
    api_client, auth_headers, monkeypatch
):
    monkeypatch.setattr(
        "app.api.providers.create_yandex_client", lambda _token: object()
    )
    response = api_client.put(
        "/api/providers/yandex/credentials",
        json={"token": "new-yandex-synthetic-token"},
        headers={**auth_headers, "Origin": "http://testserver"},
    )

    assert response.status_code == 200


def test_health_api_has_separate_components_without_credentials(
    api_client, auth_headers, db
):
    save_credential(
        db,
        "qobuz",
        {"token": "hidden-synthetic-token", "user_id": "10"},
    )
    db.commit()

    response = api_client.get("/api/providers/health", headers=auth_headers)

    assert response.status_code == 200
    item = next(row for row in response.json()["items"] if row["provider"] == "qobuz")
    assert set(item) >= {
        "account",
        "provider_api",
        "sidecar",
        "worker",
    }
    assert item["worker"]["state"] == "provider_down"
    assert "hidden-synthetic-token" not in response.text


@pytest.mark.parametrize(
    ("error", "account_state", "api_state"),
    [
        (QobuzAuthError("rejected"), "expired", "healthy"),
        (QobuzRateLimitedError("slow down"), "rate_limited", "rate_limited"),
        (QobuzProviderError("offline"), "provider_down", "provider_down"),
    ],
)
def test_qobuz_health_classifies_account_and_api_separately(
    db, monkeypatch, error, account_state, api_state
):
    save_credential(
        db,
        "qobuz",
        {"token": "hidden-synthetic-token", "user_id": "10"},
    )
    db.commit()
    monkeypatch.setattr("app.config.settings.qobuz_enabled", True)
    monkeypatch.setattr(
        "app.config.settings.qobuz_sidecar_url", "http://qobuz-sidecar.invalid"
    )
    monkeypatch.setattr("app.config.settings.qobuz_internal_token", "i" * 32)
    monkeypatch.setattr(
        "app.services.provider_health.QobuzSidecarClient.status",
        lambda _self: {"configured": True},
    )

    def fail(_db):
        raise error

    monkeypatch.setattr("app.services.provider_health.create_qobuz_client", fail)
    snapshot = run_provider_health_check(db, "qobuz")

    assert snapshot["account"]["state"] == account_state
    assert snapshot["provider_api"]["state"] == api_state
    assert snapshot["sidecar"]["state"] == "healthy"
    assert snapshot["worker"]["state"] == "healthy"


def test_manual_health_check_queues_safe_job(
    api_client, auth_headers, db, monkeypatch
):
    queued = []
    monkeypatch.setattr(
        "app.api.providers.provider_health_check_task.delay",
        lambda *args: queued.append(args),
    )

    response = api_client.post(
        "/api/providers/qobuz/health-check",
        headers=auth_headers,
    )

    assert response.status_code == 202
    job = db.get(Job, response.json()["id"])
    assert job.status == JobStatus.pending
    assert job.scope == JobScope.system
    assert job.user_id is None
    assert queued == [("qobuz", job.id)]
    assert "token" not in response.text.casefold()
