"""The Flask session is a signed JSON cookie, so what goes in must serialise.

`remember_context` stored `user.university` -- the University row -- which
raised "Object of type University is not JSON serializable" from
save_session, i.e. after the view had already succeeded, on every request by
a user who had finished registration.

The suite did not catch it because the `app` fixture holds one app context
for the whole test; `g` lives there, so Flask-Login's cached user survived
between client requests and the loader ran once per test instead of once per
request. `remember_context` was therefore only ever reached on a test's first
request, when university_id is still None -- and None serialises fine. The
fixture now drops that cache per request; see conftest.
"""

import json

import flask_login

from app import model
from app.sessions import SessionManager

from .conftest import complete_profile_form


def test_the_session_survives_a_request_by_a_registered_user(client):
    """The regression. Fails on save_session, so the view's 200 is not enough."""
    client.post("/auth/signup2", data=complete_profile_form())
    assert client.get("/").status_code == 200


def test_every_session_value_is_json_serializable(client):
    client.post("/auth/signup2", data=complete_profile_form())
    client.get("/")
    with client.session_transaction() as session:
        json.dumps(dict(session))       # raises TypeError on an ORM object


def test_the_cached_scope_is_the_university_id(client, app):
    client.post("/auth/signup2", data=complete_profile_form())
    client.get("/")
    with client.session_transaction() as session:
        cached = session["uni"]
    assert isinstance(cached, int)
    assert not isinstance(cached, model.University)


def test_the_scope_is_none_before_registration_step_two(client):
    client.get("/auth/signup2")
    with client.session_transaction() as session:
        assert session["uni"] is None


def test_the_cached_scope_matches_the_users_row(client, app):
    client.post("/auth/signup2", data=complete_profile_form())
    client.get("/")
    with client.session_transaction() as session:
        cached = session["uni"]
    with app.test_request_context("/"):
        user = flask_login.current_user
        assert cached == user.university_id


def test_the_request_loader_runs_on_every_request(client, monkeypatch):
    """Guards the fixture fix: one loader call per request, not per test.

    Without this the suite silently stops re-authenticating after a test's
    first request, which is what hid the bug above.
    """
    from app import services

    calls = []
    real = services.AuthService.user_from_request
    monkeypatch.setattr(
        services.AuthService, "user_from_request",
        staticmethod(lambda req: (calls.append(1), real(req))[1]),
    )

    # Three protected requests. The loader is lazy -- it runs when
    # current_user is first touched -- so an auth-exempt route like
    # /auth/signup2 legitimately does not trigger it and cannot be counted.
    client.get("/")
    client.get("/")
    client.get("/")
    assert len(calls) == 3, f"loader ran {len(calls)} times for 3 requests"
