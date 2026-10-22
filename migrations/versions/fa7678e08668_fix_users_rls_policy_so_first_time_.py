"""fix users RLS policy so first-time registration works

Migration 4de96d5e2e95 gave `users` the policy
`university_id = app_current_university_id()`. That cannot admit a new row:

  - FR-AUTH-001 collects university in step 2, so sync_from_provider inserts
    the row with university_id NULL.
  - app_current_university_id() resolves the requester by looking them up in
    `users`, and on a first sign-in there is no row yet, so it returns NULL.
  - The INSERT's WITH CHECK then evaluates NULL = NULL, which is not true.

Result: "new row violates row-level security policy for table users" on every
first sign-in. Registration was impossible.

The fix is to key a user's access to their own row on the identity they
already proved -- the Clerk subject in the GUC -- rather than on a university
they have not chosen yet. The single FOR ALL policy is also split per command,
because the original let any user UPDATE or DELETE any peer at their own
university; only SELECT needs to reach peers.

University and birthday immutability (FR-PROF-002, FR-PROF-004) cannot be a
policy: WITH CHECK sees only the new row, so it cannot tell a first-time
assignment from a later change. That needs a trigger, which this migration
also adds -- so the constraint now holds even if a controller forgets it.

Revision ID: fa7678e08668
Revises: 4de96d5e2e95
"""
from alembic import op

revision = 'fa7678e08668'
down_revision = '4de96d5e2e95'
branch_labels = None
depends_on = None

# The identity the requester proved, straight from the GUC. Does not touch
# `users`, so it works before the row exists.
OWN_ROW = "auth_provider_id = nullif(current_setting('app.auth_provider_id', true), '')"
SAME_UNIVERSITY = "university_id = app_current_university_id()"


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP POLICY IF EXISTS users_university_isolation ON users;")

    # Read your own row, and your university's other members (FR-PROF-001).
    op.execute(
        f"CREATE POLICY users_select ON users FOR SELECT "
        f"USING ({OWN_ROW} OR {SAME_UNIVERSITY});"
    )
    # Create only your own row, whatever university_id it carries -- the
    # auth_provider_id must match the verified subject, so no one can insert a
    # row on anyone else's behalf.
    op.execute(f"CREATE POLICY users_insert ON users FOR INSERT WITH CHECK ({OWN_ROW});")
    # Change and remove only your own row. The previous FOR ALL policy allowed
    # both against any peer at the same university.
    op.execute(f"CREATE POLICY users_update ON users FOR UPDATE "
               f"USING ({OWN_ROW}) WITH CHECK ({OWN_ROW});")
    op.execute(f"CREATE POLICY users_delete ON users FOR DELETE USING ({OWN_ROW});")

    # FR-PROF-002 and FR-PROF-004: once set, university and birthday are
    # fixed. Age derives from birthday (FR-PROF-003), so an editable birthday
    # would let a user leave the 18+ check they already passed (FR-AUTH-005),
    # and university is the FR-PROF-009 isolation boundary.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION users_reject_immutable_changes() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.university_id IS NOT NULL
               AND NEW.university_id IS DISTINCT FROM OLD.university_id THEN
                RAISE EXCEPTION
                    'university_id cannot be changed once set (FR-PROF-002)';
            END IF;
            IF OLD.birthday IS NOT NULL
               AND NEW.birthday IS DISTINCT FROM OLD.birthday THEN
                RAISE EXCEPTION
                    'birthday cannot be changed once set (FR-PROF-004)';
            END IF;
            RETURN NEW;
        END $$;
        """
    )
    op.execute(
        "CREATE TRIGGER users_immutable_fields BEFORE UPDATE ON users "
        "FOR EACH ROW EXECUTE FUNCTION users_reject_immutable_changes();"
    )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("DROP TRIGGER IF EXISTS users_immutable_fields ON users;")
    op.execute("DROP FUNCTION IF EXISTS users_reject_immutable_changes();")
    for name in ("users_select", "users_insert", "users_update", "users_delete"):
        op.execute(f"DROP POLICY IF EXISTS {name} ON users;")
    op.execute(
        f"CREATE POLICY users_university_isolation ON users "
        f"FOR ALL USING ({SAME_UNIVERSITY}) WITH CHECK ({SAME_UNIVERSITY});"
    )
