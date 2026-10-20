import os
from datetime import datetime
from flask import Blueprint, current_app, render_template, request, redirect, url_for, flash, session
import flask_login

from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from . import db
from . import model

bp = Blueprint("auth", __name__)

@bp.route("/signup", method=["GET"])
def signup():
    return render_template(
        "auth/signup.html"
    )

@bp.route("/signup", methods=["POST"])
def signup_post():
    email = request.form.get("email")
    username = request.form.get("username")
    password = request.form.get("password")
    password_repeat = request.form.get("password_repeat")

    if password != password_repeat:
        flash("Sorry, passwords do not match.")
        return redirect(url_for("auth.signup"))

    query = db.select(model.User).where(model.User.email == email)
    user = db.session.execute(query).scalar_one_or_none()
    #Check to see whether user already exists. If so, signup cannot occur
    if user: 
        flash("Sorry, the provided email is already associated with another account.")
        return redirect(url_for("auth.signup"))

    session["signup_data"] = {
        "email": email, 
        "username": username, 
        "password_hash": generate_password_hash(password),
        "profile_pic": "Defaultpfp.png"
    }

    return redirect(url_for("auth.signup2"))

@bp.route("/signup2", methods=["POST"])
def signup2():
    signup_data = session.get("signup_data")
    if not signup_data:
        return redirect(url_for("auth.signup"))
    return render_template(
        "auth/signup2.html"
    )

@bp.route("signup2", methods["POST")
#will finish shortly