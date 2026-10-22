"""The RLS identity GUC must be in force for every transaction of a request.

The policies in migration fa7678e08668 resolve the requester from
`app.auth_provider_id`, and set_config(..., local=true) binds that to one
transaction. Applying it once per request was therefore not enough: the
commit inside sync_from_provider ended the scoped transaction, so every read
after it ran unscoped, matched no policy, and made the requester's own row
invisible to them -- surfacing as ObjectDeletedError when Flask-SQLAlchemy
refreshed an attribute the commit had expired.

These tests assert the wiring rather than the policies: the suite runs on
SQLite, which has no row-level security, so a real policy test needs a
PostgreSQL database (see README, "Tests").
"""

import pytest
import sqlalchemy

from app import db, services
from app.services import AuthService

from .conftest import complete_profile_form


@pytest.fixture
def scoped(app, monkeypatch):
    """Record every (transaction, identity) the scope was applied to."""
    applied = []
    real = services._scope_connection

    def spy(connection, provider_user_id):
        applied.append(provider_user_id)
        return real(connection, provider_user_id)

    monkeypatch.setattr(services, "_scope_connection", spy)
    return applied


def test_the_scope_is_applied_before_the_first_query(app, scoped, identity):
    with app.test_request_context("/"):
        AuthService.user_from_request(None)
    assert identity["provider_user_id"] in scoped


def test_a_commit_does_not_leave_later_transactions_unscoped(app, scoped, identity):
    """The regression: one application per request, discarded by the commit."""
    with app.test_request_context("/"):
        AuthService.user_from_request(None)   # commits inside
        before = len(scoped)
        db.session.commit()                   # ends the scoped transaction
        db.session.execute(db.select(1))      # opens a fresh one
        assert len(scoped) > before, "new transaction ran unscoped"
        assert scoped[-1] == identity["provider_user_id"]


def test_reading_an_expired_attribute_after_the_commit_still_works(client):
    """Smoke test for the path that failed: commit, then render.

    It cannot fail for the original reason -- SQLite enforces no policy, so
    the row stays visible to an unscoped read. The regression is pinned by
    test_a_commit_does_not_leave_later_transactions_unscoped above.
    """
    response = client.post("/auth/signup2", data=complete_profile_form())
    assert response.status_code in (200, 302)
    response = client.get("/")
    assert response.status_code == 200


def test_the_listener_is_registered_on_the_session(app):
    assert sqlalchemy.event.contains(
        db.session, "after_begin", services._scope_every_transaction)


def test_no_scope_is_applied_without_an_identity(app, scoped):
    """An anonymous request must not publish anyone's identity."""
    with app.test_request_context("/"):
        db.session.execute(db.select(1))
    assert scoped == []
