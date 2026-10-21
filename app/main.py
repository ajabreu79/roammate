from flask import Blueprint, abort, render_template, current_app, flash, request, redirect, url_for
import flask_login

from . import db
from . import model

from sqlalchemy import or_

from datetime import datetime
import os
import dateutil.tz



bp = Blueprint("main", __name__)

upload_folder = "static/resources"


@bp.route("/")
@flask_login.login_required
def index():
    query = db.select(model.ActivityProposal).where(or_(
        model.ActivityProposal.status == model.ActivityProposalStatus.OPEN.value,
        model.ActivityProposal.status == model.ActivityProposalStatus.APPROVAL_REQUIRED.value)
    ).order_by(model.ActivityProposal.timestamp.desc()).limit(4)
    latest_activities = db.session.execute(query).scalars().all()

    return render_template("main/index.html", latest_activities = latest_activities))



@bp.route("/profile/<int:user_id>")
@flask_login.login_required
def profile(user_id):
    user = db.get_or_404(model.User, user_id)

    query = (
        db.select(model.ActivityProposal)
        .join(model.ActivityProposal.participants)
        .filter(model.User.id == user.id)
        .order_by(model.ActivityProposal.timestamp.desc())
    )
    activities = db.session.execute(query).scalars().all()

    if flask_login.current_user.id == user.id:
        follow_button = None #if user is viewing their own profile, no follow button
    elif hasattr(user, "followers") and flask_login.current_user in user.followers:
        follow_button = "unfollow" #if user is already following, button should be "unfollow"
    else:
        follow_button = "follow" #if user isn't following yet, show follow button
    
    return render_template(
        "main/profile.html", 
        user=user,
        activities = activities,
        follow_button=follow_button,
    )

@bp.route("/activity", methods=["POST"])
@flask_login.login_required
def new_activity:
    user = flask_login.current_user
    title = request.form.get("title")
    description = request.form.get("description")
    origin = request.form.get("departure_location")
    destinations = request.form.getlist("destination")
    start = request.form.get("start")
    end = request.form.get("end")
    budget = request.form.get("budget")
    max_members = request.form.get("max_members")
    minAge = request.form.get("min")
    university = request.form.get("university")
    status = model.ActivityProposalStatus.OPEN.value
    image = request.files.get("activity_image")
    if image and image.filename != "":
        filename = secure_filename(image.filename)
        save_path = os.path.join(current_app.static_folder, "resources", filename)
        image.save(save_path)
    else:
        filename = "example-activity.jpg" #or, replace with filename of default picture

    destinations_str = ",".join(destinations)

    new_activity = model.ActivityProposal(
        creator=user,
        title=title,
        description=description,
        origin=origin,
        destinations=destinations_str,
        start_time=start,
        end_time=end,
        budget=budget,
        max_members=max_members,
        min_age=minAge,
        max_age=maxAge,
        university=university,
        timestamp=datetime.now(dateutil.tz.tzlocal()),
        status=status,
        image=filename
    )

    db.session.add(new_trip)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=new_activity.id))

