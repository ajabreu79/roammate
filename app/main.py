"""Activity, participation, messaging and discovery controllers.

REQUIREMENTS TRACEABILITY
Every route below is tagged with the SRS requirements it implements. Where a
requirement is only partly met, the gap is recorded as a GAP comment next to
the code rather than left to be rediscovered. Grep for "GAP" to list them.

DESIGN DEVIATIONS that apply to this whole file:

  - Layering. The design doc specifies Controller -> Service -> Repository per
    module, with authorization and transaction boundaries in the service layer
    (NFR-SECU-004, NFR-REL-005). This file holds controllers, business logic
    and queries together, so there is no layer where a rule can be enforced
    once for every caller.

  - Module boundaries. The design splits these routes across five modules
    (Activity, Participation, Messaging, Discover, Profile), each owning its
    own tables and route prefix. They are all in the `main` blueprint here.

  - Route paths. The design uses /activities/{id}/join, /leave, /requests,
    /users/{id}/follow, /feed and /browse. This file uses /activity/{id}/join,
    /follow/{id}, /browse_activities, /make_editor/... and /edit_activity/...

  - Entity names. The design entity is Activity with an ActivityMembership
    carrying a role enum (ADMIN, EDITOR, ATTENDEE), a FinalizedField table and
    a ForumTopic table. This file uses ActivityProposal,
    ActivityProposalParticipation with an is_editor boolean, *_finalized
    boolean columns, and topics derived from existing messages.

  - Missing model. None of model.ActivityProposal,
    ActivityProposalParticipation, ActivityProposalJoinRequest,
    ActivityProposalMessage, ActivityProposalStatus or Meetup is defined in
    model.py, so every route here raises AttributeError at request time.

  - Meetup appears in neither the SRS nor the design doc's 21-table schema.

  - University scoping. FR-PROF-009 and NFR-SECU-010 require the university
    predicate on every query, enforced at the query layer (the design's
    UniversityScope helper). No query in this file is scoped, and
    browse_activities applies it only when the client sends a parameter --
    the exact leak SRS inspection finding on NFR-SECU-010 resolved against.

  - Image uploads. NFR-SECU-008 requires content-type and size validation,
    re-encoding to strip EXIF, and non-guessable storage keys. Both upload
    paths here save secure_filename(upload.filename) directly, so names are
    guessable and two activities sharing a filename overwrite each other.
    app/auth.py:_save_profile_pic has the pattern this needs.
"""

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


# FR-AD-001 home feed; FR-AD-004 card contents (template); FR-AD-006 scroll.
# GAP (FR-AD-001): shows APPROVAL_REQUIRED as well as OPEN, and has no
#   university predicate, so the feed spans every campus (FR-PROF-009,
#   NFR-SECU-010).
# GAP (FR-AD-006): limit(4) with no cursor; infinite scroll needs keyset
#   pagination on (timestamp, id) per the design's CursorPaginator.
@bp.route("/")
@flask_login.login_required
def index():
    query = db.select(model.ActivityProposal).where(or_(
        model.ActivityProposal.status == model.ActivityProposalStatus.OPEN.value,
        model.ActivityProposal.status == model.ActivityProposalStatus.APPROVAL_REQUIRED.value)
    ).order_by(model.ActivityProposal.timestamp.desc()).limit(4)
    latest_activities = db.session.execute(query).scalars().all()

    return render_template("main/index.html", latest_activities=latest_activities)



# FR-PROF-001 view another user's profile; FR-PROF-003 derived age;
# FR-PROF-005 follow state.
# GAP (FR-PROF-001): one flat `activities` list. The requirement asks for
#   past and upcoming activities separately, each with date and link.
# GAP (NFR-SECU-009): the whole User row reaches the template, so email and
#   exact birthday are available to any viewer. The design routes other
#   users' data through PublicUserView, which exposes age instead of birthday.
# GAP (FR-PROF-009): no check that the viewer shares the subject's university.
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

# FR-ACE-001 create an activity; FR-ACE-002 defaults to OPEN.
# GAP (FR-ACE-001): no activity type/theme, no allowed year range, no allowed
#   gender preferences. The default image filename is still a placeholder
#   ("example-activity.jpg").
# GAP (FR-ACE-003): the creator is set as `creator` but no
#   ActivityProposalParticipation row is created, so the activity has no Admin
#   and no members. P1.
# GAP (FR-ACE-004): no validation at all -- required fields, end >= start,
#   dates >= today, budget >= 0, max members > 0, min age <= max age. P1.
#   The design puts these in ActivityValidator.
# GAP (NFR-REL-005): creating the activity and its admin membership must be
#   one transaction.
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

