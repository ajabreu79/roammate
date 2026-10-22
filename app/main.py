"""Activity, participation, messaging and discovery controllers.

REQUIREMENTS TRACEABILITY
Every route is tagged with the SRS requirements it implements. Where one is
only partly met the gap is recorded as a GAP comment beside the code
responsible rather than left to be rediscovered. Grep "GAP" to list them.

DESIGN DEVIATIONS that apply to this whole file:

  - Layering. The design specifies Controller -> Service -> Repository per
    module, with authorization and transaction boundaries in the service layer
    (NFR-SECU-004, NFR-REL-005). This file holds controllers, business logic
    and queries together, so there is no layer where a rule is enforced once
    for every caller. The rules themselves are extracted into
    app/activity_rules.py, which is as far as that goes without the modules.

  - Module boundaries. The design splits these routes across five modules
    (Activity, Participation, Messaging, Discover, Profile), each owning its
    tables and route prefix. They are all in the `main` blueprint here.

  - Route paths. The design uses /activities/{id}/join, /leave, /requests,
    /users/{id}/follow, /feed and /browse. This file uses /activity/{id}/join,
    /follow/{id}, /browse_activities, /make_editor/... and /edit_activity/...

  - University scoping is applied by calling _university_scoped explicitly.
    The design enforces it with a UniversityScope base repository plus
    PostgreSQL row-level security, so a query cannot opt out.

The schema is now the design doc's (model.py), so entity and field names match
it: Activity, ActivityMembership with a role enum, FinalizedField rows,
ForumTopic and Message.
"""

import os
import uuid
from datetime import datetime

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for,
)
import flask_login

from sqlalchemy import or_
from werkzeug.utils import secure_filename

from . import db
from . import model
from .activity_rules import (
    ActivityStatusMachine, ActivityValidator, EligibilityChecker, RolePolicy,
)

bp = Blueprint("main", __name__)

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# FR-MESS-003: the topic every activity starts with.
DEFAULT_FORUM_TOPIC = "Main"


# --- shared helpers ----------------------------------------------------------

