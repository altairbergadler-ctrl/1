from __future__ import annotations

import base64
import hashlib
import logging
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from jose import jwt

from app.config import settings
from app.models import GoogleLoginAttempt, User, UserRole, UserState, utcnow
from app.services import google_login
from app.services.authentication import keyed_digest, open_login_value, seal_login_value
from app.services.google_login import (
    EXPECTED_ISSUER,
    GoogleAccountNotInvited,
    GoogleIdentity,
    GoogleIdentityError,
    GoogleLoginStateError,
)


CLIENT_ID = "audiofeel-login-test.apps.googleusercontent.com"


def _b64_integer(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _key_pair(kid: str):
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = private.public_key().public_numbers()
    public_jwk = {
        "kty": "RSA",
        "use": "sig",
        "alg": "RS256",
        "kid": kid,
        "n": _b64_integer(numbers.n),
        "e": _b64_integer(numbers.e),
    }
    private_pem = private.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return private_pem, public_jwk


def _claims(*, nonce: str = "login-nonce") -> dict:
    now = datetime.now(UTC)
    return {
        "iss": EXPECTED_ISSUER,
        "aud": CLIENT_ID,
        "sub": "google-subject-1",
        "email": "invited@example.test",
        "email_verified": True,
        "name": "Invited User",
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
    }


def _signed_token(private_pem: bytes, kid: str, claims: dict) -> str:
    return jwt.encode(claims, private_pem, algorithm="RS256", headers={"kid": kid})


@pytest.fixture()
def google_config(tmp_path, monkeypatch):
    secret_file = tmp_path / "google-login-secret"
    secret_file.write_text("test-client-secret-value", encoding="utf-8")
    monkeypatch.setattr(settings, "google_login_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_login_client_secret_file", str(secret_file))
    monkeypatch.setattr(
        settings,
        "google_login_redirect_uri",
        "https://audiofeel.example/api/auth/google/callback",
    )
    monkeypatch.setattr(settings, "google_login_clock_skew_seconds", 30)
    monkeypatch.setattr(settings, "google_login_max_token_age_seconds", 600)
    google_login.clear_google_oidc_cache()
    yield
    google_login.clear_google_oidc_cache()


def _validate(monkeypatch, private_pem, public_jwk, claims, *, access_token=None):
    monkeypatch.setattr(google_login, "_jwks", lambda **_: {public_jwk["kid"]: public_jwk})
    return google_login.validate_google_id_token(
        _signed_token(private_pem, public_jwk["kid"], claims),
        google_access_token=access_token,
        expected_nonce_hash=keyed_digest("google-nonce-v1", "login-nonce"),
        attempt_created_at=utcnow() - timedelta(seconds=2),
    )


def test_oidc_claims_signature_audience_nonce_and_verified_email(
    google_config, monkeypatch
):
    private_pem, public_jwk = _key_pair("current-key")

    identity = _validate(monkeypatch, private_pem, public_jwk, _claims())

    assert identity.sub == "google-subject-1"
    assert identity.email == "invited@example.test"
    assert identity.display_name == "Invited User"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(iss="https://accounts.example.invalid"),
        lambda value: value.update(aud="another-client"),
        lambda value: value.update(azp="another-client"),
        lambda value: value.update(aud=[CLIENT_ID, "another-client"]),
        lambda value: value.update(nonce="wrong-nonce"),
        lambda value: value.update(email_verified=False),
        lambda value: value.update(email_verified="true"),
        lambda value: value.update(iat=True),
        lambda value: value.update(iat=int((datetime.now(UTC) - timedelta(hours=1)).timestamp())),
        lambda value: value.update(iat=int((datetime.now(UTC) + timedelta(minutes=2)).timestamp())),
        lambda value: value.update(exp=int((datetime.now(UTC) - timedelta(minutes=2)).timestamp())),
    ],
)
def test_oidc_rejects_invalid_security_claims(google_config, monkeypatch, mutate):
    private_pem, public_jwk = _key_pair("claim-key")
    claims = _claims()
    mutate(claims)

    with pytest.raises(GoogleIdentityError):
        _validate(monkeypatch, private_pem, public_jwk, claims)


