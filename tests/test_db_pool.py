"""Pool settings that keep a suspended Neon compute from breaking a request.

Neon suspends an idle compute after about five minutes, closing the
connections the pool still holds. The next request checked out a dead socket
and failed with "SSL connection has been closed unexpectedly" on whatever
statement touched it first -- which, since the RLS scope went in, is the
set_config in _scope_connection.

These assert the configuration rather than the reconnect, which needs a real
PostgreSQL server; the reconnect was verified by hand against Neon by closing
a pooled connection's socket behind the pool's back (it raises
OperationalError with pool_pre_ping off, and recovers with it on).
"""

from app import create_app

POSTGRES = "postgresql://u:p@example.neon.tech/db"


def _options(uri):
    return create_app({"SQLALCHEMY_DATABASE_URI": uri}).config.get(
        "SQLALCHEMY_ENGINE_OPTIONS") or {}


def test_postgresql_validates_connections_on_checkout():
    assert _options(POSTGRES)["pool_pre_ping"] is True


def test_postgresql_recycles_before_the_suspend_window():
    """Neon's default scale-to-zero is 300s; recycling must beat it."""
    assert 0 < _options(POSTGRES)["pool_recycle"] < 300


def test_in_memory_sqlite_is_left_alone():
    """Recycling would swap the schema for an empty database.

    An in-memory SQLite database lives inside its connection, so discarding
    that connection discards every table with it -- which would make the
    whole test suite fail in ways that look nothing like a pool setting.
    """
    options = _options("sqlite:///:memory:")
    assert "pool_recycle" not in options
    assert "pool_pre_ping" not in options


def test_an_explicit_setting_is_not_overridden():
    """The defaults must not fight a deployment that tuned them."""
    app = create_app({
        "SQLALCHEMY_DATABASE_URI": POSTGRES,
        "SQLALCHEMY_ENGINE_OPTIONS": {"pool_recycle": 60},
    })
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_recycle"] == 60
    assert app.config["SQLALCHEMY_ENGINE_OPTIONS"]["pool_pre_ping"] is True
