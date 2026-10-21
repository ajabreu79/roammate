"""AuthController: reads the request, calls a service, renders the result.

Credential handling lives in Clerk, so there are no password form handlers
here. The routes that would own them (/auth/verify-email, /auth/password-change)
remain as the landing pages Clerk returns the user to.
"""

import json
import os
import uuid
from datetime import date

from flask import (
    Blueprint, Response, current_app, flash, jsonify, redirect,
    render_template, request, url_for,
)
import flask_login

from werkzeug.utils import secure_filename

from . import db
from . import universities
from .validators import MAX_DESC_LENGTH
from .ratelimit import rate_limit
from .services import AccountDeletionService, AuthService, DataExportService
from .validators import RegistrationValidator

bp = Blueprint("auth", __name__, url_prefix="/auth")

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# FR-AUTH-010: every endpoint not listed here is a protected route.
PUBLIC_ENDPOINTS = {
    "static",
    "auth.signup", "auth.login",
    "auth.register_alias", "auth.register_step2_alias",
    "auth.verify_email",
    "auth.oauth_google_callback",
    "auth.university_options",
}

# FR-AUTH-014: an authenticated but unverified user may only reach these --
# enough to verify, or to leave.
VERIFICATION_EXEMPT = PUBLIC_ENDPOINTS | {
    "auth.verification_pending",
    "auth.password_change",
    "auth.logout_post",
    "auth.delete_account",
    "auth.export_data",
}

# Reachable while registration step 2 is still unanswered.
ONBOARDING_EXEMPT = VERIFICATION_EXEMPT | {
    "auth.signup2",
    "auth.signup2_post",
}


def require_authentication():
    """Gate protected routes, unverified accounts, then unfinished signups.

    Registered once on the app (see create_app) so protection is the default
    rather than something each new route has to opt into.
    """
    endpoint = request.endpoint
    if endpoint is None:
        return None

    user = flask_login.current_user

    if not user.is_authenticated:
        if endpoint in PUBLIC_ENDPOINTS:
            return None
        return redirect(url_for("auth.signup"))

    if not user.email_verified and endpoint not in VERIFICATION_EXEMPT:
        return redirect(url_for("auth.verification_pending"))

    if not user.profile_complete and endpoint not in ONBOARDING_EXEMPT:
        return redirect(url_for("auth.signup2"))

    return None


def _flash_all(messages):
    for message in messages:
        flash(message)


def _save_profile_pic(upload):
    """Store an upload under a unique name. Returns (filename, errors).

    The name is randomised rather than taken from the upload so two users
    cannot overwrite each other's photo.
    """
    _, extension = os.path.splitext(secure_filename(upload.filename))
    extension = extension.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return None, ["Profile pictures must be a PNG, JPG, GIF or WEBP image."]

    filename = f"{uuid.uuid4().hex}{extension}"
    directory = current_app.config.get("PROFILE_PIC_DIR") or os.path.join(
        current_app.static_folder, "resources")
    os.makedirs(directory, exist_ok=True)
    upload.save(os.path.join(directory, filename))
    return filename, []


# --- registration step 1: Clerk (FR-AUTH-001, 002, 003, 015, 016, 018) ------

@bp.route("/signup", methods=["GET"])
def signup():
    """Hosts Clerk's sign-up component, which collects email, first and last
    name, password and confirmation, then verifies the address."""
    if flask_login.current_user.is_authenticated:
        return redirect(url_for("auth.signup2"))
    return render_template("auth/signup.html")


@bp.route("/login", methods=["GET"])
def login():
    """Hosts Clerk's sign-in component (FR-AUTH-007), including Google."""
    if flask_login.current_user.is_authenticated:
        return redirect(url_for("main.index"))
    return render_template("auth/login.html")


@bp.route("/oauth/google/callback", methods=["GET"])
def oauth_google_callback():
    """Where Clerk returns the browser after a Google sign-in (FR-AUTH-016).

    By the time this runs the request already carries a Clerk session, so the
    request loader has created or synced the local row; all that is left is
    to route the user to whatever they still owe us.
    """
    if not flask_login.current_user.is_authenticated:
        flash("Google sign-in did not complete. Please try again.")
        return redirect(url_for("auth.login"))
    return redirect(url_for("main.index"))


# --- registration step 2: our own profile fields (FR-AUTH-001) --------------

@bp.route("/signup2", methods=["GET"])
@flask_login.login_required
def signup2():
    if flask_login.current_user.profile_complete:
        return redirect(url_for("main.index"))
    return render_template(
        "auth/signup2.html",
        universities=universities.search(),
        countries=universities.countries(),
        today=date.today().isoformat(),
        max_desc_length=MAX_DESC_LENGTH,
    )


@bp.route("/signup2", methods=["POST"])
@flask_login.login_required
@rate_limit("signup2", limit=20, window_seconds=300,
            redirect_endpoint="auth.signup2")
