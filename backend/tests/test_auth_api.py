def test_login_cookie_authentication_and_logout(api_client):
    rejected = api_client.post(
        "/api/auth/login",
        json={"token": "неверный-токен-value"},
    )
    accepted = api_client.post(
        "/api/auth/login",
        json={"token": "test-auth-token-12345"},
    )

    assert rejected.status_code == 401
    assert accepted.status_code == 200
    assert accepted.json() == {"authenticated": True}
    assert "music_session=" in accepted.headers["set-cookie"]
    assert "HttpOnly" in accepted.headers["set-cookie"]
    assert "SameSite=strict" in accepted.headers["set-cookie"]
    assert api_client.get("/api/playlists").status_code == 200

    logged_out = api_client.post("/api/auth/logout")

    assert logged_out.status_code == 204
    assert api_client.get("/api/playlists").status_code == 401


def test_bearer_auth_remains_supported(api_client, auth_headers):
    response = api_client.get("/api/playlists", headers=auth_headers)

    assert response.status_code == 200