def test_oidc_accepts_multiple_audiences_only_with_matching_azp(
    google_config, monkeypatch
):
    private_pem, public_jwk = _key_pair("multi-audience-key")
    claims = _claims()
    claims.update(aud=[CLIENT_ID, "another-client"], azp=CLIENT_ID)

    identity = _validate(monkeypatch, private_pem, public_jwk, claims)

    assert identity.sub == "google-subject-1"


def test_oidc_code_flow_validates_optional_at_hash_with_ephemeral_access_token(
    google_config, monkeypatch
):
    private_pem, public_jwk = _key_pair("optional-at-hash-key")
    claims = _claims()
    access_token = "ephemeral-google-access-token"
    claims["at_hash"] = base64.urlsafe_b64encode(
        hashlib.sha256(access_token.encode()).digest()[:16]
    ).decode().rstrip("=")

    identity = _validate(
        monkeypatch,
        private_pem,
        public_jwk,
        claims,
        access_token=access_token,
    )

    assert identity.sub == "google-subject-1"

    with pytest.raises(GoogleIdentityError):
        _validate(
            monkeypatch,
            private_pem,
            public_jwk,
            claims,
            access_token="different-access-token",
        )


def test_oidc_rejects_wrong_signature(google_config, monkeypatch):
    trusted_private, trusted_public = _key_pair("trusted-key")
    attacker_private, _ = _key_pair("attacker-key")
    del trusted_private
    monkeypatch.setattr(
        google_login,
        "_jwks",
        lambda **_: {trusted_public["kid"]: trusted_public},
    )
    token = _signed_token(attacker_private, trusted_public["kid"], _claims())

    with pytest.raises(GoogleIdentityError):
        google_login.validate_google_id_token(
            token,
            expected_nonce_hash=keyed_digest("google-nonce-v1", "login-nonce"),
            attempt_created_at=utcnow() - timedelta(seconds=2),
        )


class _Response:
    def __init__(self, payload, *, cache_control="max-age=3600"):
        self._payload = payload
        self.headers = {"cache-control": cache_control}

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_jwks_unknown_kid_forces_one_rotation_then_uses_cache(
    google_config, monkeypatch
):
    _, old_public = _key_pair("old-key")
    new_private, new_public = _key_pair("new-key")
    certificate_calls = 0

    def fake_get(url, **kwargs):
        nonlocal certificate_calls
        assert kwargs["follow_redirects"] is False
        if url == google_login.DISCOVERY_URL:
            return _Response(
                {
                    "issuer": EXPECTED_ISSUER,
                    "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth",
                    "token_endpoint": "https://oauth2.googleapis.com/token",
                    "jwks_uri": "https://www.googleapis.com/oauth2/v3/certs",
                    "code_challenge_methods_supported": ["S256"],
                }
            )
        assert url == "https://www.googleapis.com/oauth2/v3/certs"
        certificate_calls += 1
        return _Response(
            {"keys": [old_public if certificate_calls == 1 else new_public]}
        )

    monkeypatch.setattr(google_login.httpx, "get", fake_get)
    token = _signed_token(new_private, "new-key", _claims())
    kwargs = {
        "expected_nonce_hash": keyed_digest("google-nonce-v1", "login-nonce"),
        "attempt_created_at": utcnow() - timedelta(seconds=2),
    }

    assert google_login.validate_google_id_token(token, **kwargs).sub
    assert google_login.validate_google_id_token(token, **kwargs).sub
    assert certificate_calls == 2


