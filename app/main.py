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
def new_activity():
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
    maxAge = request.form.get("max")
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

    db.session.add(new_activity)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=new_activity.id))

@bp.route("/make_editor/<int:activity_id>/<int:participant_id>", methods=["POST"])
@flask_login.login_required
def make_editor(activity_id, participant_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)
    user = flask_login.current_user
    
    if not user.is_editor_for(activity):
        flash("You do not have permission to promote editors for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    editor = model.ActivityProposalParticipation.query.get((participant_id, activity.id))
    editor.is_editor = True
    db.session.commit()
    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/edit_activity/<int:activity_id>", methods=["POST"])
@flask_login.login_required
def edit_activity(activity_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)
    user = flask_login.current_user
    
    participation = model.ActivityProposalParticipation.query.get((user.id, activity.id))
    
    if activity.status == model.ActivityProposalStatus.FINALIZED or activity.status == model.ActivityProposalStatus.CANCELLED:
        flash("This activity has been finalized or cancelled and cannot be edited.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    if (user not in activity.participants) and not (participation and participation.is_editor):
        flash("You do not have permission to edit this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    
    status = request.form.get("status")
    if activity.status in [2, 3, 4]:
        flash("This activity is closed and cannot be modified.", "error")
        return redirect(url_for('main.activity', activity_id=activity.id))
    
    origin = request.form.get("departure_location", "").strip()
    
    destinations = [d.strip() for d in request.form.getlist("destination") if d.strip()]
    destinations_str = ",".join(destinations) 
    
    start = request.form.get("start")
    end = request.form.get("end")
    
    budget = request.form.get("budget")
    max_members = request.form.get("max_members")
    minAge = request.form.get("min_age")
    maxAge = request.form.get("max_age")
    university = request.form.get("university_specific") or None

    if university == "":
        university = None
    
    image = request.files.get("activity_image")
    
    if activity.title != title:
        activity.title = title

    if activity.description != description:
        activity.description = description
        
    if status and activity.status != status:
        activity.status = status
        
    activity.origin_finalized = 'origin_finalized' in request.form
    activity.destinations_finalized = 'destinations_finalized' in request.form
    activity.dates_finalized = 'dates_finalized' in request.form
    activity.budget_finalized = 'budget_finalized' in request.form
    activity.max_memberss_finalized = 'max_members_finalized' in request.form
    activity.age_range_finalized = 'age_range_finalized' in request.form
    activity.status_finalized = 'status_finalized' in request.form
    activity.university_finalized ='university_finalized' in request.form
    if not activity.origin_finalized and origin and activity.origin != origin:
        activity.origin = origin

    if not activity.destinations_finalized and destinations_str and activity.destinations != destinations_str:
        activity.destinations = destinations_str

    if not activity.dates_finalized:
        if start and activity.start_date != start:
            activity.start_date = start
        if end and activity.end_date != end:
            activity.end_date = end
    if not activity.budget_finalized and budget and activity.budget != budget:
        activity.budget = budget
    if not activity.max_members_finalized and max_members and activity.max_members != max_members:
        activity.max_members = max_members
    if not activity.age_range_finalized:
        if minAge and activity.min_age != minAge:
            activity.min_age = minAge
        if maxAge and activity.max_age != maxAge:
            activity.max_age = maxAge
    if not activity.university_finalized:
        if activity.university != university:
            activity.university = university
    if image and image.filename and image.filename != activity.image:
        filename = secure_filename(image.filename)
        save_path = os.path.join(current_app.static_folder, "resources", filename)
        image.save(save_path)
        activity.image = filename
    
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))
       

@bp.route("/activity/<int:activity_id>")
@bp.route("/activity/<int:activity_id>/forum/<forum_topic>")
@flask_login.login_required
def activity(activity_id, forum_topic=None):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user
    already_joined = user in activity.participants
    
    activity_admin = False
    join_requests = []
    requesting_users = []
    
    if already_joined:
        is_editor_query = db.select(model.ActivityProposalParticipation.is_editor).where(
            model.ActivityProposalParticipation.user_id == user.id,
            model.ActivityProposalParticipation.activity_proposal_id == activity.id
        )
        is_editor = db.session.execute(is_editor_query).scalar_one_or_none()
        if is_editor:
            activity_admin = True
            if activity.status == model.ActivityProposalStatus.APPROVAL_REQUIRED.value:
                join_requests_query = db.select(model.ActivityProposalJoinRequest).where(model.ActivityProposalJoinRequest.activity_proposal_id == activity.id).order_by(model.ActivityProposalJoinRequest.timestamp.asc())
                join_requests = db.session.execute(join_requests_query).scalars().all()
                for req in join_requests:
                    requesting_users.append(db.get_or_404(model.User, req.user_id))
                join_requests = [(req, user) for req, user in zip(join_requests, requesting_users)]

    topics_query = (db.select(model.ActivityProposalMessage.forum_topic).where(model.ActivityProposalMessage.activity_proposal_id == activity_id).distinct().order_by(model.ActivityProposalMessage.forum_topic))
    forum_topics = db.session.execute(topics_query).scalars().all()
    if forum_topic:
        active_topic = forum_topic
    else:
        active_topic = "Main"

    active_forum_messages_query = (db.select(model.ActivityProposalMessage).where(model.ActivityProposalMessage.activity_proposal_id == activity_id, model.ActivityProposalMessage.forum_topic == active_topic).order_by(model.ActivityProposalMessage.timestamp.asc()))
    active_forum_messages = db.session.execute(active_forum_messages_query).scalars().all()

    return render_template("main/activity.html", activity=Activity, statuses=model.activityProposalStatus, join_requests=join_requests, already_joined = already_joined, forum_topics = forum_topics, active_topic = active_topic, active_forum_messages = active_forum_messages)

@bp.route("/activity/<int:activity_id>/join", methods=["POST"])
@flask_login.login_required
def join_activity(activity_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    if activity.status != model.ActivityProposalStatus.OPEN.value:
        flash("This activity is not open for joining.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    if user in activity.participants:
        flash("You are already part of this activity!", "info")
        return redirect(url_for("main.activity", activity_id=activity_id))

    if activity.max_members is not None and len(activity.participants) >= trip.max_members:
        flash("Activity is already full!", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    if activity.age_range_finalized:
        if activity.min_age and user.age < activity.min_age:
            flash("You are too young for this activity.", "error")
            return redirect(url_for("main.activity", activity_id=activity_id))
        if activity.max_age and user.age > activity.max_age:
            flash("You are too old for this activity.", "error")
            return redirect(url_for("main.activity", activity_id=activity_id))

    if activity.university_finalized and activity.university:
        if not user.university or user.university != activity.university:
            flash("This activity is restricted to a specific university.", "error")
            return redirect(url_for("main.activity", activity_id=activity_id))

    participation = model.ActivityProposalParticipation(
        user_id=user.id,
        activity_proposal_id=activity.id,
        is_editor=False,
    )
    db.session.add(participation)
    db.session.commit()

    flash("Successfully joined the activity!", "success")
    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/activity/<int:activity_id>/request_join", methods=["POST"])
@flask_login.login_required
def request_join_activity(activity_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    if user in activity.participants:
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    existing_request = db.session.get(model.ActivityProposalJoinRequest, (activity.id, user.id))
    if existing_request:
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    join_request = model.ActivityProposalJoinRequest(
        activity_proposal_id=activity.id,
        user_id=user.id,
    )
    db.session.add(join_request)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/activity/<int:activity_id>/update_join_request/<int:requesting_user_id>", methods=["POST"])
@flask_login.login_required
def update_join_request(activity_id, requesting_user_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    if not user.is_editor_for(activity):
        flash("You do not have permission to manage join requests for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    action = request.form.get("approval_status")
    join_request = db.session.get(model.ActivityProposalJoinRequest, (activity.id, requesting_user_id))
    if not join_request:
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    if action == "approve":
        joining_user = model.ActivityProposalParticipation(
            user_id=requesting_user_id,
            activity_proposal_id=activity.id,
            is_editor=False,
        )
        db.session.add(joining_user)
    
    db.session.delete(join_request)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/activity/<int:activity_id>/cancel_join", methods=["POST"])
@flask_login.login_required
def cancel_join_request(activity_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    existing_request = db.session.get(model.ActivityProposalJoinRequest, (activity.id, user.id))
    if not existing_request:
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    db.session.delete(existing_request)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/activity/<int:activity_id>/leave", methods=["POST"])
@flask_login.login_required
def leave_activity(activity_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    if user not in activity.participants:
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    if user.is_editor_for(activity) and activity.num_editors() <= 1:
        flash("The last editor cannot leave the activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))
    
    user_participation = (
        db.session.query(model.ActivityProposalParticipation)
        .filter_by(user_id=user.id, activity_proposal_id=activity.id)
        .first()
    )
        
    db.session.delete(user_participation)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))

@bp.route("/activity/<int:activity_id>/message", methods=["POST"])
@flask_login.login_required
def new_message(activity_id):
    user = flask_login.current_user
    content = request.form.get("message-content", "").strip()
    forum_topic = request.form.get("forum_topic", "Main").strip() #to get the specific channel/forum of the messages
    new_forum_topic = request.form.get("new_forum_topic", "").strip() #if user wants to make a new forum for some specific messages
    if new_forum_topic and new_forum_topic != "":
        forum_topic = new_forum_topic

    if not content:
        flash("Message cannot be empty", "error")
        return redirect(url_for("main.activity", activity_id=activity_id, forum_topic=forum_topic))
    
    message = model.ActivityProposalMessage(activity_proposal_id=activity_id, user_id = flask_login.current_user.id, content=content, forum_topic = forum_topic)
    db.session.add(message)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id, forum_topic = forum_topic))

@bp.route("/form")
@flask_login.login_required
def form():
    return render_template("main/activity_form.html")

@bp.route("/follow/<int:user_id>", methods=["POST"])
@flask_login.login_required
def follow(user_id):
    user = db.get_or_404(model.User, user_id)
    if (flask_login.current_user not in user.activity_proposals):
        user.activity_proposals.append(flask_login.current_user)
        db.session.commit()
    return redirect(url_for("main.profile", user_id=user_id))

@bp.route("/unfollow/<int:user_id>", methods=["POST"])
@flask_login.login_required
def unfollow(user_id):
    user = db.get_or_404(model.User, user_id)
    if (flask_login.current_user in user.activity_proposals):
        user.activity_proposals.remove(flask_login.current_user)
        db.session.commit()
    return redirect(url_for("main.profile", user_id=user_id))


@bp.route('/activity/<int:activity_id>/meetups')
@flask_login.login_required
def activity_meetup_retrieval(activity_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)

    meetups = model.Meetup.query.filter_by(activity_proposal_id=activity_id).all()

    return jsonify([{
        'id': meetup.id,
        'title': meetup.location,
        'start': meetup.date_time.isoformat(),
        'description': meetup.description,
        'extendedProps': {
            'location': meetup.location,
            'description': meetup.description,
            'creator': meetup.creator.name,
            'link': meetup.link
        }
    } for meetup in meetups])

@bp.route('/activity/<int:activity_id>/meetups', methods=['POST'])
@flask_login.login_required
def new_activity_meetup(activity_id):
    current_user=flask_login.current_user
    activity = model.ActivityProposal.query.get_or_404(activity_id)

    json = request.get_json()

    try:
        date = json['date']
        time = json['time']
        datetime_string = f"{date} {time}"
        date_time = datetime.strptime(datetime_string, '%Y-%m-%d %H:%M')

        meetup = model.Meetup(
            activity_proposal_id=activity_id,
            creator_id=current_user.id,
            location=json['location'],
            date_time=date_time,
            description=json.get('description'),
            link=json.get('link')

        )

        db.session.add(meetup)
        db.session.commit()

        return jsonify({'success': True, 'id': meetup.id})
    
    except Exception as e:
        db.session.rollback()
        return jsonify({'success':False, 'error': str(e)}), 400
    
@bp.route('/activity/<int:activity_id>/meetups/<int:meetup_id>', methods=['DELETE'])
@flask_login.login_required
def delete_meetup(activity_id, meetup_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)
    meetup = model.Meetup.query.get_or_404(meetup_id)

    try:
        db.session.delete(meetup)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    
@bp.route("/browse_activities")
@flask_login.login_required
def browse_activities():
    user = flask_login.current_user

    activities_query = model.ActivityProposal.query

    max_budget = request.args.get("max_budget", type=float)
    if max_budget:
        activities_query = activities_query.filter(model.ActivityProposal.budget <= max_budget)

    min_age = request.args.get("min_age", type=int)
    max_age = request.args.get("max_age", type=int)
    if min_age:
        activities_query = activities_query.filter(model.ActivityProposal.min_age >= min_age)
    if max_age:
        activites_query = activites_query.filter(model.ActivityProposal.max_age <= max_age)

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date:
        activities_query = activities_query.filter(model.ActivityProposal.start_date >= start_date)
    if end_date:
        activites_query = activites_query.filter(model.ActiviyProposal.end_date <= end_date)

    origin = request.args.get("origin")
    if origin:
        activites_query = activites_query.filter(model.ActivityProposal.origin.ilike(f"%{origin}%")) 
        #referenced ChatGPT for 'ilike', which compares with case insensitive values

    destination = request.args.get("destination")
    if destination:
        activites_query = activites_query.filter(model.ActivityProposal.destinations.ilike(f"%{destination}%"))

    university = request.args.get("university")
    if university:
       activites_query = activites_query.join(model.User, model.ActivityProposal.creator_id == model.User.id)

       activities_query = activities_query.filter(
        db.or_(
            # Activity is for user's home university
            db.and_(
                model.ActivityProposal.university == "home_uni",
                db.or_(
                    model.User.home_uni == user.home_uni,
                    model.User.visiting_uni == user.home_uni
                )
            ),
            # Activity is for user's visiting university
            db.and_(
                model.ActivityProposal.university == "visiting_uni",
                db.or_(
                    model.User.home_uni == user.visiting_uni,
                    model.User.visiting_uni == user.visiting_uni
                )
            ),
            # Activity is for both universities
            db.and_(
                model.ActivityProposal.university == "both",
                db.or_(
                    model.User.home_uni == user.home_uni,
                    model.User.visiting_uni == user.home_uni,
                    model.User.home_uni == user.visiting_uni,
                    model.User.visiting_uni == user.visiting_uni
                )
            )
        )
    )
    
    status = request.args.get("status")
    if status:
        status_map = {
            "OPEN" : model.ActivityProposalStatus.OPEN.value,
            "APPROVAL_REQUIRED" : model.ActivityProposalStatus.APPROVAL_REQUIRED.value, # type: ignore
            "CLOSED" : model.ActivityProposalStatus.CLOSED.value, # type: ignore
            "FINALIZED" : model.ActivityProposalStatus.FINALIZED.value, # type: ignore
            "CANCELLED" : model.ActivityProposalStatus.CANCELLED.value, # type: ignore
        }

        status_value = status_map.get(status)
        if status_value:
            activities_query = activities_query.filter(model.ActivityProposal.status == status_value)

    activites = activites_query.order_by(model.ActivityProposal.timestamp.desc()).all()

    return render_template("main/browse_activities.html", activities=activities)