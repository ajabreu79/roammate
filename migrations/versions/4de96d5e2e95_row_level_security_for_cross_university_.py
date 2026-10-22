"""row-level security for cross-university isolation

NFR-SECU-010: "Cross-university data isolation (FR-PROF-009) will be enforced
at the query layer, such that no request can return a record belonging to a
university other than the requester's." The SRS inspection finding on this
requirement rejected UI-level filtering precisely because an API call could
bypass it, so the guarantee has to sit below the application.

_university_scoped() in main.py is the application half. This is the half that
holds when a query forgets to call it.

How it works
    app_current_university_id() reads the `app.auth_provider_id` GUC, which the
    application sets per transaction from the verified Clerk token, and resolves
    it to a university. It is SECURITY DEFINER so its own lookup against `users`
    runs as the table owner and therefore bypasses RLS -- without that, the
    policy on `users` would recurse into itself. It is STABLE so the planner
    evaluates it once per query rather than per row.

    With the GUC unset the function returns NULL, every predicate compares
    against NULL and no rows are visible. Unauthenticated access fails closed.

Who it applies to
    RLS never applies to a table's owner. Migrations keep running as the owner,
    and this is inert until the application connects as a non-owner role
    without BYPASSRLS. scripts/setup_app_role.py creates that role.

PostgreSQL only; SQLite has no row-level security, so this is a no-op there and
the SQLite test suite continues to rely on the application-level predicate.

Revision ID: 4de96d5e2e95
Revises: f1bba40f69e7
"""
from alembic import op

revision = '4de96d5e2e95'
down_revision = 'f1bba40f69e7'
branch_labels = None
depends_on = None

UNIVERSITY_FN = "app_current_university_id()"

# Tables carrying university_id directly.
DIRECT = ("users", "activities")

# Tables owned by an activity.
VIA_ACTIVITY = (
    "activity_destinations", "activity_memberships", "finalized_fields",
    "join_requests", "waitlist_entries", "invitations", "saved_activities",
    "forum_topics",
)

# Tables owned by a user.
VIA_USER = ("user_preferences", "external_links", "notifications", "verification_tokens")

# Deliberately left without RLS: `universities` and `activity_types` are shared
# reference data; `outbox_events`, `audit_logs` and `alembic_version` are
# infrastructure the application reaches only through narrow grants.

ACTIVITY_SCOPE = f"activity_id IN (SELECT id FROM activities WHERE university_id = {UNIVERSITY_FN})"
USER_SCOPE = f"user_id IN (SELECT id FROM users WHERE university_id = {UNIVERSITY_FN})"
MESSAGE_SCOPE = (
    "topic_id IN (SELECT id FROM forum_topics WHERE activity_id IN "
    f"(SELECT id FROM activities WHERE university_id = {UNIVERSITY_FN}))"
)
MENTION_SCOPE = (
    "message_id IN (SELECT m.id FROM messages m JOIN forum_topics t ON t.id = m.topic_id "
    f"WHERE t.activity_id IN (SELECT id FROM activities WHERE university_id = {UNIVERSITY_FN}))"
)
FOLLOW_SCOPE = (
    f"follower_id IN (SELECT id FROM users WHERE university_id = {UNIVERSITY_FN}) "
    f"AND followee_id IN (SELECT id FROM users WHERE university_id = {UNIVERSITY_FN})"
)


def _policies():
    """(table, predicate) for every table that gets isolated."""
    for table in DIRECT:
        yield table, f"university_id = {UNIVERSITY_FN}"
    for table in VIA_ACTIVITY:
        yield table, ACTIVITY_SCOPE
    for table in VIA_USER:
        yield table, USER_SCOPE
    yield "messages", MESSAGE_SCOPE
    yield "message_mentions", MENTION_SCOPE
    yield "follows", FOLLOW_SCOPE


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION app_current_university_id() RETURNS integer
        LANGUAGE sql
        STABLE
        SECURITY DEFINER
        SET search_path = public, pg_temp
        AS $$
            SELECT university_id
            FROM users
            WHERE auth_provider_id = nullif(current_setting('app.auth_provider_id', true), '')
        $$;
        """
    )
    # The function must not be callable by just anyone, but the application
    # role needs it for every policy check.
    op.execute("REVOKE ALL ON FUNCTION app_current_university_id() FROM PUBLIC;")
    op.execute("GRANT EXECUTE ON FUNCTION app_current_university_id() TO PUBLIC;")

    for table, predicate in _policies():
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;")
        # FOR ALL so reads and writes are both constrained: WITH CHECK stops a
        # row being written into another university.
        op.execute(
            f"CREATE POLICY {table}_university_isolation ON {table} "
            f"FOR ALL USING ({predicate}) WITH CHECK ({predicate});"
        )


def downgrade():
    if op.get_bind().dialect.name != "postgresql":
        return

    for table, _predicate in _policies():
        op.execute(f"DROP POLICY IF EXISTS {table}_university_isolation ON {table};")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY;")

    op.execute("DROP FUNCTION IF EXISTS app_current_university_id();")
