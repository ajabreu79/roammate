"""Contract tests for the real Clerk adapter.

tests/test_auth.py stubs ClerkAdapter so the flows can be tested without
credentials -- which means nothing there would notice if the SDK's surface
moved underneath us. These tests exercise the adapter itself against a fake
client, and assert the SDK symbols we call actually exist.
"""

import inspect
from types import SimpleNamespace

import pytest

from app import create_app, providers


@pytest.fixture
def app():
    application = create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "TESTING": True,
        "CLERK_SECRET_KEY": "sk_test_stub",
    })
    with application.app_context():
        from app import db
        db.create_all()
        yield application


def fake_clerk_user(verified=True, email="student@case.edu"):
    verification = SimpleNamespace(status="verified" if verified else "unverified")
    address = SimpleNamespace(id="idn_1", email_address=email, verification=verification)
    return SimpleNamespace(
        id="user_1",
        first_name="Ada",
        last_name="Lovelace",
        image_url="https://img.clerk.com/ada",
        primary_email_address_id="idn_1",
        email_addresses=[address],
    )


# --- the SDK surface we depend on --------------------------------------------

def test_authenticate_request_options_accepts_the_fields_we_pass():
    from clerk_backend_api.security import AuthenticateRequestOptions

    fields = set(AuthenticateRequestOptions.__dataclass_fields__)
    assert {"secret_key", "authorized_parties"} <= fields


def test_sdk_methods_take_the_keyword_arguments_we_use():
    from clerk_backend_api import Clerk

    client = Clerk(bearer_auth="sk_test_stub")
    assert "user_id" in inspect.signature(client.users.get).parameters
    assert "user_id" in inspect.signature(client.users.delete).parameters
    assert "session_id" in inspect.signature(client.sessions.revoke).parameters
    # scripts/check_clerk.py calls users.list(request={"limit": 1})
    assert "request" in inspect.signature(client.users.list).parameters


# --- authenticate ------------------------------------------------------------

def test_authenticate_passes_the_request_and_secret_key(app):
    captured = {}

    class FakeClient:
        def authenticate_request(self, request, options):
            # Read the headers here: outside the request context the Flask
            # request proxy is unbound.
            captured["cookie"] = request.headers.get("cookie")
            captured["options"] = options
            return SimpleNamespace(is_signed_in=True, payload={"sub": "user_1", "sid": "sess_1"})

    app.extensions["clerk"] = FakeClient()

    with app.test_request_context("/", headers={"Cookie": "__session=jwt"}):
        from flask import request
        payload = providers.CLERK.authenticate(request)

    assert payload == {"sub": "user_1", "sid": "sess_1"}
    # The SDK reads the session token off `.headers`; nothing else is required.
    assert captured["cookie"] == "__session=jwt"
    assert captured["options"].secret_key == "sk_test_stub"


def test_authenticate_returns_none_when_not_signed_in(app):
    class FakeClient:
        def authenticate_request(self, request, options):
            return SimpleNamespace(is_signed_in=False, payload=None)

    app.extensions["clerk"] = FakeClient()

    with app.test_request_context("/"):
        from flask import request
        assert providers.CLERK.authenticate(request) is None


def test_authenticate_raises_provider_unavailable_without_a_client(app):
    app.extensions.pop("clerk", None)

    with app.test_request_context("/"):
        from flask import request
        with pytest.raises(providers.ProviderUnavailable):
            providers.CLERK.authenticate(request)


# --- fetch_identity ----------------------------------------------------------

def test_fetch_identity_maps_the_primary_email(app):
    app.extensions["clerk"] = SimpleNamespace(
        users=SimpleNamespace(get=lambda user_id: fake_clerk_user()))

    identity = providers.CLERK.fetch_identity("user_1")
    assert identity == {
        "provider_user_id": "user_1",
        "email": "student@case.edu",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email_verified": True,
        "image_url": "https://img.clerk.com/ada",
    }


def test_fetch_identity_reports_an_unverified_address(app):
    app.extensions["clerk"] = SimpleNamespace(
        users=SimpleNamespace(get=lambda user_id: fake_clerk_user(verified=False)))

    assert providers.CLERK.fetch_identity("user_1")["email_verified"] is False


def test_fetch_identity_normalizes_the_email(app):
    app.extensions["clerk"] = SimpleNamespace(
        users=SimpleNamespace(get=lambda user_id: fake_clerk_user(email="  Ada@CASE.edu ")))

    assert providers.CLERK.fetch_identity("user_1")["email"] == "ada@case.edu"


def test_fetch_identity_returns_none_for_an_unknown_user(app):
    app.extensions["clerk"] = SimpleNamespace(users=SimpleNamespace(get=lambda user_id: None))
    assert providers.CLERK.fetch_identity("nobody") is None


# --- revoke / delete ---------------------------------------------------------

def test_revoke_session_calls_the_sdk(app):
    calls = []
    app.extensions["clerk"] = SimpleNamespace(
        sessions=SimpleNamespace(revoke=lambda session_id: calls.append(session_id)))

    assert providers.CLERK.revoke_session("sess_1") is True
    assert calls == ["sess_1"]
    # A request with no session id must not call Clerk at all.
    assert providers.CLERK.revoke_session(None) is False
    assert calls == ["sess_1"]


def test_delete_identity_calls_the_sdk(app):
    calls = []
    app.extensions["clerk"] = SimpleNamespace(
        users=SimpleNamespace(delete=lambda user_id: calls.append(user_id)))

    assert providers.CLERK.delete_identity("user_1") is True
    assert calls == ["user_1"]


# --- regression ---------------------------------------------------------------

def test_anonymous_request_redirects_instead_of_erroring(app):
    """With the real adapter and no session cookie, a protected route must
    redirect rather than raise -- the failure mode when the adapter called an
    SDK import path that no longer existed."""
    response = app.test_client().get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/signup")


def test_anonymous_request_redirects_when_clerk_is_not_configured(app):
    app.config["CLERK_SECRET_KEY"] = None
    app.extensions.pop("clerk", None)

    response = app.test_client().get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/signup")