def test_begin_login_persists_only_hashes_and_encrypted_pkce(
    db, google_config, monkeypatch
):
    monkeypatch.setattr(
        google_login,
        "_metadata",
        lambda **_: {
            "authorization_endpoint": "https://accounts.google.com/o/oauth2/v2/auth"
        },
    )

    authorization = google_login.begin_google_login(db)
    query = parse_qs(urlsplit(authorization.url).query)
    attempt = db.query(GoogleLoginAttempt).one()
    verifier = open_login_value(
        attempt.id,
        attempt.pkce_ciphertext,
        attempt.pkce_nonce,
        attempt.key_id,
    )
    expected_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")

    assert query["scope"] == ["openid email profile"]
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [expected_challenge]
    assert query["nonce"][0]
    assert attempt.state_hash == keyed_digest("google-state-v1", query["state"][0])
    assert attempt.browser_binding_hash == keyed_digest(
        "google-binding-v1", authorization.binding
    )
    assert query["state"][0].encode() not in attempt.state_hash
    assert verifier not in attempt.pkce_ciphertext


def test_state_binding_is_single_use(db):
    state = "state-value"
    binding = "binding-value"
    attempt_id = str(uuid.uuid4())
    ciphertext, nonce, auth_key_id = seal_login_value(attempt_id, "pkce-verifier")
    db.add(
        GoogleLoginAttempt(
            id=attempt_id,
            state_hash=keyed_digest("google-state-v1", state),
            browser_binding_hash=keyed_digest("google-binding-v1", binding),
            nonce_hash=keyed_digest("google-nonce-v1", "nonce"),
            pkce_ciphertext=ciphertext,
            pkce_nonce=nonce,
            key_id=auth_key_id,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=5),
        )
    )
    db.commit()

    with pytest.raises(GoogleLoginStateError):
        google_login._consume_attempt(db, state, "wrong-binding")
    google_login._consume_attempt(db, state, binding)
    with pytest.raises(GoogleLoginStateError):
        google_login._consume_attempt(db, state, binding)


def test_expired_state_is_rejected(db):
    state, binding = _login_attempt(db, "expired")
    attempt = db.query(GoogleLoginAttempt).one()
    attempt.expires_at = utcnow() - timedelta(seconds=1)
    db.commit()

    with pytest.raises(GoogleLoginStateError):
        google_login._consume_attempt(db, state, binding)


def _login_attempt(db, suffix: str):
    state = f"state-{suffix}"
    binding = f"binding-{suffix}"
    attempt_id = str(uuid.uuid4())
    ciphertext, nonce, auth_key_id = seal_login_value(attempt_id, f"verifier-{suffix}")
    db.add(
        GoogleLoginAttempt(
            id=attempt_id,
            state_hash=keyed_digest("google-state-v1", state),
            browser_binding_hash=keyed_digest("google-binding-v1", binding),
            nonce_hash=keyed_digest("google-nonce-v1", f"nonce-{suffix}"),
            pkce_ciphertext=ciphertext,
            pkce_nonce=nonce,
            key_id=auth_key_id,
            created_at=utcnow(),
            expires_at=utcnow() + timedelta(minutes=5),
        )
    )
    db.commit()
    return state, binding


def test_invitation_binding_uses_email_once_then_stable_google_sub(db, monkeypatch):
    invited = User(
        email="Invited@Example.Test",
        email_key="invited@example.test",
        role=UserRole.user,
        state=UserState.pending,
        is_bootstrap_owner=False,
        created_at=utcnow(),
    )
    db.add(invited)
    db.commit()
    monkeypatch.setattr(
        google_login,
        "_exchange_code",
        lambda code, verifier: google_login.GoogleTokenResponse(
            id_token="id-token", access_token="access-token"
        ),
    )
    identities = iter(
        [
            GoogleIdentity("stable-google-sub", "invited@example.test", "First Name"),
            GoogleIdentity("stable-google-sub", "changed@example.test", "New Name"),
        ]
    )
    monkeypatch.setattr(
        google_login,
        "validate_google_id_token",
        lambda *args, **kwargs: next(identities),
    )

    first = google_login.complete_google_login(
        db,
        state=_login_attempt(db, "first")[0],
        binding="binding-first",
        code="code",
    )
    second_state, second_binding = _login_attempt(db, "second")
    second = google_login.complete_google_login(
        db,
        state=second_state,
        binding=second_binding,
        code="code",
    )

    assert first.id == second.id
    assert second.google_sub == "stable-google-sub"
    assert second.email == "Invited@Example.Test"
    assert second.display_name == "New Name"
    assert second.state == UserState.active
    assert db.query(User).count() == 1