def _role_in(activity, user):
    """The user's role in this activity (FR-ACE-007), or None if not a member."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None

    membership = db.session.get(model.ActivityMembership, (activity.id, user.id))
    return membership.role if membership else None


def _university_scoped(query):
    """Add the FR-PROF-009 / NFR-SECU-010 university predicate.

    Unconditional by design: the SRS inspection finding on NFR-SECU-010
    rejected university separation that depends on a UI filter, because
    omitting the parameter then returns every campus.
    """
    return query.where(
        model.Activity.university_id == flask_login.current_user.university_id
    )


def _visible_activity_or_404(activity_id):
    """Fetch an activity, but only within the viewer's university.

    A cross-university id is a 404 rather than a 403, so the response does not
    confirm that the activity exists (NFR-SECU-010).
    """
    activity = db.session.execute(
        _university_scoped(db.select(model.Activity).where(model.Activity.id == activity_id))
    ).scalar_one_or_none()
    if activity is None:
        from flask import abort
        abort(404)
    return activity


def _save_activity_image(upload):
    """Store an activity image. Returns (key, errors).

    NFR-SECU-008: the key is random rather than taken from the upload, so two
    activities cannot overwrite each other and keys are not guessable.
    """
    _, extension = os.path.splitext(secure_filename(upload.filename))
    extension = extension.lower()
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        return None, ["Activity images must be a PNG, JPG, GIF or WEBP image."]

    key = f"{uuid.uuid4().hex}{extension}"
    directory = current_app.config.get("ACTIVITY_IMAGE_DIR") or os.path.join(
        current_app.static_folder, "resources")
    os.makedirs(directory, exist_ok=True)
    upload.save(os.path.join(directory, key))
    return key, []
    # GAP (NFR-SECU-008): still missing the size check and the re-encode that
    #   strips EXIF, including GPS. The design puts both in a shared
    #   ImageService that this and auth._save_profile_pic should both call.


def _finalized(activity):
    """The set of field names already finalized (FR-ACE-008)."""
    return {row.field_name for row in activity.finalized_fields}


def _flash_all(messages, category="error"):
    for message in messages:
        flash(message, category)


def _replace_destinations(activity, texts):
    """FR-ACE-001: destinations are ordered rows, not a joined string.

    Stored per-row so a destination containing a comma survives, and so
    position is explicit.
    """
    activity.destinations.clear()
    for position, text in enumerate(text.strip() for text in texts):
        if text:
            activity.destinations.append(
                model.ActivityDestination(position=position, text=text))


# --- FR-AD-001 home feed -----------------------------------------------------

# FR-AD-001 latest OPEN activities, viewer's university only; FR-AD-004 card
# contents (template); FR-AD-006 scroll.
# GAP (FR-AD-006): limit(4) with no cursor. Infinite scroll needs keyset
#   pagination on (created_at, id) per the design's CursorPaginator.
# GAP (FR-PROF-006): activities from followed creators are not prioritized;
#   ordering is newest-first for everyone.
@bp.route("/")
@flask_login.login_required
def index():
    query = _university_scoped(
        db.select(model.Activity).where(model.Activity.status == model.STATUS_OPEN)
    ).order_by(model.Activity.created_at.desc()).limit(4)
    latest_activities = db.session.execute(query).scalars().all()

    return render_template("main/index.html", latest_activities=latest_activities)


# --- FR-PROF-001 profile -----------------------------------------------------

# FR-PROF-001 view another user's profile, with past and upcoming activities;
# FR-PROF-003 derived age; FR-PROF-005 follow state.
# NFR-SECU-009: the template renders user.age, never birthday or email.
@bp.route("/profile/<user_id>")
@flask_login.login_required
def profile(user_id):
    user = db.get_or_404(model.User, user_id)
    viewer = flask_login.current_user

    # FR-PROF-009: profiles are visible only within one university.
    if user.university_id != viewer.university_id:
        from flask import abort
        abort(404)

    memberships = db.session.execute(
        db.select(model.ActivityMembership)
        .join(model.Activity)
        .where(model.ActivityMembership.user_id == user.id)
        .order_by(model.Activity.start_at.desc())
    ).scalars().all()

    now = model.utcnow()
    past_activities = [m.activity for m in memberships if m.activity.end_at < now]
    upcoming_activities = [m.activity for m in memberships if m.activity.end_at >= now]

    if viewer.id == user.id:
        follow_button = None  # viewing their own profile, no follow button
    else:
        follow = db.session.get(model.Follow, (viewer.id, user.id))
        follow_button = "unfollow" if follow else "follow"

    return render_template(
        "main/profile.html",
        user=user,
        past_activities=past_activities,
        upcoming_activities=upcoming_activities,
        follow_button=follow_button,
    )


# --- FR-ACE-001 to 004 activity creation -------------------------------------

# FR-ACE-001 create an activity; FR-ACE-002 defaults to OPEN;
# FR-ACE-003 creator becomes the Admin; FR-ACE-004 field validation.
# GAP (FR-ACE-001): the form does not yet collect activity type, the school
#   year range or allowed genders, though the schema has columns for all three.
@bp.route("/activity", methods=["POST"])
@flask_login.login_required
def new_activity():
    user = flask_login.current_user

    # FR-ACE-004: validate before anything is written, reporting every failure
    # at once so the form can show them together.
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
        _flash_all(errors)
        return redirect(url_for("main.form"))

    image_key = None
    upload = request.files.get("activity_image")
    if upload and upload.filename:
        image_key, image_errors = _save_activity_image(upload)
        if image_errors:
            _flash_all(image_errors)
            return redirect(url_for("main.form"))

    activity = model.Activity(
        university_id=user.university_id,
        creator_id=user.id,
        title=request.form.get("title").strip(),
        description=request.form.get("description").strip(),
        meeting_point=request.form.get("departure_location").strip(),
        start_at=cleaned["start"],
        end_at=cleaned["end"],
        budget=cleaned["budget"],
        max_attendees=cleaned["max_members"],
        min_age=cleaned["min_age"],
        max_age=cleaned["max_age"],
        image_key=image_key,
        # FR-ACE-002
        status=model.STATUS_OPEN,
        # FR-ACE-003: the creator is the first attendee.
        attendee_count=1,
    )
    db.session.add(activity)
    _replace_destinations(activity, request.form.getlist("destination"))

    # FR-ACE-003 and FR-MESS-003 in the same transaction as the activity
    # (NFR-REL-005): an activity with no Admin, or none with a main topic, is
    # a state the rest of the system does not expect.
    db.session.add(model.ActivityMembership(
        activity=activity, user_id=user.id, role=model.ROLE_ADMIN))
    db.session.add(model.ForumTopic(
        activity=activity, name=DEFAULT_FORUM_TOPIC, created_by=user.id))

    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity.id))


# --- FR-ACE-006 role grants --------------------------------------------------

# FR-ACE-006 grant editor access, to a participant, by the Admin only
# (FR-ACE-007 via RolePolicy).
# GAP (NFR-SECU-012): role grants are privileged actions and must be appended
#   to the audit log. model.AuditLog exists; nothing writes to it yet.
# GAP (FR-ACE-007): there is no revoke-Editor route, and no transfer-Admin
#   route -- the latter needs SuccessionService.
@bp.route("/make_editor/<activity_id>/<participant_id>", methods=["POST"])
@flask_login.login_required
def make_editor(activity_id, participant_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    if not RolePolicy.can(_role_in(activity, user), "grant_editor"):
        flash("You do not have permission to promote editors for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    membership = db.session.get(model.ActivityMembership, (activity.id, participant_id))
    if membership is None:
        flash("Only a participant of this activity can be made an editor.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-ACE-007: there is exactly one Admin, so promoting to Editor must not
    # touch the Admin's own row.
    if membership.role == model.ROLE_ADMIN:
        flash("The activity admin cannot be changed to an editor.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    membership.role = model.ROLE_EDITOR
    db.session.commit()
    return redirect(url_for("main.activity", activity_id=activity_id))


# --- FR-ACE-005, 008, 009 editing --------------------------------------------

# FR-ACE-005 Admin and Editor update non-finalized fields (RolePolicy);
# FR-ACE-008 finalization is one-way, recorded as FinalizedField rows;
# FR-ACE-009 status changes run through ActivityStatusMachine.
# GAP (FR-ACE-004): edits are validated field by field below rather than as a
#   whole, so a pair that is individually fine but jointly wrong (a new end
#   before an existing start) is not caught. ActivityValidator should run over
#   the merged result.
@bp.route("/edit_activity/<activity_id>", methods=["POST"])
@flask_login.login_required
def edit_activity(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user
    role = _role_in(activity, user)

    if ActivityStatusMachine.is_terminal(activity.status):
        flash("This activity has been finalized or cancelled and cannot be edited.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-ACE-005, NFR-SECU-004: Admin and Editor only.
    if not RolePolicy.can(role, "edit_fields"):
        flash("You do not have permission to edit this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    finalized = _finalized(activity)

    # FR-ACE-009: run a requested status change through the transition table
    # rather than assigning whatever the form sent.
    status = request.form.get("status")
    if status and status != activity.status:
        transition_errors = ActivityStatusMachine.check_transition(
            activity.status, status, role, status_finalized="status" in finalized,
        )
        if transition_errors:
            _flash_all(transition_errors)
            return redirect(url_for("main.activity", activity_id=activity.id))
        activity.status = status

        # FR-ACE-009: moving to FINALIZED finalizes every field.
        if status == model.STATUS_FINALIZED:
            for field in model.FINALIZABLE_FIELDS:
                if field not in finalized:
                    db.session.add(model.FinalizedField(
                        activity_id=activity.id, field_name=field, finalized_by=user.id))
            finalized = set(model.FINALIZABLE_FIELDS)

    title = request.form.get("title", "").strip()
    if title:
        activity.title = title

    description = request.form.get("description", "").strip()
    if description:
        errors = [] if len(description) <= model.MAX_DESCRIPTION_LENGTH else [
            f"Description must be {model.MAX_DESCRIPTION_LENGTH} characters or fewer."]
        if errors:
            _flash_all(errors)
            return redirect(url_for("main.activity", activity_id=activity_id))
        activity.description = description

    # Each field is writable only while it is not finalized (FR-ACE-008).
    meeting_point = request.form.get("departure_location", "").strip()
    if meeting_point and "meeting_point" not in finalized:
        activity.meeting_point = meeting_point

    destinations = request.form.getlist("destination")
    if destinations and "destinations" not in finalized:
        _replace_destinations(activity, destinations)

    if "dates" not in finalized:
        errors, cleaned = ActivityValidator.validate(
            title=activity.title, description=activity.description,
            meeting_point=activity.meeting_point,
            start=request.form.get("start"), end=request.form.get("end"),
        )
        if request.form.get("start") or request.form.get("end"):
            if errors:
                _flash_all(errors)
                return redirect(url_for("main.activity", activity_id=activity_id))
            activity.start_at = cleaned["start"]
            activity.end_at = cleaned["end"]

    if "budget" not in finalized and request.form.get("budget"):
        activity.budget = request.form.get("budget")

    if "max_attendees" not in finalized and request.form.get("max_members"):
        # The schema caps attendee_count at max_attendees, so shrinking below
        # the current roster is rejected by the database rather than silently
        # leaving the activity over capacity.
        activity.max_attendees = request.form.get("max_members")

    if "age_range" not in finalized:
        if request.form.get("min_age"):
            activity.min_age = request.form.get("min_age")
        if request.form.get("max_age"):
            activity.max_age = request.form.get("max_age")

    # FR-ACE-008: finalizing is one-way and needs the finalize permission. A
    # row's presence is the fact, so there is no way to un-finalize by
    # omitting a checkbox -- which the previous boolean columns allowed.
    if RolePolicy.can(role, "finalize_field"):
        for field in model.FINALIZABLE_FIELDS:
            if f"{field}_finalized" in request.form and field not in finalized:
                db.session.add(model.FinalizedField(
                    activity_id=activity.id, field_name=field, finalized_by=user.id))

    upload = request.files.get("activity_image")
    if upload and upload.filename:
        key, image_errors = _save_activity_image(upload)
        if image_errors:
            _flash_all(image_errors)
            return redirect(url_for("main.activity", activity_id=activity_id))
        activity.image_key = key

    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))


# --- FR-PE-007, FR-MESS-001 activity detail and board ------------------------

# FR-PE-007 member list; FR-MESS-001 message board, participants only
# (FR-MESS-004); FR-MESS-003 topics; FR-MESS-005 switching between them;
# FR-MESS-006 author and timestamp.
@bp.route("/activity/<activity_id>")
@bp.route("/activity/<activity_id>/forum/<forum_topic>")
@flask_login.login_required
def activity(activity_id, forum_topic=None):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user
    role = _role_in(activity, user)
    already_joined = role is not None

    join_requests = []
    if RolePolicy.can(role, "review_requests"):
        join_requests = db.session.execute(
            db.select(model.JoinRequest)
            .where(model.JoinRequest.activity_id == activity.id,
                   model.JoinRequest.status == model.REQUEST_PENDING)
            .order_by(model.JoinRequest.created_at.asc())
        ).scalars().all()

    # FR-MESS-004: the board is for participants only.
    forum_topics = []
    active_topic = None
    active_forum_messages = []

    if RolePolicy.can(role, "view"):
        forum_topics = list(activity.topics)
        wanted = forum_topic or DEFAULT_FORUM_TOPIC
        active_topic = next((t for t in forum_topics if t.name == wanted), None)
        if active_topic is None and forum_topics:
            active_topic = forum_topics[0]
        if active_topic is not None:
            active_forum_messages = db.session.execute(
                db.select(model.Message)
                .where(model.Message.topic_id == active_topic.id)
                .order_by(model.Message.id.asc())
            ).scalars().all()

    return render_template(
        "main/activity.html",
        activity=activity,
        statuses=model.STATUSES,
        # FR-ACE-007: the template needs the role to decide which controls to
        # show. The server-side checks above remain the access control
        # (NFR-SECU-004); this only drives the UI.
        role=role,
        finalized=_finalized(activity),
        members=activity.memberships,
        join_requests=join_requests,
        already_joined=already_joined,
        forum_topics=forum_topics,
        active_topic=active_topic,
        active_forum_messages=active_forum_messages,
    )


# --- FR-PE-001 to 009 joining ------------------------------------------------

def _eligibility_errors(activity, user):
    """FR-PE-003 to 009 and FR-PROF-009, in one place for join and approval."""
    return EligibilityChecker.check(
        status=activity.status,
        already_member=_role_in(activity, user) is not None,
        attendee_count=activity.attendee_count,
        max_members=activity.max_attendees,
        viewer_age=user.age,
        min_age=activity.min_age,
        max_age=activity.max_age,
        viewer_school_year=user.school_year,
        allowed_years=None if not activity.min_year else [activity.min_year, activity.max_year],
        viewer_gender=user.gender,
        allowed_genders=([g.strip() for g in activity.allowed_genders.split(",")]
                         if activity.allowed_genders else None),
        viewer_university=user.university_id,
        activity_university=activity.university_id,
    )


# FR-PE-001 join an OPEN activity. FR-PE-003, 004, 005, 008, 009 and
# FR-PROF-009 are all enforced by EligibilityChecker, which reports every
# failed rule rather than the first.
# GAP (FR-PE-010): a full activity should offer the waitlist. model.WaitlistEntry
#   exists but nothing writes to it, so this reports an error instead.
# GAP (NFR-REL-005): the capacity check and the attendee_count increment are
#   not under a row lock, so two users can still race for the last seat. The
#   schema's attendee_count <= max_attendees CHECK makes the loser's commit
#   fail rather than letting the count go wrong, which is a backstop, not the
#   fix. The design locks the activity row first.
# GAP (FR-PE-005): allowed_years is approximated from min_year/max_year as a
#   two-element list, which only works when they name adjacent years. A
#   year ordering is needed to compare ranges properly.
@bp.route("/activity/<activity_id>/join", methods=["POST"])
@flask_login.login_required
def join_activity(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    # FR-PE-001 vs FR-PE-002: only OPEN admits a member without approval.
    if not ActivityStatusMachine.accepts_direct_join(activity.status):
        flash("This activity is not open for joining.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    errors = _eligibility_errors(activity, user)
    if errors:
        _flash_all(errors)
        return redirect(url_for("main.activity", activity_id=activity_id))

    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=user.id, role=model.ROLE_ATTENDEE))
    activity.attendee_count += 1
    db.session.commit()

    flash("Successfully joined the activity!", "success")
    return redirect(url_for("main.activity", activity_id=activity_id))


# FR-PE-002 request to join an APPROVAL_REQUIRED activity, with an optional
# message to the current attendees.
# GAP (FR-PE-002): no notification is published. The design emits
#   JoinRequestCreated to the Admin and Editors. model.Notification and
#   model.OutboxEvent exist; nothing writes to them yet.
@bp.route("/activity/<activity_id>/request_join", methods=["POST"])
@flask_login.login_required
def request_join_activity(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    errors = _eligibility_errors(activity, user)
    if errors:
        _flash_all(errors)
        return redirect(url_for("main.activity", activity_id=activity_id))

    # FR-PE-002: one outstanding request per user, which the partial unique
    # index also enforces.
    existing = db.session.execute(
        db.select(model.JoinRequest).where(
            model.JoinRequest.activity_id == activity.id,
            model.JoinRequest.user_id == user.id,
            model.JoinRequest.status == model.REQUEST_PENDING)
    ).scalar_one_or_none()
    if existing:
        flash("You already have a pending request for this activity.", "info")
        return redirect(url_for("main.activity", activity_id=activity_id))

    message = (request.form.get("request_message") or "").strip() or None
    if message and len(message) > model.MAX_DESCRIPTION_LENGTH:
        flash(f"Your message must be {model.MAX_DESCRIPTION_LENGTH} characters or fewer.",
              "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    db.session.add(model.JoinRequest(
        activity_id=activity.id, user_id=user.id, message=message))
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))


# FR-PE-002 approve or deny, Admin and Editor only (FR-ACE-007);
# FR-PE-004 eligibility is re-checked at approval time.
# GAP (FR-PE-002): the decision is recorded but not delivered. The design
#   emits JoinRequestDecided to the requester, in-app and by email.
@bp.route("/activity/<activity_id>/update_join_request/<requesting_user_id>", methods=["POST"])
@flask_login.login_required
def update_join_request(activity_id, requesting_user_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    if not RolePolicy.can(_role_in(activity, user), "review_requests"):
        flash("You do not have permission to manage join requests for this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    join_request = db.session.execute(
        db.select(model.JoinRequest).where(
            model.JoinRequest.activity_id == activity.id,
            model.JoinRequest.user_id == requesting_user_id,
            model.JoinRequest.status == model.REQUEST_PENDING)
    ).scalar_one_or_none()
    if not join_request:
        return redirect(url_for("main.activity", activity_id=activity_id))

    action = request.form.get("approval_status")

    if action == "approve":
        # FR-PE-004: re-check now, not when the request was made. Seats may
        # have gone, or the requester's eligibility may have changed, while it
        # waited.
        requesting_user = db.get_or_404(model.User, requesting_user_id)
        errors = _eligibility_errors(activity, requesting_user)
        if errors:
            _flash_all(errors)
            return redirect(url_for("main.activity", activity_id=activity_id))

        db.session.add(model.ActivityMembership(
            activity_id=activity.id, user_id=requesting_user_id, role=model.ROLE_ATTENDEE))
        activity.attendee_count += 1
        join_request.status = model.REQUEST_APPROVED
    else:
        join_request.status = model.REQUEST_DENIED

    # FR-PE-002: the row is kept with its decision rather than deleted, so the
    # requester can be told and the decision is auditable.
    join_request.reviewed_by = user.id
    join_request.decided_at = model.utcnow()
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))


# FR-PE-002 supporting route: withdraw one's own pending request.
@bp.route("/activity/<activity_id>/cancel_join", methods=["POST"])
@flask_login.login_required
def cancel_join_request(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    existing = db.session.execute(
        db.select(model.JoinRequest).where(
            model.JoinRequest.activity_id == activity.id,
            model.JoinRequest.user_id == user.id,
            model.JoinRequest.status == model.REQUEST_PENDING)
    ).scalar_one_or_none()
    if not existing:
        return redirect(url_for("main.activity", activity_id=activity_id))

    db.session.delete(existing)
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))


# FR-PE-006 leave an activity, whatever the member's role.
# GAP (FR-ACE-007): an Admin leaving must name a successor from the roster,
#   and the earliest-joined member is promoted when none is chosen. Neither
#   happens here, so an Admin is refused instead -- which FR-PE-006 does not
#   allow. This needs SuccessionService; the schema is ready for it
#   (ActivityMembership.joined_at is indexed for exactly this).
# GAP (FR-PE-006): leaving as the only member must cancel the activity.
# GAP (FR-PE-010): a freed seat must draw from the waitlist, first-come
#   first-served.
# GAP (NFR-REL-002): a distinct confirmation step is required before an action
#   that removes access.
@bp.route("/activity/<activity_id>/leave", methods=["POST"])
@flask_login.login_required
def leave_activity(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    membership = db.session.get(model.ActivityMembership, (activity.id, user.id))
    if membership is None:
        return redirect(url_for("main.activity", activity_id=activity_id))

    if membership.role == model.ROLE_ADMIN and activity.attendee_count > 1:
        flash("Choose a successor before leaving this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    db.session.delete(membership)
    activity.attendee_count -= 1
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id))


# --- FR-MESS messaging -------------------------------------------------------

# FR-MESS-002 non-empty and 500 characters or fewer; FR-MESS-004 participants
# only; FR-MESS-003 and FR-MESS-005 topics.
# GAP (FR-MESS-009): delivery is a redirect, so other participants only see
#   the message after reloading. The design uses WebSocket plus Redis pub/sub.
# GAP (FR-MESS-007, FR-MESS-008): no edit or delete with the "message deleted
#   by author" placeholder, and no @mention parsing. Message.deleted_at,
#   Message.edited_at and model.MessageMention exist for both.
@bp.route("/activity/<activity_id>/message", methods=["POST"])
@flask_login.login_required
def new_message(activity_id):
    activity = _visible_activity_or_404(activity_id)
    user = flask_login.current_user

    # FR-MESS-004: only participants may post.
    if not RolePolicy.can(_role_in(activity, user), "post_message"):
        flash("Only participants of this activity can post messages.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))

    content = request.form.get("message-content", "").strip()
    topic_name = request.form.get("forum_topic", DEFAULT_FORUM_TOPIC).strip()
    new_topic_name = request.form.get("new_forum_topic", "").strip()
    if new_topic_name:
        topic_name = new_topic_name

    # FR-MESS-002: non-empty and within the cap.
    if not content:
        flash("Message cannot be empty", "error")
        return redirect(url_for("main.activity", activity_id=activity_id,
                                forum_topic=topic_name))
    if len(content) > model.MAX_MESSAGE_LENGTH:
        flash(f"Messages are limited to {model.MAX_MESSAGE_LENGTH} characters.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id,
                                forum_topic=topic_name))

    # FR-MESS-005: a new topic is a row, unique per activity, so an empty
    # topic survives and messages cannot leak between topics.
    topic = db.session.execute(
        db.select(model.ForumTopic).where(
            model.ForumTopic.activity_id == activity.id,
            model.ForumTopic.name == topic_name)
    ).scalar_one_or_none()
    if topic is None:
        if not RolePolicy.can(_role_in(activity, user), "create_topic"):
            flash("You cannot create a topic in this activity.", "error")
            return redirect(url_for("main.activity", activity_id=activity_id))
        topic = model.ForumTopic(activity_id=activity.id, name=topic_name,
                                 created_by=user.id)
        db.session.add(topic)
        db.session.flush()

    db.session.add(model.Message(topic_id=topic.id, author_id=user.id, body=content))
    db.session.commit()

    return redirect(url_for("main.activity", activity_id=activity_id,
                            forum_topic=topic.name))


# FR-ACE-001 supporting route: renders the activity creation form.
@bp.route("/form")
@flask_login.login_required
def form():
    from . import universities
    return render_template(
        "main/activity_form.html",
        activity_types=db.session.execute(
            db.select(model.ActivityType).order_by(model.ActivityType.name)).scalars().all(),
        universities=universities.search(),
    )


# --- FR-PROF-005 follows -----------------------------------------------------

# FR-PROF-005 follow a user. A follow on a private account is created as
# PENDING until the owner accepts.
# GAP (FR-PROF-005): nothing accepts a PENDING follow yet, and the follower
#   and following lists are not exposed as their own views.
@bp.route("/follow/<user_id>", methods=["POST"])
@flask_login.login_required
def follow(user_id):
    target = db.get_or_404(model.User, user_id)
    viewer = flask_login.current_user

    # FR-PROF-009: follows do not cross universities.
    if target.university_id != viewer.university_id or target.id == viewer.id:
        from flask import abort
        abort(404)

    if db.session.get(model.Follow, (viewer.id, target.id)) is None:
        db.session.add(model.Follow(
            follower_id=viewer.id, followee_id=target.id,
            status=model.FOLLOW_ACCEPTED if target.account_public else model.FOLLOW_PENDING,
        ))
        db.session.commit()

    return redirect(url_for("main.profile", user_id=user_id))


# FR-PROF-005 unfollow a user.
@bp.route("/unfollow/<user_id>", methods=["POST"])
@flask_login.login_required
def unfollow(user_id):
    viewer = flask_login.current_user
    follow_row = db.session.get(model.Follow, (viewer.id, user_id))
    if follow_row is not None:
        db.session.delete(follow_row)
        db.session.commit()
    return redirect(url_for("main.profile", user_id=user_id))


# --- Meetups: NOT IN THE SRS OR THE DESIGN DOC -------------------------------
# Meetup is not one of the design's 21 tables and no requirement covers it, so
# there is no model.Meetup and these three routes raise at request time. They
# are left in place rather than deleted because that is a product decision:
# either add a requirement and a table, or remove the routes and the calendar
# UI that calls them. Until then they are dead.
# GAP: no participation or role check on any of the three (NFR-SECU-004), the
#   meetup is not checked against the activity in the URL on delete, deletion
#   has no confirmation step (NFR-REL-002), and two of them return str(e) to
#   the client (NFR-SOFT-006).
# NFR-REL-001: if they are kept, location must stay user-entered text.

@bp.route('/activity/<activity_id>/meetups')
@flask_login.login_required
def activity_meetup_retrieval(activity_id):
    meetups = model.Meetup.query.filter_by(activity_id=activity_id).all()

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


@bp.route('/activity/<activity_id>/meetups', methods=['POST'])
@flask_login.login_required
def new_activity_meetup(activity_id):
    current_user = flask_login.current_user
    json = request.get_json()

    try:
        date = json['date']
        time = json['time']
        datetime_string = f"{date} {time}"
        date_time = datetime.strptime(datetime_string, '%Y-%m-%d %H:%M')

        meetup = model.Meetup(
            activity_id=activity_id,
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
        return jsonify({'success': False, 'error': str(e)}), 400


@bp.route('/activity/<activity_id>/meetups/<int:meetup_id>', methods=['DELETE'])
@flask_login.login_required
def delete_meetup(activity_id, meetup_id):
    meetup = model.Meetup.query.get_or_404(meetup_id)

    try:
        db.session.delete(meetup)
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500


# --- FR-AD-002, FR-AD-003 browse ---------------------------------------------

# FR-AD-002 browse with filters; FR-AD-003 age eligibility computed from the
# viewer's birthday.
# GAP (FR-AD-002): the activity type filter and the school-year range filter
#   the requirement lists are not implemented, though the schema has columns
#   for both. Filters on origin and destination are not in the requirement.
# GAP (FR-AD-006): no cursor, so this is one page rather than infinite scroll.
#   The design uses keyset pagination on (created_at, id).
@bp.route("/browse_activities")
@flask_login.login_required
def browse_activities():
    user = flask_login.current_user

    # FR-PROF-009 / NFR-SECU-010: applied first and unconditionally.
    activities_query = _university_scoped(db.select(model.Activity))

    # FR-AD-002: "Only joinable activities should be shown" -- not full, not
    # closed, and age-eligible for this viewer.
    activities_query = activities_query.where(
        model.Activity.status.in_([model.STATUS_OPEN, model.STATUS_APPROVAL_REQUIRED]),
        model.Activity.attendee_count < model.Activity.max_attendees,
    )

    max_budget = request.args.get("max_budget", type=float)
    if max_budget:
        activities_query = activities_query.where(model.Activity.budget <= max_budget)

    # FR-AD-003: the filter is on the VIEWER's age, derived from their
    # birthday. An activity qualifies when the viewer falls inside its own
    # min_age..max_age bounds.
    if user.age is not None:
        activities_query = activities_query.where(
            or_(model.Activity.min_age.is_(None), model.Activity.min_age <= user.age),
            or_(model.Activity.max_age.is_(None), model.Activity.max_age >= user.age),
        )

    start_date = request.args.get("start_date")
    if start_date:
        activities_query = activities_query.where(model.Activity.start_at >= start_date)

    end_date = request.args.get("end_date")
    if end_date:
        activities_query = activities_query.where(model.Activity.end_at <= end_date)

    origin = request.args.get("origin")
    if origin:
        activities_query = activities_query.where(
            model.Activity.meeting_point.ilike(f"%{origin}%"))
        #referenced ChatGPT for 'ilike', which compares with case insensitive values

    destination = request.args.get("destination")
    if destination:
        activities_query = activities_query.where(
            model.Activity.destinations.any(
                model.ActivityDestination.text.ilike(f"%{destination}%")))

    activities = db.session.execute(
        activities_query.order_by(model.Activity.created_at.desc())
    ).scalars().all()

    return render_template("main/browse_activities.html", activities=activities)
