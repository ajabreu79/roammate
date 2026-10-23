import os
import uuid
from datetime import timedelta

from flask import Flask, request
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
import flask_login
import sqlalchemy
from sqlalchemy.engine import Engine

db = SQLAlchemy()
# render_as_batch rewrites ALTER TABLE as copy-and-swap, which SQLite needs
# for anything beyond adding a column.
migrate = Migrate(render_as_batch=True)
login_manager = flask_login.LoginManager()


@sqlalchemy.event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, _record):
    """SQLite ignores foreign keys unless asked.

    The schema leans on ON DELETE CASCADE and SET NULL for account deletion
    (FR-AUTH-017), so without this the dev database silently skips them while
    PostgreSQL enforces them -- the worst kind of difference between
    environments.
    """
    if type(dbapi_connection).__module__.startswith("sqlite3"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)

    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        SQLALCHEMY_DATABASE_URI=os.environ.get(
            "DATABASE_URL",
            "sqlite:///" + os.path.join(app.instance_path, "roammate.sqlite"),
        ),
        # Profile photos are the only upload; cap the request body accordingly.
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        # Where uploaded profile photos land. Overridden in tests so they do
        # not write into the served static folder.
        PROFILE_PIC_DIR=None,

        # Clerk owns credentials, sessions, email verification, Google OAuth
        # and sign-in rate limiting. See providers.CLERK_REQUIRED_SETTINGS for
        # the dashboard values the requirements depend on.
        CLERK_SECRET_KEY=os.environ.get("CLERK_SECRET_KEY"),
        CLERK_PUBLISHABLE_KEY=os.environ.get("CLERK_PUBLISHABLE_KEY"),
        CLERK_AUTHORIZED_PARTIES=[
            party for party in (os.environ.get("CLERK_AUTHORIZED_PARTIES") or "").split(",")
            if party.strip()
        ],

        # Our own cookie carries no credentials -- only the cached `sid`/`uni`
        # context from the Clerk session. FR-AUTH-008's 30 days is a Clerk
        # session setting; this keeps our cache from outliving it.
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),

        # Set COOKIE_SECURE=1 in any deployment served over HTTPS. Off by
        # default so cookies are not silently dropped on a plain-http dev server.
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
    )
    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    # FR-AUTH-010: unauthenticated access to a protected route lands on sign up.
    login_manager.login_view = "auth.signup"

    from . import model  # noqa: F401  (registers the mappings before create_all)
    from . import auth, main, providers
    from .services import AuthService

    providers.register_provider(app)

    # Identity comes from Clerk's session cookie on every request rather than
    # from a login cookie of our own, so this is a request loader, not a
    # user loader. It is the single place identity enters the application.
    @login_manager.request_loader
    def load_user_from_request(flask_request):
        return AuthService.user_from_request(flask_request)

    @app.context_processor
    def clerk_context():
        """Templates mount Clerk's components with the publishable key."""
        publishable_key = app.config.get("CLERK_PUBLISHABLE_KEY")
        enabled, message = providers.clerk_status(
            publishable_key, app.config.get("CLERK_SECRET_KEY"))
        return {
            "clerk_publishable_key": publishable_key,
            "clerk_js_url": providers.clerk_js_url(publishable_key),
            "clerk_enabled": enabled,
            "clerk_problem": message,
        }

    app.register_blueprint(auth.bp)
    app.register_blueprint(main.bp)

    # FR-AUTH-010 / 014 / 001: route protection, verification gating and
    # unfinished-signup redirection are the default for every route.
    app.before_request(auth.require_authentication)

    register_error_handlers(app)
    register_cli(app)

    return app


def register_error_handlers(app):
    """NFR-SOFT-006: no user-facing output carries diagnostic detail.

    Without these, Werkzeug answers an unhandled exception with its debugger
    (in debug mode) or a bare 500, and the former puts a full stack trace --
    file paths, SQL, framework versions, an interactive console -- in front of
    whoever triggered it.

    Flask only consults the 500 handler when it is not propagating
    exceptions, so `flask run --debug` still shows the debugger by design.
    Run without --debug to see these pages.
    """
    from flask import render_template

    def page(status, heading, message, correlation_id=None):
        return render_template(
            "errors/error.html", heading=heading, message=message,
            correlation_id=correlation_id,
        ), status

    @app.errorhandler(403)
    def forbidden(_error):
        return page(403, "Not allowed",
                    "You do not have permission to view this page.")

    @app.errorhandler(404)
    def not_found(_error):
        return page(404, "Page not found",
                    "That page does not exist, or the link has changed.")

    @app.errorhandler(405)
    def method_not_allowed(_error):
        return page(405, "That action is not available here",
                    "The page was reached in a way it does not accept.")

    @app.errorhandler(413)
    def too_large(_error):
        # MAX_CONTENT_LENGTH is 5 MB; FR-AUTH-012 uploads land here.
        return page(413, "That file is too large",
                    "Profile photos must be smaller than 5 MB. "
                    "Please choose a smaller image.")

    @app.errorhandler(Exception)
    def unhandled(error):
        """Everything not handled above, including a failed database call.

        HTTPException subclasses carry their own status and message and are
        re-raised so the specific handlers above still run; only genuine
        server faults reach the 500 page.
        """
        from werkzeug.exceptions import HTTPException
        if isinstance(error, HTTPException):
            return error

        # NFR-SOFT-007: one id ties this response to its log entry. The
        # traceback goes to the log, never to the response.
        correlation_id = uuid.uuid4().hex[:12]
        app.logger.exception(
            "Unhandled error %s on %s %s", correlation_id,
            request.method, request.path,
        )

        # A failed request can leave the session holding a dead transaction,
        # which would make rendering the page fail too.
        try:
            db.session.rollback()
        except Exception:                # pragma: no cover - best effort
            app.logger.warning("Could not roll back after error %s", correlation_id)

        return page(500, "Something went wrong on our end",
                    "The problem has been logged. Please try again in a moment.",
                    correlation_id=correlation_id)


def register_cli(app):
    """Commands for work that used to happen on every startup.

    The schema is created by `flask db upgrade`, not by create_all, so that a
    change to model.py reaches a database that already has the old tables.
    Seeding is explicit for the same reason: it is data, not schema.
    """

    @app.cli.command("seed-universities")
    def seed_universities():
        """FR-AUTH-013: fill the University table from the seed list."""
        from . import universities
        added = universities.seed()
        print(f"{added} universities added.")

    @app.cli.command("seed-activity-types")
    def seed_activity_types():
        """FR-ACE-001: fill the ActivityType table from the seed list."""
        from . import activity_types
        added = activity_types.seed()
        print(f"{added} activity types added.")

    @app.cli.command("dispatch-events")
    def dispatch_events():
        """NFR-REL-004: deliver committed outbox events to their subscribers.

        Run it on a schedule, or in a loop as a worker; each run delivers
        whatever is due. Safe to run in several places at once.
        """
        from .events import EventBus
        delivered = EventBus.dispatch_pending()
        print(f"{delivered} events delivered.")