def signup2_post():
    user = flask_login.current_user

    # Validate before writing any upload to disk, so a rejected submission
    # leaves no orphaned file behind.
    upload = request.files.get("profile_pic")
    filename = None
    if upload and upload.filename:
        filename, upload_errors = _save_profile_pic(upload)
        if upload_errors:
            _flash_all(upload_errors)
            return redirect(url_for("auth.signup2"))

    errors, _user = AuthService.complete_profile(
        user,
        request.form.get("gender"),
        request.form.get("school_year"),
        request.form.get("university"),
        request.form.get("birthday"),
        request.form.get("description") or "",
        profile_pic_filename=filename,
    )
    if errors:
        _flash_all(errors)
        return redirect(url_for("auth.signup2"))

    flash("Welcome to roammate!")
    return redirect(url_for("main.index"))


# The design doc names these two paths /auth/register and /auth/register/step2.
# They redirect to the canonical routes above rather than double-registering,
# which would make url_for's choice between them ambiguous.

@bp.route("/register", methods=["GET"])
def register_alias():
    return redirect(url_for("auth.signup"))


@bp.route("/register/step2", methods=["GET"])
def register_step2_alias():
    return redirect(url_for("auth.signup2"))


@bp.route("/universities", methods=["GET"])
def university_options():
    """Backs the filterable university dropdown (FR-AUTH-013)."""
    return jsonify(universities.search(
        query=request.args.get("q"),
        country=request.args.get("country"),
    ))


# --- verification and password change: Clerk-owned (FR-AUTH-014) ------------

@bp.route("/verify-email", methods=["GET"])
def verify_email():
    """Landing page for Clerk's verification link.

    Clerk marks the address verified on its side; sync_from_provider mirrors
    that onto our row on the next request, which is this one.
    """
    if flask_login.current_user.is_authenticated and flask_login.current_user.email_verified:
        flash("Email verified. Welcome to roammate!")
        return redirect(url_for("auth.signup2"))
    return render_template("auth/verify_email.html")


@bp.route("/verify-email/pending", methods=["GET"])
@flask_login.login_required
def verification_pending():
    return render_template("auth/verify_email.html", email=flask_login.current_user.email)


@bp.route("/password-change", methods=["GET"])
def password_change():
    """Hosts Clerk's user-profile security panel, which owns password change."""
    return render_template("auth/password_change.html")


# --- logout (FR-AUTH-009) ---------------------------------------------------

@bp.route("/logout", methods=["POST"])
@flask_login.login_required
def logout_post():
    AuthService.logout()
    return redirect(url_for("auth.login"))


# --- account deletion and export (FR-AUTH-017) ------------------------------

@bp.route("/account", methods=["DELETE", "POST"])
@flask_login.login_required
def delete_account():
    """DELETE per the design doc; POST too, since HTML forms cannot send DELETE."""
    if request.form.get("confirm") != "DELETE":
        flash("Type DELETE to confirm account deletion.")
        return redirect(url_for("auth.edit_profile"))

    errors, deleted = AccountDeletionService.delete(flask_login.current_user)
    if not deleted:
        _flash_all(errors)
        return redirect(url_for("auth.edit_profile"))

    flash("Your account has been deleted.")
    return redirect(url_for("auth.signup"))


@bp.route("/export", methods=["GET"])
@flask_login.login_required
@rate_limit("export", limit=5, window_seconds=300, redirect_endpoint="auth.edit_profile")
def export_data():
    payload = DataExportService.export(flask_login.current_user)
    return Response(
        json.dumps(payload, indent=2, ensure_ascii=False),
        mimetype="application/json",
        headers={"Content-Disposition": 'attachment; filename="roammate-export.json"'},
    )


# --- profile details and photo (FR-AUTH-012) --------------------------------

@bp.route("/edit-profile", methods=["GET"])
@flask_login.login_required
def edit_profile():
    return render_template(
        "main/edit_profile.html",
        universities=universities.search(),
        countries=universities.countries(),
        today=date.today().isoformat(),
        max_desc_length=MAX_DESC_LENGTH,
    )


@bp.route("/edit-profile", methods=["POST"])
@flask_login.login_required
def edit_profile_post():
    """Edit the fields FR-PROF-002 permits.

    Email, university and birthday are deliberately NOT editable here. The SRS
    Design and Implementation Constraints and FR-PROF-002 both forbid it, and
    the design doc's ProfileService "rejects edits to email, university and
    birthday" -- age is derived from birthday (FR-PROF-003), so an editable
    birthday would let a user move themselves out of the 18+ check they
    already passed at registration (FR-AUTH-005), and university is the
    isolation boundary for FR-PROF-009. Any such field arriving in the form is
    ignored rather than trusted.
    """
    user = flask_login.current_user

    description = request.form.get("description")
    if description is not None:
        errors = RegistrationValidator.check_description(description)
        if errors:
            _flash_all(errors)
            return redirect(url_for("auth.edit_profile"))
        user.desc = description

    for field in ("gender", "school_year"):
        value = request.form.get(field)
        if value:
            setattr(user, field, value)

    upload = request.files.get("profile_pic")
    if upload and upload.filename:
        filename, errors = _save_profile_pic(upload)
        if errors:
            _flash_all(errors)
            return redirect(url_for("auth.edit_profile"))
        user.profile_pic = filename

    db.session.commit()
    flash("Profile updated.")
    return redirect(url_for("main.profile", user_id=user.id))
