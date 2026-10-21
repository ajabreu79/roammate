"""Minimal non-auth routes, enough for the auth flows to redirect somewhere."""

from flask import Blueprint, abort, render_template
import flask_login

from . import db
from . import model

bp = Blueprint("main", __name__)


@bp.route("/")
@flask_login.login_required
def index():
    return render_template("main/index.html")


@bp.route("/profile/<int:user_id>")
@flask_login.login_required
def profile(user_id):
    user = db.session.get(model.User, user_id)
    if user is None:
        abort(404)
    return render_template("main/profile.html", user=user)
