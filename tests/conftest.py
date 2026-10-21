import datetime

import pytest

from app import create_app, db as _db, providers, ratelimit


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

    ratelimit.reset()
    with application.app_context():
        yield application


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
