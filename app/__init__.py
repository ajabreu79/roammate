import os
from datetime import timedelta

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
import flask_login

db = SQLAlchemy()
login_manager = flask_login.LoginManager()


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

    with app.app_context():
        db.create_all()

    return app
