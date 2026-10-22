#!/usr/bin/env python
"""Create the non-owner database role the application connects as.

    APP_DB_PASSWORD=... .venv/bin/python scripts/setup_app_role.py --write-env

Without --write-env the connection URL is only printed, and DATABASE_URL has
to be updated by hand. Note that `neon env pull` -- which `neon link` and
`neon checkout` both run -- resets DATABASE_URL to the owner role, so this
script needs re-running after a branch switch.

Why this is not a migration: a role needs a password, and a migration file is
committed. The role is also cluster-level rather than schema-level, so it is
environment setup, not schema history.

RLS never applies to a table's owner, and Neon's generated owner role also has
BYPASSRLS. Connecting as that role makes the policies from migration
4de96d5e2e95 inert, which is the state this script fixes: it creates a role
with neither property, so NFR-SECU-010's isolation is enforced by the database
for application traffic while migrations keep running as the owner.

Idempotent: re-running rotates the role's password, which invalidates any
previously issued URL.
"""

import os
import pathlib
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv                  # noqa: E402

load_dotenv()

from app import create_app, db                  # noqa: E402

ROLE = os.environ.get("APP_DB_ROLE", "roammate_app")

# Tables the application writes. The remaining tables are reference data or
# infrastructure and get narrower grants below.
WRITABLE = (
    "users", "user_preferences", "external_links", "follows",
    "activities", "activity_destinations", "activity_memberships",
    "finalized_fields", "join_requests", "waitlist_entries", "invitations",
    "saved_activities", "forum_topics", "messages", "message_mentions",
    "notifications", "outbox_events", "verification_tokens",
)
READ_ONLY = ("universities", "activity_types", "alembic_version")
# NFR-SECU-012: the audit log is append-only, so the application gets INSERT
# and SELECT and nothing else.
APPEND_ONLY = ("audit_logs",)


def write_env(app_url, path=".env"):
    """Replace DATABASE_URL in `path`, leaving every other line untouched."""
    import shutil

    target = pathlib.Path(path)
    if not target.exists():
        print(f"{path} does not exist; set DATABASE_URL yourself.")
        return False

    shutil.copyfile(target, f"{path}.bak")
    lines = target.read_text().splitlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.startswith("DATABASE_URL="):
            lines[i] = f"DATABASE_URL={app_url}"
            replaced = True
    if not replaced:
        lines.append(f"DATABASE_URL={app_url}")

    target.write_text("\n".join(lines) + "\n")
    print(f"Updated DATABASE_URL in {path} (previous copy at {path}.bak).")
    return True


def main():
    write = "--write-env" in sys.argv
    password = os.environ.get("APP_DB_PASSWORD")
    if not password:
        print("APP_DB_PASSWORD is required. Generate one with:")
        print("  python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
        return 2

    app = create_app()
    with app.app_context():
        if db.engine.dialect.name != "postgresql":
            print("Not a PostgreSQL database; row-level security does not apply here.")
            return 2

        quoted = password.replace("'", "''")
        statements = [
            # CREATE ROLE is not IF NOT EXISTS in PostgreSQL.
            f"""
            DO $$ BEGIN
                IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{ROLE}') THEN
                    CREATE ROLE {ROLE} LOGIN;
                END IF;
            END $$;
            """,
            # Only the password and LOGIN are set here. NOSUPERUSER and
            # NOBYPASSRLS are the defaults for a new role and can only be
            # *changed* by a superuser, which Neon's owner role is not. The
            # assertion below verifies the role really lacks both.
            f"ALTER ROLE {ROLE} WITH LOGIN PASSWORD '{quoted}'",
            # The database name can contain characters that need quoting
            # (Neon's default is roammate-db).
            f'GRANT CONNECT ON DATABASE "{db.engine.url.database}" TO {ROLE}',
            f"GRANT USAGE ON SCHEMA public TO {ROLE}",
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON {', '.join(WRITABLE)} TO {ROLE}",
            f"GRANT SELECT ON {', '.join(READ_ONLY)} TO {ROLE}",
            f"GRANT SELECT, INSERT ON {', '.join(APPEND_ONLY)} TO {ROLE}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {ROLE}",
            # Future tables created by the owner stay reachable without a
            # second trip through this script.
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {ROLE}",
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {ROLE}",
        ]
        for statement in statements:
            db.session.execute(db.text(statement))
        db.session.commit()

        checks = db.session.execute(db.text(
            "SELECT rolsuper, rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = :r"
        ), {"r": ROLE}).one()
        print(f"role {ROLE}: superuser={checks[0]} bypassrls={checks[1]} canlogin={checks[2]}")
        if checks[0] or checks[1]:
            print("ERROR: the role can still bypass row-level security.")
            return 1

        # Build the application URL on the POOLED host, which is what
        # DATABASE_URL already points at, so the role swap does not also
        # change the endpoint.
        pooled = urllib.parse.urlparse(os.environ["DATABASE_URL"])
        secret = urllib.parse.quote(password, safe="")
        app_url = (f"postgresql://{ROLE}:{secret}@{pooled.hostname}"
                   f"{pooled.path}?sslmode=require")
        print()
        if write:
            write_env(app_url)
        else:
            print("Set this as DATABASE_URL for application traffic:")
            print("  " + app_url)
            print("  (or re-run with --write-env to have this done for you)")
        print()
        print("DATABASE_URL_UNPOOLED stays on the owner role: migrations have to")
        print("bypass RLS, and running as the owner is what lets them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
