"""The migrations must describe the models.

Without this, changing model.py and forgetting `flask db migrate` passes every
other test -- they build the schema from the models directly -- and then fails
on a real database with "no such column". That is exactly how the local
database ended up one schema behind.
"""

import pathlib

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from flask_migrate import upgrade

from app import create_app, db

MIGRATIONS = pathlib.Path(__file__).resolve().parent.parent / "migrations"


@pytest.fixture
def migrated_app(tmp_path):
    """An app whose database was built by running the migrations."""
    application = create_app({
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'migrated.sqlite'}",
        "TESTING": True,
    })
    with application.app_context():
        upgrade(directory=str(MIGRATIONS))
        yield application


def test_the_migrations_apply_cleanly(migrated_app):
    from sqlalchemy import inspect

    tables = set(inspect(db.engine).get_table_names())
    assert "alembic_version" in tables
    # The design doc's 21 tables, plus Alembic's own.
    assert len(tables - {"alembic_version"}) == 21


def test_no_model_change_is_missing_a_migration(migrated_app):
    """compare_metadata returns the diff Alembic would autogenerate now."""
    with db.engine.connect() as connection:
        context = MigrationContext.configure(connection)
        diff = compare_metadata(context, db.metadata)

    assert diff == [], (
        "model.py and the migrations disagree. Run:\n"
        "  flask --app app db migrate -m '<what changed>'\n"
        "  flask --app app db upgrade\n"
        f"Outstanding differences: {diff}"
    )


def test_the_one_admin_index_survives_the_migration(migrated_app):
    """FR-ACE-007's guarantee has to exist in a migrated database, not only in
    one built by create_all."""
    from sqlalchemy import inspect

    indexes = inspect(db.engine).get_indexes("activity_memberships")
    admin_index = next(i for i in indexes if i["name"] == "uq_activity_single_admin")
    assert admin_index["unique"]


def test_the_pending_request_index_survives_the_migration(migrated_app):
    """FR-PE-002."""
    from sqlalchemy import inspect

    indexes = inspect(db.engine).get_indexes("join_requests")
    assert any(i["name"] == "uq_join_request_pending" and i["unique"] for i in indexes)


def test_the_rls_migration_is_a_no_op_on_sqlite(migrated_app):
    """Row-level security is PostgreSQL-only, so the SQLite path must skip it
    rather than fail -- the whole suite runs on SQLite."""
    from sqlalchemy import inspect

    # It applied cleanly (the fixture ran upgrade) and created no extra tables.
    tables = set(inspect(db.engine).get_table_names())
    assert len(tables - {"alembic_version"}) == 21


def test_both_revisions_are_applied(migrated_app):
    head = db.session.execute(db.text("select version_num from alembic_version")).scalar()
    assert head == "4de96d5e2e95"
