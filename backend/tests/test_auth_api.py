def test_health_exposes_non_secret_release_identity(api_client):
    response = api_client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["release_sha"]
    assert response.headers["Cache-Control"] == "private, no-store"


def test_validation_errors_do_not_echo_secret_inputs(api_client):
    marker = "secret-input-marker"
    response = api_client.post(
        "/api/auth/recovery/login",
        json={"token": marker + "x" * 4096},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 422
    assert marker not in response.text


def test_server_session_authentication_and_logout(api_client, auth_headers):
    me = api_client.get("/api/auth/me", headers=auth_headers)

    assert me.status_code == 200
    assert me.json()["role"] == "owner"
    assert me.json()["csrf_token"] == auth_headers["X-CSRF-Token"]
    assert "google_sub" not in me.text
    assert me.headers["Cache-Control"] == "private, no-store"

    playlists = api_client.get("/api/playlists", headers=auth_headers)
    assert playlists.headers["Cache-Control"] == "private, no-store"

    logged_out = api_client.post("/api/auth/logout", headers=auth_headers)

    assert logged_out.status_code == 204
    assert api_client.get("/api/playlists", headers=auth_headers).status_code == 401


def test_bearer_app_token_is_not_normal_user_authentication(api_client):
    response = api_client.get(
        "/api/playlists",
        headers={"Authorization": "Bearer test-auth-token-12345"},
    )

    assert response.status_code == 401


def test_recovery_is_isolated_and_uses_server_cookie(api_client, owner_user):
    rejected = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "invalid-recovery-value"},
        headers={"Origin": "http://testserver"},
    )
    accepted = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "test-auth-token-12345"},
        headers={"Origin": "http://testserver"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json()["authenticated"] is True
    assert accepted.json()["csrf_token"]
    cookie = accepted.headers["set-cookie"]
    assert "audiofeel_recovery=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api/auth/recovery" in cookie
    assert api_client.get("/api/playlists").status_code == 401


def test_recovery_cookie_is_secure_when_configured(api_client, owner_user, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    response = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "test-auth-token-12345"},
        headers={"Origin": "http://testserver"},
    )

    assert response.status_code == 200
    assert "Secure" in response.headers["set-cookie"]


def test_recovery_uses_owner_binding_not_general_invitation(api_client, db, owner_user):
    login = api_client.post(
        "/api/auth/recovery/login",
        json={"token": "test-auth-token-12345"},
        headers={"Origin": "http://testserver"},
    )
    csrf = login.json()["csrf_token"]

    status = api_client.get("/api/auth/recovery/status")
    updated = api_client.post(
        "/api/auth/recovery/owner-binding",
        json={
            "email": "replacement-owner@example.test",
            "confirm": "RESET BOOTSTRAP OWNER",
        },
        headers={
            "Origin": "http://testserver",
            "X-CSRF-Token": csrf,
        },
    )

    assert status.status_code == 200
    assert status.json()["owner_configured"] is True
    assert "owner_invited" not in status.json()
    assert updated.status_code == 204
    db.refresh(owner_user)
    assert owner_user.email == "replacement-owner@example.test"
    assert owner_user.state.value == "pending"
    assert owner_user.role.value == "owner"
    assert owner_user.is_bootstrap_owner is True
    assert api_client.post("/api/auth/recovery/owner-invitation").status_code == 404