# FR-ACE-006 grant editor access.
# GAP (FR-ACE-007): the design's RolePolicy gives "grant or revoke Editor" to
#   Admin only. is_editor_for() lets any Editor promote further Editors.
# GAP (FR-ACE-006): no check that the target is actually a participant.
# GAP (NFR-SECU-012): role grants are privileged actions and must be audited.
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

# FR-ACE-005 editors update fields; FR-ACE-008 field finalization;
# FR-ACE-009 status changes.
# GAP (FR-ACE-005, NFR-SECU-004): the permission test is
#   `(user not in participants) and not (participation and is_editor)`. For a
#   plain Attendee the first clause is False, so the whole condition is False
#   and the edit proceeds -- every attendee can edit. P1 authorization defect.
# GAP (FR-ACE-009): `activity.status` holds an enum .value (an int) but is
#   compared against the enum member here, so the guard never fires and
#   FINALIZED/CANCELLED activities stay editable. The later check uses the
#   magic numbers [2, 3, 4] for the same idea.
# GAP (FR-ACE-008): the *_finalized flags are assigned from checkbox presence
#   on every submit, so omitting a checkbox un-finalizes a field that
#   FR-ACE-008 says is immutable. Finalization is also one-way by design.
# GAP (FR-ACE-009): status is taken straight from the form with no transition
#   table and no role check -- the design isolates this in
#   ActivityStatusMachine, which also rejects changes once status is finalized.
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
       

# FR-PE-007 member list; FR-MESS-001 message board; FR-MESS-003 forum topics;
# FR-MESS-005 switching between topics; FR-MESS-006 author and timestamp.
# GAP (FR-MESS-004): messages are loaded regardless of `already_joined`, so a
#   non-participant who knows an activity id can read its board. P1.
# GAP (FR-MESS-003): topics are derived from DISTINCT message.forum_topic, so
#   an empty topic disappears and "Main" here vs "main" elsewhere can split a
#   board in two. The design gives ForumTopic its own table, created with the
#   activity.
# GAP (FR-ACE-007): `activity_admin` is set from is_editor, conflating Admin
#   with Editor. The two have different permissions.
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

# FR-PE-001 join an OPEN activity; FR-PE-003 no double join;
# FR-PE-004 capacity; FR-PE-005 age eligibility; FR-PE-008 blocked statuses.
# GAP (FR-PE-005): the age check runs only `if activity.age_range_finalized`.
#   Age limits apply whether or not the field is finalized, and school year is
#   never checked at all.
# GAP (FR-PE-009): allowed-gender preference is not enforced.
# GAP (FR-PE-010): a full activity should offer the waitlist; this returns an
#   error instead. No waitlist exists.
# GAP (NFR-REL-005): capacity is read with len(participants) outside any
#   transaction, so two users can take the last seat at once. The design locks
#   the activity row first.
# GAP (FR-PROF-009): the university check is gated on
#   `university_finalized`, so cross-campus joins are possible by default.
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

# FR-PE-002 request to join an APPROVAL_REQUIRED activity.
# GAP (FR-PE-002): the optional message to current attendees is not read from
#   the form, and no eligibility check runs before the request is stored.
# GAP (FR-PE-002): no notification is published. The design emits
#   JoinRequestCreated to the Admin and Editors.
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

# FR-PE-002 approve or deny a join request.
# GAP (FR-PE-004): approval adds a member without re-checking capacity, so an
#   activity can be pushed past max_members. The design re-runs the whole
#   eligibility check at approval time, because seats may have gone.
# GAP (FR-PE-002): the request row is deleted rather than moved to
#   APPROVED/DENIED with reviewed_by and decided_at, so there is no record to
#   notify the requester from and no audit trail.
# GAP (FR-ACE-007): is_editor_for() is the only gate; Attendees are excluded
#   correctly but Admin and Editor are not distinguished.
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

# FR-PE-002 supporting route: withdraw one's own pending request.
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

# FR-PE-006 leave an activity.
# GAP (FR-PE-006, FR-ACE-007): the requirement is that a participant may leave
#   "regardless of their privilege/role". This refuses outright when the user
#   is the last editor. FR-ACE-007 instead requires prompting the Admin to
#   name a successor from the roster, and promoting the earliest joinedAt
#   member when none is chosen (the design's SuccessionService).
# GAP (FR-PE-006): leaving as the only member must cancel the activity.
# GAP (FR-PE-010): a seat freed by leaving must draw from the waitlist,
#   first-come-first-served.
# GAP (NFR-REL-005): removal, succession, promotion and cancellation have to
#   commit as one transaction, so the activity is never left with zero or two
#   admins or a stale attendee count.
# GAP (NFR-REL-002): a distinct confirmation step is required before an
#   action that removes access.
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

