import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from app.models import PushSubscription, StorageSecret
from app.services.web_push import send_workflow_completed


def _vapid_key(tmp_path):
    path = tmp_path / "vapid.pem"
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return path


def test_push_is_disabled_without_server_key(api_client, auth_headers):
    response = api_client.get("/api/push", headers=auth_headers)
    assert response.status_code == 200
    assert response.json() == {
        "enabled": False,
        "configured": False,
        "public_key": None,
        "subscriptions": [],
    }


def test_push_rejects_non_browser_endpoint(
    api_client, auth_headers, tmp_path, monkeypatch
):
    key_path = _vapid_key(tmp_path)
    monkeypatch.setattr("app.services.web_push.settings.web_push_enabled", True)
    monkeypatch.setattr(
        "app.services.web_push.settings.web_push_vapid_private_key_file",
        str(key_path),
    )
    response = api_client.post(
        "/api/push/subscriptions",
        headers=auth_headers,
        json={
            "endpoint": "https://127.0.0.1/internal",
            "keys": {"p256dh": "A" * 88, "auth": "B" * 24},
        },
    )
    assert response.status_code == 409


def test_push_subscription_is_encrypted_and_only_final_event_is_sent(
    api_client, auth_headers, db, tmp_path, monkeypatch
):
    key_path = _vapid_key(tmp_path)
    monkeypatch.setattr("app.services.web_push.settings.web_push_enabled", True)
    monkeypatch.setattr(
        "app.services.web_push.settings.web_push_vapid_private_key_file",
        str(key_path),
    )
    monkeypatch.setattr(
        "app.services.web_push.settings.web_push_vapid_subject",
        "mailto:test@example.test",
    )
    endpoint = "https://fcm.googleapis.com/subscriptions/device-secret"
    response = api_client.post(
        "/api/push/subscriptions",
        headers=auth_headers,
        json={
            "endpoint": endpoint,
            "expirationTime": None,
            "keys": {"p256dh": "A" * 88, "auth": "B" * 24},
            "user_agent_label": "test browser",
        },
    )
    assert response.status_code == 201
    assert endpoint not in response.text
    db.expire_all()
    subscription = db.query(PushSubscription).one()
    secret = db.get(StorageSecret, subscription.secret_id)
    assert endpoint not in secret.ciphertext

    delivered = []

    def capture(**kwargs):
        delivered.append(json.loads(kwargs["data"]))

    monkeypatch.setattr("app.services.web_push.webpush", capture)
    result = send_workflow_completed(
        db,
        user_id=subscription.user_id,
        workflow_id=77,
        ready=8,
        missing=2,
        needs_review=1,
    )
    db.commit()

    assert result["sent"] == 1
    assert delivered == [
        {
            "type": "workflow_completed",
            "title": "Music Service",
            "body": "Готово: 8 треков доступны, 2 не найдены, 1 требуют проверки",
            "url": "/#/playlists",
            "tag": "workflow-77",
        }
    ]


def test_unsubscribe_is_user_scoped(api_client, auth_headers, db, tmp_path, monkeypatch):
    key_path = _vapid_key(tmp_path)
    monkeypatch.setattr("app.services.web_push.settings.web_push_enabled", True)
    monkeypatch.setattr("app.services.web_push.settings.web_push_vapid_private_key_file", str(key_path))
    endpoint = "https://fcm.googleapis.com/subscriptions/remove-me"
    created = api_client.post(
        "/api/push/subscriptions",
        headers=auth_headers,
        json={
            "endpoint": endpoint,
            "keys": {"p256dh": "A" * 88, "auth": "B" * 24},
        },
    )
    assert created.status_code == 201
    removed = api_client.post(
        "/api/push/subscriptions/unsubscribe",
        headers=auth_headers,
        json={"endpoint": endpoint},
    )
    assert removed.status_code == 204
    db.expire_all()
    assert db.query(PushSubscription).count() == 0