def test_uninvited_google_account_does_not_create_user(db, monkeypatch):
    state, binding = _login_attempt(db, "uninvited")
    monkeypatch.setattr(
        google_login,
        "_exchange_code",
        lambda code, verifier: google_login.GoogleTokenResponse(
            id_token="id-token", access_token="access-token"
        ),
    )
    monkeypatch.setattr(
        google_login,
        "validate_google_id_token",
        lambda *args, **kwargs: GoogleIdentity(
            "unknown-google-sub", "unknown@example.test", None
        ),
    )

    with pytest.raises(GoogleAccountNotInvited):
        google_login.complete_google_login(
            db, state=state, binding=binding, code="code"
        )

    assert db.query(User).count() == 0
    with pytest.raises(GoogleLoginStateError):
        google_login.complete_google_login(
            db, state=state, binding=binding, code="code"
        )


def test_callback_rotates_to_secure_server_cookie_without_logging_identity(
    api_client, owner_user, monkeypatch, caplog
):
    from app.api import auth as auth_api

    monkeypatch.setattr(settings, "auth_cookie_secure", True)
    monkeypatch.setattr(
        auth_api,
        "complete_google_login",
        lambda *args, **kwargs: owner_user,
    )
    api_client.cookies.set(
        "audiofeel_oidc",
        "browser-binding",
        path="/api/auth/google/callback",
    )
    with caplog.at_level(logging.DEBUG):
        response = api_client.get(
            "/api/auth/google/callback?state=opaque-state&code=one-time-code",
            follow_redirects=False,
        )

    cookie = response.headers["set-cookie"]
    assert response.status_code == 303
    assert response.headers["location"] == "/#/playlists"
    assert "audiofeel_session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/api" in cookie
    assert "one-time-code" not in caplog.text
    assert (owner_user.email or "not-present") not in caplog.text


def test_callback_rejects_oversized_code_without_echoing_it(api_client):
    marker = "sensitive-code-marker"
    api_client.cookies.set(
        "audiofeel_oidc",
        "browser-binding",
        path="/api/auth/google/callback",
    )

    response = api_client.get(
        "/api/auth/google/callback",
        params={"state": "opaque-state", "code": marker + "x" * 4096},
    )

    assert response.status_code == 400
    assert marker not in response.text


def test_token_exchange_uses_pkce_and_does_not_log_or_return_access_token(
    google_config, monkeypatch, caplog
):
    captured = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs["data"])
        return _Response(
            {
                "id_token": "verified-id-token-placeholder",
                "access_token": "must-not-be-used-or-logged",
            }
        )

    monkeypatch.setattr(
        google_login,
        "_metadata",
        lambda **_: {"token_endpoint": "https://oauth2.googleapis.com/token"},
    )
    monkeypatch.setattr(google_login.httpx, "post", fake_post)
    with caplog.at_level(logging.DEBUG):
        result = google_login._exchange_code("one-time-code", "pkce-verifier")

    assert result.id_token == "verified-id-token-placeholder"
    assert result.access_token == "must-not-be-used-or-logged"
    assert "verified-id-token-placeholder" not in repr(result)
    assert "must-not-be-used-or-logged" not in repr(result)
    assert captured["code_verifier"] == "pkce-verifier"
    assert captured["grant_type"] == "authorization_code"
    assert "one-time-code" not in caplog.text
    assert "must-not-be-used-or-logged" not in caplog.text


def test_login_and_drive_callbacks_are_distinct():
    assert settings.google_login_redirect_uri.endswith("/api/auth/google/callback")
    assert settings.google_drive_redirect_uri.endswith("/api/storage/google/callback")
    assert settings.google_login_redirect_uri != settings.google_drive_redirect_uri