# FR-MESS-002 post a non-empty message; FR-MESS-003 and FR-MESS-005 topics.
# GAP (FR-MESS-004): no participation check, so any authenticated user can
#   post to any activity's board by id. P1.
# GAP (FR-MESS-002): the 500-character cap is not enforced.
# GAP (FR-MESS-009): delivery is a redirect, so other participants only see
#   the message after reloading. The design uses WebSocket plus Redis pub/sub.
# GAP (FR-MESS-007, FR-MESS-008): no edit/delete with the
#   "message deleted by author" placeholder, and no @mention parsing.
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

# FR-ACE-001 supporting route: renders the activity creation form.
@bp.route("/form")
@flask_login.login_required
def form():
    return render_template("main/activity_form.html")

# FR-PROF-005 follow a user.
# GAP (FR-PROF-005): this appends to `user.activity_proposals`, the activity
#   relationship, not a follow relationship -- following a user adds them to an
#   activity list. The profile view reads `user.followers` instead, so the two
#   halves disagree.
# GAP (FR-PROF-005): private accounts need the follow created as PENDING until
#   the owner accepts (the design's FollowService).
# GAP (FR-PROF-009): no check that both users share a university.
@bp.route("/follow/<int:user_id>", methods=["POST"])
@flask_login.login_required
def follow(user_id):
    user = db.get_or_404(model.User, user_id)
    if (flask_login.current_user not in user.activity_proposals):
        user.activity_proposals.append(flask_login.current_user)
        db.session.commit()
    return redirect(url_for("main.profile", user_id=user_id))

# FR-PROF-005 unfollow a user. Same wrong relationship as follow() above.
@bp.route("/unfollow/<int:user_id>", methods=["POST"])
@flask_login.login_required
def unfollow(user_id):
    user = db.get_or_404(model.User, user_id)
    if (flask_login.current_user in user.activity_proposals):
        user.activity_proposals.remove(flask_login.current_user)
        db.session.commit()
    return redirect(url_for("main.profile", user_id=user_id))


# NOT IN THE SRS OR DESIGN DOC. Meetup is not one of the 21 tables in the
# design's Data Design section and no FR covers it. Either add a requirement
# for it or drop it -- undesigned entities are what NFR-SOFT-009 is meant to
# prevent.
# GAP (FR-MESS-004 equivalent): no participation check, so any authenticated
#   user can read any activity's meetups.
# NFR-REL-001: location here must stay user-entered text; no device location.
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

# NOT IN THE SRS OR DESIGN DOC -- see activity_meetup_retrieval above.
# GAP (NFR-SOFT-006): returns str(e) to the client, which can leak SQL
#   fragments, internal identifiers and framework details.
# GAP: no participation or role check before creating a meetup.
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
    
# NOT IN THE SRS OR DESIGN DOC -- see activity_meetup_retrieval above.
# GAP (NFR-SECU-004): any authenticated user can delete any meetup; the
#   meetup is not even checked against the activity in the URL.
# GAP (NFR-REL-002): destructive, with no distinct confirmation step.
# GAP (NFR-SOFT-006): returns str(e) to the client.
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
    
# FR-AD-002 browse with filters; FR-AD-003 age eligibility.
# GAP (FR-AD-002): "Only joinable activities should be shown". status is an
#   optional client filter, so CLOSED, FINALIZED and CANCELLED activities are
#   listed by default, as are activities that are already full.
# GAP (FR-AD-002): no activity type filter and no school-year range filter.
#   Filters on origin and destination are not in the requirement.
# GAP (FR-AD-003): the requirement is to compute the VIEWER's age from their
#   birthday and show activities where that age falls within minAge..maxAge.
#   This filters the activity's own bounds against client-supplied numbers and
#   never reads the viewer's birthday. model.age_from_birthday is the shared
#   calculator to use.
# GAP (FR-PROF-009, NFR-SECU-010): university scoping applies only when the
#   client sends a `university` parameter, so omitting it returns every
#   campus. This must be an unconditional query-layer predicate.
# GAP: home_uni / visiting_uni and the "home_uni"/"visiting_uni"/"both" values
#   appear in neither the SRS nor the design doc, which give User a single
#   university. Needs a requirement before it is built on.
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