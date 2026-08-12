from __future__ import annotations

from sqlalchemy import select

from app.models import PlayerCredential, User, UserRole, UserState, utcnow
from app.services.authentication import keyed_digest


def test_player_key_is_returned_once_and_only_digest_is_persisted(
    api_client, auth_headers, db, owner_user
):
    created = api_client.post(
        "/api/player-credentials",
        headers=auth_headers,
        json={"label": "  Symfonium phone  "},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["api_key"].startswith("afk1.")
    assert body["server_url"] == "http://testserver"

    row = db.scalar(select(PlayerCredential).where(PlayerCredential.id == body["id"]))
    assert row is not None
    assert row.label == "Symfonium phone"
    assert row.secret_hash == keyed_digest("opensubsonic-api-key-v1", body["api_key"])
    assert body["api_key"].encode() not in row.secret_hash

    listed = api_client.get("/api/player-credentials", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json()["items"][0]["id"] == body["id"]
    assert "api_key" not in listed.json()["items"][0]
    assert "public_handle" not in listed.text
    assert "secret_hash" not in listed.text


def test_revoke_one_and_revoke_all_are_user_scoped(api_client, auth_headers, db, owner_user):
    first = api_client.post(
        "/api/player-credentials", headers=auth_headers, json={"label": "Phone"}
    ).json()
    second = api_client.post(
        "/api/player-credentials", headers=auth_headers, json={"label": "Tablet"}
    ).json()
    assert api_client.post(
        f"/api/player-credentials/{first['id']}/revoke", headers=auth_headers
    ).status_code == 204
    assert api_client.post(
        "/api/player-credentials/revoke-all", headers=auth_headers
    ).status_code == 204
    rows = db.scalars(
        select(PlayerCredential).where(PlayerCredential.id.in_([first["id"], second["id"]]))
    ).all()
    assert all(row.revoked_at is not None for row in rows)


def test_disabling_user_revokes_web_and_player_credentials(
    api_client, auth_headers, db, owner_user
):
    user = User(
        email="listener@example.test",
        email_key="listener@example.test",
        role=UserRole.user,
        state=UserState.active,
        is_bootstrap_owner=False,
        created_at=utcnow(),
        activated_at=utcnow(),
    )
    db.add(user)
    db.flush()
    from app.services.player_credentials import create_player_credential

    _raw, credential = create_player_credential(db, user.id, "Listener")
    db.commit()
    response = api_client.post(f"/api/admin/users/{user.id}/disable", headers=auth_headers)
    assert response.status_code == 200
    db.refresh(credential)
    assert credential.revoked_at is not None
