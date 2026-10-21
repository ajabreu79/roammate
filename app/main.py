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

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
from werkzeug.utils import secure_filename
import flask_login

from . import db
from . import model
from .activity_rules import (
    ADMIN, ATTENDEE, EDITOR,
    ActivityStatusMachine, ActivityValidator, EligibilityChecker, RolePolicy,
)

from sqlalchemy import or_

from datetime import datetime
import os
import dateutil.tz



bp = Blueprint("main", __name__)

upload_folder = "static/resources"


# FR-MESS-002
MAX_MESSAGE_LENGTH = 500

# FR-MESS-003: the default forum topic. One spelling everywhere -- "Main" and
# "main" in different places would split a board in two.
DEFAULT_FORUM_TOPIC = "Main"


def _status_name(value):
    """Map a stored status value to the name the rule classes use."""
    try:
        return model.ActivityProposalStatus(value).name
    except ValueError:
        return str(value)


def _role_in(activity, user):
    """The user's role in this activity (FR-ACE-007), or None if not a member.

    GAP (FR-ACE-007): ActivityProposalParticipation has only an is_editor
      boolean, so Admin is derived from being the creator. The model needs a
      real role column before admin succession (FR-ACE-007) or transfer can be
      built -- a derived Admin cannot be handed to someone else.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    participation = db.session.execute(
        db.select(model.ActivityProposalParticipation).where(
            model.ActivityProposalParticipation.user_id == user.id,
            model.ActivityProposalParticipation.activity_proposal_id == activity.id,
        )
    ).scalar_one_or_none()

    if participation is None:
        return None
    if activity.creator_id == user.id:
        return ADMIN
    return EDITOR if participation.is_editor else ATTENDEE


def _university_scoped(query):
    """Add the FR-PROF-009 / NFR-SECU-010 university predicate.

    Unconditional by design: the SRS inspection finding on NFR-SECU-010
    rejected university separation that depends on a UI filter, because
    omitting the parameter then returns every campus. Scoping is by the
    creator's university, since a user belongs to exactly one.

    GAP (NFR-SECU-010): the design enforces this with a UniversityScope base
      repository plus PostgreSQL row-level security, so a query cannot opt
      out. This helper still has to be called explicitly.
    """
    return (
        query.join(model.User, model.ActivityProposal.creator_id == model.User.id)
             .where(model.User.university == flask_login.current_user.university)
    )


# FR-AD-001 home feed, OPEN only and scoped to the viewer's university;
# FR-AD-004 card contents (template); FR-AD-006 scroll.
# GAP (FR-AD-006): limit(4) with no cursor; infinite scroll needs keyset
#   pagination on (timestamp, id) per the design's CursorPaginator.
# GAP (FR-AD-001): "prioritize accounts the user follows" (FR-PROF-006) is not
#   applied -- ordering is newest-first for everyone.
@bp.route("/")
@flask_login.login_required
def index():
    # FR-AD-001: OPEN only -- APPROVAL_REQUIRED is not an open proposal -- and
    # restricted to the viewer's university.
    query = _university_scoped(
        db.select(model.ActivityProposal).where(
            model.ActivityProposal.status == model.ActivityProposalStatus.OPEN.value
        )
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

# FR-ACE-001 create an activity; FR-ACE-002 defaults to OPEN;
# FR-ACE-003 creator becomes a participant with Admin privileges;
# FR-ACE-004 field validation via ActivityValidator.
# GAP (FR-ACE-001): no activity type/theme, no allowed year range, no allowed
#   gender preferences -- the model has no columns for them. The default image
#   filename is still a placeholder ("example-activity.jpg").
# GAP (NFR-SECU-008): the upload is saved under secure_filename(), so names
#   are guessable and two activities sharing a filename overwrite each other.
#   No content-type or size check and no EXIF stripping. See
#   app/auth.py:_save_profile_pic for the pattern this needs.
@bp.route("/activity", methods=["POST"])
@flask_login.login_required
def new_activity():
    user = flask_login.current_user
    # FR-ACE-004: validate before anything is written. ActivityValidator
    # returns every failure at once so the form can show them together.
    errors, cleaned = ActivityValidator.validate(
        title=request.form.get("title"),
        description=request.form.get("description"),
        meeting_point=request.form.get("departure_location"),
        start=request.form.get("start"),
        end=request.form.get("end"),
        budget=request.form.get("budget"),
        max_members=request.form.get("max_members"),
        min_age=request.form.get("min"),
        max_age=request.form.get("max"),
    )
    if errors:
        for error in errors:
            flash(error, "error")
        return redirect(url_for("main.form"))

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

    # FR-ACE-003: the creator is a participant with Admin privileges. Flushed
    # rather than committed first so the activity and its one membership land
    # together (NFR-REL-005) -- an activity with no Admin is the state
    # FR-ACE-007 forbids.
    db.session.flush()
    db.session.add(model.ActivityProposalParticipation(
        user_id=user.id,
        activity_proposal_id=new_activity.id,
        is_editor=True,
    ))
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=new_activity.id))

# FR-ACE-006 grant editor access, to a participant, by the Admin only
# (FR-ACE-007 via RolePolicy).
# GAP (NFR-SECU-012): role grants are privileged actions and must be appended
#   to the audit log.
# GAP (FR-ACE-007): there is no revoke-Editor route.
@bp.route("/make_editor/<int:activity_id>/<int:participant_id>", methods=["POST"])
@flask_login.login_required
def make_editor(activity_id, participant_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)
    user = flask_login.current_user
    
    # FR-ACE-007: granting Editor is Admin-only, per the design's RolePolicy.
    if not RolePolicy.can(_role_in(activity, user), "grant_editor"):
        flash("You do not have permission to promote editors for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-ACE-006: only a registered participant can be made an Editor.
    if _role_in(activity, db.session.get(model.User, participant_id)) is None:
        flash("Only a participant of this activity can be made an editor.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    editor = model.ActivityProposalParticipation.query.get((participant_id, activity.id))
    editor.is_editor = True
    db.session.commit()
    return redirect(url_for("main.activity", activity_id=activity_id))

# FR-ACE-005 Admin and Editor update non-finalized fields (RolePolicy);
# FR-ACE-008 finalization is one-way and gated on the finalize permission;
# FR-ACE-009 status changes run through ActivityStatusMachine.
# GAP (FR-ACE-008): finalization is held in *_finalized boolean columns. The
#   design stores a FinalizedField row per field, with who finalized it and
#   when, which these columns cannot record.
# GAP (FR-ACE-004): edits are not re-validated, so an edit can still set
#   end before start or a negative budget. ActivityValidator should run here
#   as well as in new_activity.
# GAP (NFR-SECU-008): same upload handling as new_activity.
@bp.route("/edit_activity/<int:activity_id>", methods=["POST"])
@flask_login.login_required
def edit_activity(activity_id):
    activity = model.ActivityProposal.query.get_or_404(activity_id)
    user = flask_login.current_user
    
    role = _role_in(activity, user)
    current_status = _status_name(activity.status)

    # FR-ACE-008: comparing against the status NAME, not the enum member --
    # activity.status holds the enum's value, so the previous comparison with
    # the member itself was never true and finalized activities stayed editable.
    if ActivityStatusMachine.is_terminal(current_status):
        flash("This activity has been finalized or cancelled and cannot be edited.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-ACE-005, NFR-SECU-004: Admin and Editor only. The previous condition
    # was `(user not in participants) and not is_editor`, which evaluates False
    # for a plain Attendee and let every attendee through.
    if not RolePolicy.can(role, "edit_fields"):
        flash("You do not have permission to edit this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()

    # FR-ACE-009: run the requested status change through the transition table
    # rather than assigning whatever the form sent.
    status = request.form.get("status")
    if status:
        transition_errors = ActivityStatusMachine.check_transition(
            current_status, _status_name(status), role,
            status_finalized=bool(activity.status_finalized),
        )
        if transition_errors:
            for error in transition_errors:
                flash(error, "error")
            return redirect(url_for("main.activity", activity_id=activity.id))
    
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
        
    # FR-ACE-008: finalizing makes a field immutable, so these only ever go
    # from False to True. Assigning `name in request.form` directly meant
    # submitting the form without a checkbox un-finalized the field.
    # Only Admin and Editor may finalize.
    if RolePolicy.can(role, "finalize_field"):
        for field in ("origin", "destinations", "dates", "budget",
                      "max_members", "age_range", "status", "university"):
            attribute = f"{field}_finalized"
            if f"{attribute}" in request.form and not getattr(activity, attribute, False):
                setattr(activity, attribute, True)
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
       

# FR-PE-007 member list; FR-MESS-001 message board, participants only
# (FR-MESS-004); FR-MESS-003 forum topics; FR-MESS-005 switching between
# topics; FR-MESS-006 author and timestamp.
# GAP (FR-MESS-003): topics are still derived from DISTINCT
#   message.forum_topic, so a topic with no messages disappears. The design
#   gives ForumTopic its own table, created with the activity.
# GAP (FR-PROF-009): the activity itself is fetched unscoped, so its details
#   are readable across universities even though its board is not.
@bp.route("/activity/<int:activity_id>")
@bp.route("/activity/<int:activity_id>/forum/<forum_topic>")
@flask_login.login_required
def activity(activity_id, forum_topic=None):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user
    already_joined = user in activity.participants
    
    role = _role_in(activity, user)
    join_requests = []
    requesting_users = []

    if already_joined:
        is_editor_query = db.select(model.ActivityProposalParticipation.is_editor).where(
            model.ActivityProposalParticipation.user_id == user.id,
            model.ActivityProposalParticipation.activity_proposal_id == activity.id
        )
        is_editor = db.session.execute(is_editor_query).scalar_one_or_none()
        if is_editor:
            if activity.status == model.ActivityProposalStatus.APPROVAL_REQUIRED.value:
                join_requests_query = db.select(model.ActivityProposalJoinRequest).where(model.ActivityProposalJoinRequest.activity_proposal_id == activity.id).order_by(model.ActivityProposalJoinRequest.timestamp.asc())
                join_requests = db.session.execute(join_requests_query).scalars().all()
                for req in join_requests:
                    requesting_users.append(db.get_or_404(model.User, req.user_id))
                join_requests = [(req, user) for req, user in zip(join_requests, requesting_users)]

    # FR-MESS-004: the board is visible to participants only. It used to be
    # loaded for anyone who could reach the URL.
    forum_topics = []
    active_topic = forum_topic or DEFAULT_FORUM_TOPIC
    active_forum_messages = []

    if RolePolicy.can(_role_in(activity, user), "view"):
        topics_query = (db.select(model.ActivityProposalMessage.forum_topic).where(model.ActivityProposalMessage.activity_proposal_id == activity_id).distinct().order_by(model.ActivityProposalMessage.forum_topic))
        forum_topics = db.session.execute(topics_query).scalars().all()

        active_forum_messages_query = (db.select(model.ActivityProposalMessage).where(model.ActivityProposalMessage.activity_proposal_id == activity_id, model.ActivityProposalMessage.forum_topic == active_topic).order_by(model.ActivityProposalMessage.timestamp.asc()))
        active_forum_messages = db.session.execute(active_forum_messages_query).scalars().all()

    return render_template(
        "main/activity.html",
        activity=activity,
        statuses=model.ActivityProposalStatus,
        # FR-ACE-007: the template needs the role to decide which controls to
        # show. Server-side checks above remain the access control
        # (NFR-SECU-004); this only drives the UI.
        role=role,
        join_requests=join_requests,
        already_joined=already_joined,
        forum_topics=forum_topics,
        active_topic=active_topic,
        active_forum_messages=active_forum_messages,
    )

# FR-PE-001 join an OPEN activity. FR-PE-003, 004, 005, 008 and FR-PROF-009
# are all enforced by EligibilityChecker, which reports every failed rule.
# GAP (FR-PE-005, FR-PE-009): EligibilityChecker checks school year and
#   gender preference, but the model has no allowed-years or allowed-genders
#   columns to pass it, so those two rules are inert here.
# GAP (FR-PE-010): a full activity should offer the waitlist; this reports an
#   error instead. No waitlist exists.
# GAP (NFR-REL-005): capacity is read with len(participants) outside a
#   transaction, so two users can still take the last seat at once. The design
#   locks the activity row first, which is the only way to close this.
@bp.route("/activity/<int:activity_id>/join", methods=["POST"])
@flask_login.login_required
def join_activity(activity_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    # FR-PE-001 vs FR-PE-002: only OPEN admits a member without approval.
    if not ActivityStatusMachine.accepts_direct_join(_status_name(activity.status)):
        flash("This activity is not open for joining.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-PE-003 to 009, all in one place. Replaces a chain that referenced an
    # undefined `trip`, gated the age check on age_range_finalized, and gated
    # the university check on university_finalized.
    errors = EligibilityChecker.check(
        status=_status_name(activity.status),
        already_member=_role_in(activity, user) is not None,
        attendee_count=len(activity.participants),
        max_members=activity.max_members,
        viewer_age=user.age,
        min_age=activity.min_age,
        max_age=activity.max_age,
        viewer_university=user.university,
        activity_university=getattr(activity.creator, "university", None),
    )
    if errors:
        for error in errors:
            flash(error, "error")
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

# FR-PE-002 approve or deny a join request, Admin and Editor only
# (FR-ACE-007); FR-PE-004 eligibility is re-checked at approval time.
# GAP (FR-PE-002): the request row is deleted rather than moved to
#   APPROVED/DENIED with reviewed_by and decided_at, so there is no record to
#   notify the requester from and no audit trail.
# GAP (FR-PE-002): no notification is published on the decision. The design
#   emits JoinRequestDecided to the requester, in-app and by email.
@bp.route("/activity/<int:activity_id>/update_join_request/<int:requesting_user_id>", methods=["POST"])
@flask_login.login_required
def update_join_request(activity_id, requesting_user_id):
    activity = db.get_or_404(model.ActivityProposal, activity_id)
    user = flask_login.current_user

    # FR-ACE-007: reviewing requests is Admin and Editor only.
    if not RolePolicy.can(_role_in(activity, user), "review_requests"):
        flash("You do not have permission to manage join requests for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    action = request.form.get("approval_status")
    join_request = db.session.get(model.ActivityProposalJoinRequest, (activity.id, requesting_user_id))
    if not join_request:
        return redirect(url_for("main.activity", activity_id=activity_id))

    if action == "approve":
        # FR-PE-004: re-check eligibility now, not when the request was made.
        # Seats may have gone while it waited, and approving blind could push
        # the activity past max_members.
        requesting_user = db.get_or_404(model.User, requesting_user_id)
        errors = EligibilityChecker.check(
            status=_status_name(activity.status),
            already_member=_role_in(activity, requesting_user) is not None,
            attendee_count=len(activity.participants),
            max_members=activity.max_members,
            viewer_age=requesting_user.age,
            min_age=activity.min_age,
            max_age=activity.max_age,
            viewer_university=requesting_user.university,
            activity_university=getattr(activity.creator, "university", None),
        )
        if errors:
            for error in errors:
                flash(error, "error")
            return redirect(url_for("main.activity", activity_id=activity_id))

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

# FR-MESS-002 non-empty and 500 characters or fewer; FR-MESS-004 participants
# only; FR-MESS-003 and FR-MESS-005 topics.
# GAP (FR-MESS-009): delivery is a redirect, so other participants only see
#   the message after reloading. The design uses WebSocket plus Redis pub/sub.
# GAP (FR-MESS-007, FR-MESS-008): no edit/delete with the
#   "message deleted by author" placeholder, and no @mention parsing.
@bp.route("/activity/<int:activity_id>/message", methods=["POST"])
@flask_login.login_required
def new_message(activity_id):
    user = flask_login.current_user
    activity = db.get_or_404(model.ActivityProposal, activity_id)

    content = request.form.get("message-content", "").strip()
    forum_topic = request.form.get("forum_topic", DEFAULT_FORUM_TOPIC).strip() #to get the specific channel/forum of the messages
    new_forum_topic = request.form.get("new_forum_topic", "").strip() #if user wants to make a new forum for some specific messages
    if new_forum_topic and new_forum_topic != "":
        forum_topic = new_forum_topic

    # FR-MESS-004: only participants may post. There was no check at all, so
    # any authenticated user could post to any board by id.
    if not RolePolicy.can(_role_in(activity, user), "post_message"):
        flash("Only participants of this activity can post messages.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-MESS-002: non-empty and 500 characters or fewer. The cap was missing.
    if not content:
        flash("Message cannot be empty", "error")
        return redirect(url_for("main.activity", activity_id=activity_id, forum_topic=forum_topic))

    if len(content) > MAX_MESSAGE_LENGTH:
        flash(f"Messages are limited to {MAX_MESSAGE_LENGTH} characters.", "error")
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

    # FR-PROF-009 / NFR-SECU-010: scoping is applied first and unconditionally.
    # It used to run only when the client sent a `university` parameter, so
    # omitting it returned every campus -- the leak the SRS inspection finding
    # on NFR-SECU-010 resolved against.
    #
    # The home_uni / visiting_uni block that stood here was removed: it read
    # User.home_uni and User.visiting_uni and compared ActivityProposal.university
    # against the literals "home_uni", "visiting_uni" and "both". None of those
    # fields or values appears in the SRS or the design doc, both of which give
    # a user exactly one university, and none exists on the model. Restore it
    # only behind a requirement that defines what a visiting university is.
    activities_query = _university_scoped(db.select(model.ActivityProposal))

    # FR-AD-002: "Only joinable activities should be shown." Status was an
    # optional client filter, so CLOSED, FINALIZED and CANCELLED activities
    # were listed by default.
    joinable_values = [
        model.ActivityProposalStatus[name].value
        for name in ("OPEN", "APPROVAL_REQUIRED")
    ]
    activities_query = activities_query.where(
        model.ActivityProposal.status.in_(joinable_values)
    )

    max_budget = request.args.get("max_budget", type=float)
    if max_budget:
        activities_query = activities_query.where(model.ActivityProposal.budget <= max_budget)

    # FR-AD-003: the filter is on the VIEWER's age, derived from their
    # birthday, not on client-supplied numbers. An activity qualifies when the
    # viewer falls inside its own min_age..max_age bounds.
    viewer_age = user.age
    if viewer_age is not None:
        activities_query = activities_query.where(
            or_(model.ActivityProposal.min_age.is_(None),
                model.ActivityProposal.min_age <= viewer_age),
            or_(model.ActivityProposal.max_age.is_(None),
                model.ActivityProposal.max_age >= viewer_age),
        )

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    if start_date:
        activities_query = activities_query.where(model.ActivityProposal.start_date >= start_date)
    if end_date:
        activities_query = activities_query.where(model.ActivityProposal.end_date <= end_date)

    origin = request.args.get("origin")
    if origin:
        activities_query = activities_query.where(model.ActivityProposal.origin.ilike(f"%{origin}%"))
        #referenced ChatGPT for 'ilike', which compares with case insensitive values

    destination = request.args.get("destination")
    if destination:
        activities_query = activities_query.where(
            model.ActivityProposal.destinations.ilike(f"%{destination}%"))

    # GAP (FR-AD-002): still missing the activity type filter and the
    #   school-year range filter the requirement lists, and a full activity is
    #   not excluded -- that needs a stored attendee_count to filter on.
    # GAP (FR-AD-006): no cursor, so this is one page rather than infinite
    #   scroll. The design uses keyset pagination on (createdAt, id).
    # GAP (FR-AD-002): start_date / end_date are read here but new_activity
    #   writes start_time / end_time. One of the two names is wrong; the model
    #   has to settle it before either filter works.

    activities = db.session.execute(
        activities_query.order_by(model.ActivityProposal.timestamp.desc())
    ).scalars().all()

    return render_template("main/browse_activities.html", activities=activities)