"""FR-AUTH-009: logging out must actually sign the browser out.

The bug these cover: SessionManager.end() revoked the session at Clerk and
cleared our own cookie, but nothing removed Clerk's __session cookie. The
adapter verifies that JWT against Clerk's JWKS -- a local cryptographic check
that cannot see a revoked session -- so the token kept authenticating for the
rest of its minute-long life. The redirect to /auth/login then found a
signed-in user and sent them back to the dashboard, so logging out appeared to
do nothing, and only "worked" when the token happened to expire first.

The existing suite could not catch it: conftest stubs `authenticate` to
return a payload while a flag is set, and logout never changed that flag. So
these tests keep the stub signed in on purpose -- that is the real situation,
a browser still holding a valid token -- and assert that logout de-authorises
it anyway.
"""

import flask_login

from app import auth, model
from app.sessions import SessionManager

from .conftest import complete_profile_form


def _register(client):
    client.post("/auth/signup2", data=complete_profile_form())


def test_logout_redirects_to_login(client):
    _register(client)
    response = client.post("/auth/logout")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")


def test_logout_expires_clerks_cookies(client):
    """The fix: the response must tell the browser to drop them.

    Keeping only the server-side revoke is what made logout a no-op, because
    the adapter never asks Clerk whether a session is still live.
    """
    _register(client)
    client.set_cookie("__session", "stale.jwt.value")
    client.set_cookie("__client_uat", "1792715938")
    client.set_cookie("__session_VAihZPV5", "stale.suffixed.value")

    response = client.post("/auth/logout")
    cleared = response.headers.getlist("Set-Cookie")
    for name in ("__session", "__client_uat", "__session_VAihZPV5"):
        assert any(header.startswith(f"{name}=") and "Expires=Thu, 01 Jan 1970" in header
                   for header in cleared), f"{name} was not expired: {cleared}"


def test_logout_leaves_unrelated_cookies_alone(client):
    _register(client)
    client.set_cookie("theme", "dark")
    response = client.post("/auth/logout")
    assert not any(h.startswith("theme=") for h in response.headers.getlist("Set-Cookie"))


def test_logout_clears_our_own_session(client, app):
    _register(client)
    with client.session_transaction() as session:
        assert session.get("sid")
    client.post("/auth/logout")
    with client.session_transaction() as session:
        assert "sid" not in session
        assert "uni" not in session


def test_logout_revokes_the_session_at_clerk(client, clerk_calls):
    _register(client)
    client.post("/auth/logout")
    assert clerk_calls["revoked"] == ["sess_1"]


def test_logout_works_from_an_already_signed_out_browser(signed_out, client):
    """session.js signs out of Clerk first, so this POST arrives anonymous.

    Gating the endpoint sent that request to the sign-up page instead, which
    left our own cookie in place.
    """
    response = client.post("/auth/logout")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/login")


def test_logout_is_not_a_protected_route(app):
    """Guards the PUBLIC_ENDPOINTS entry the test above depends on."""
    assert "auth.logout_post" in auth.PUBLIC_ENDPOINTS


def test_logout_is_post_only(client):
    """A GET logout can be triggered by any page embedding it as an image."""
    _register(client)
    assert client.get("/auth/logout").status_code == 405
