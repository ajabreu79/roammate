import datetime

import pytest
from flask import g

from app import create_app, db as _db, providers, ratelimit, universities


@pytest.fixture
def identity():
    """The Clerk record our stubbed adapter will return. Mutate per test."""
    return {
        "provider_user_id": "user_clerk_1",
        "email": "student@case.edu",
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email_verified": True,
        "image_url": None,
    }


@pytest.fixture
def clerk_calls():
    return {"revoked": [], "deleted": []}


@pytest.fixture
def app(monkeypatch, identity, clerk_calls, tmp_path):
    application = create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "TESTING": True,
        "CLERK_SECRET_KEY": "sk_test_stub",
        "CLERK_PUBLISHABLE_KEY": "pk_test_stub",
        "PROFILE_PIC_DIR": str(tmp_path / "uploads"),
    })

    # Signed-in state is controlled by this flag rather than by a real cookie.
    state = {"signed_in": True}
    application.config["_SIGNED_IN"] = state

    monkeypatch.setattr(
        providers.ClerkAdapter, "authenticate",
        lambda self, req: {"sub": identity["provider_user_id"], "sid": "sess_1"}
        if state["signed_in"] else None,
    )
    monkeypatch.setattr(
        providers.ClerkAdapter, "fetch_identity",
        lambda self, uid: dict(identity) if uid == identity["provider_user_id"] else None,
    )
    monkeypatch.setattr(
        providers.ClerkAdapter, "revoke_session",
        lambda self, sid: clerk_calls["revoked"].append(sid) or True,
    )
    monkeypatch.setattr(
        providers.ClerkAdapter, "delete_identity",
        lambda self, uid: clerk_calls["deleted"].append(uid) or True,
    )

    @application.teardown_request
    def _reauthenticate_every_request(_exception=None):
        """Make each client request re-run the request loader.

        This fixture holds one app context open for the whole test, and `g`
        lives on the app context -- so Flask reuses it for every client
        request instead of pushing a fresh one, and Flask-Login's cached user
        (`g._login_user`) survives between them. The loader then ran once per
        test rather than once per request, which is nothing like production
        and hid real bugs: `remember_context` was only ever reached on a
        test's first request, when the profile is still incomplete, so it
        never stored a value that could fail to serialise.

        teardown_request rather than before_request: a before_request that
        returns a response short-circuits the rest of the chain, so this
        would be skipped on exactly the requests require_authentication
        redirects -- anonymous, unverified, or mid-registration.
        """
        g.pop("_login_user", None)

    ratelimit.reset()
    with application.app_context():
        # The real schema comes from `flask db upgrade`; tests build it
        # directly from the models so they do not need a migration run.
        _db.create_all()
        universities.seed()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def signed_out(app):
    app.config["_SIGNED_IN"]["signed_in"] = False
    return app


@pytest.fixture
def db():
    return _db


def years_ago(years, days=0):
    today = datetime.date.today()
    try:
        shifted = today.replace(year=today.year - years)
    except ValueError:              # 29 February
        shifted = today.replace(year=today.year - years, day=28)
    return (shifted + datetime.timedelta(days=days)).isoformat()


def complete_profile_form(**overrides):
    form = {
        "gender": "Woman",
        "school_year": "Junior",
        "university": "Case Western Reserve University",
        "birthday": years_ago(22),
        "description": "Looking for a roommate near campus.",
    }
    form.update(overrides)
    return form
