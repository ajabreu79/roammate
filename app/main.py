"""Activity, participation, messaging and discovery controllers.

REQUIREMENTS TRACEABILITY
Every route is tagged with the SRS requirements it implements. Where one is
only partly met the gap is recorded as a GAP comment beside the code
responsible rather than left to be rediscovered. Grep "GAP" to list them.

The activity and participation routes are thin controllers: they read the
form, call one method of ActivityService (app/activities.py) or
ParticipationService (app/participation.py), and turn its (errors, result)
into a flash and a redirect. Authorization, validation, row locking and the
transaction live in those services (NFR-SECU-004, NFR-REL-005).

DESIGN DEVIATIONS that apply to this whole file:

  - Layering. Messaging, discovery and profile routes still hold their
    queries and rules inline; only activity and participation have moved to
    the Controller -> Service layering the design specifies.

  - Module boundaries. The design splits these routes across five modules
    (Activity, Participation, Messaging, Discover, Profile), each owning its
    tables and route prefix. They are all in the `main` blueprint here.

  - Route paths. The design uses /activities/{id}/join, /leave, /requests,
    /users/{id}/follow, /feed and /browse. This file uses /activity/{id}/join,
    /follow/{id}, /browse_activities, /make_editor/... and /edit_activity/...

  - University scoping is applied by ActivityService.get_visible and, for the
    feed and browse queries, by calling _university_scoped explicitly. The
    design enforces it with a UniversityScope base repository plus PostgreSQL
    row-level security, so a query cannot opt out.
"""

import os
import uuid
from datetime import datetime

from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request,
    url_for,
)
import flask_login

from sqlalchemy import or_
from werkzeug.utils import secure_filename

from . import activity_types
from . import db
from . import model
from .activities import (
    DEFAULT_FORUM_TOPIC, FIELD_GROUPS, NOT_FOUND, ActivityService, MembershipManager,
)
from .activity_rules import SCHOOL_YEARS, ActivityStatusMachine, RolePolicy
from .participation import JOINED, ParticipationService

bp = Blueprint("main", __name__)

ALLOWED_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

# FR-PE-009: the options the activity form offers, the same ones the profile
# form offers, so an activity's allowed genders can match a profile.
GENDER_OPTIONS = ("Woman", "Man", "Non-binary", "Prefer not to say")

# The form posts model column names, so a request maps onto ActivityService
# without renaming. Destinations and allowed genders are lists.
_FORM_TEXT_FIELDS = (
    "title", "description", "meeting_point", "start_at", "end_at", "budget",
    "max_attendees", "min_age", "max_age", "min_year", "max_year", "type_id",
)

_FORM_DATETIME = "%Y-%m-%dT%H:%M"

# FR-ACE-008: how each finalizable field is named on the page.
FIELD_LABELS = {
    "meeting_point": "Meeting point",
    "destinations": "Destinations",
    "dates": "Dates",
    "budget": "Budget",
    "max_attendees": "Max attendees",
    "allowed_genders": "Allowed genders",
    "activity_type": "Activity type",
    "age_range": "Age range",
    "year_range": "School-year range",
    "status": "Status",
}

_ROLE_ORDER = {model.ROLE_ADMIN: 0, model.ROLE_EDITOR: 1, model.ROLE_ATTENDEE: 2}


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
    activity = ActivityService.get_visible(activity_id, flask_login.current_user)
    if activity is None:
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


def _back(activity_id, errors, success=None):
    """Turn a service's errors into a response on the activity page.

    [NOT_FOUND] is a 404, never a flash, so another university's activity is
    indistinguishable from a missing one (NFR-SECU-010).
    """
    if errors and NOT_FOUND in errors:
        abort(404)
    if errors:
        _flash_all(errors)
    elif success:
        flash(success, "success")
    return redirect(url_for("main.activity", activity_id=activity_id))


def _activity_fields(form, files):
    """The fields a create or edit form posted, as model column names.

    Returns (errors, fields). Only what the form sent is included, so a
    disabled (finalized) input is left out rather than sent as blank. The two
    list fields carry a hidden *_sent marker, because a form with every
    checkbox cleared posts nothing at all for them.
    """
    fields = {name: form.get(name) for name in _FORM_TEXT_FIELDS if name in form}
    if "destinations_sent" in form:
        fields["destinations"] = form.getlist("destination")
    if "allowed_genders_sent" in form:
        fields["allowed_genders"] = ",".join(form.getlist("allowed_genders"))

    upload = files.get("activity_image")
    if upload and upload.filename:
        key, errors = _save_activity_image(upload)
        if errors:
            return errors, None
        fields["image_key"] = key
    return [], fields


def _values_from_activity(activity):
    """An activity's fields in the shape the form template reads."""
    def text(value):
        return "" if value is None else str(value)

    return {
        "title": activity.title,
        "description": activity.description,
        "meeting_point": activity.meeting_point,
        "start_at": activity.start_at.strftime(_FORM_DATETIME),
        "end_at": activity.end_at.strftime(_FORM_DATETIME),
        "budget": "" if activity.budget is None else f"{activity.budget:.2f}",
        "max_attendees": text(activity.max_attendees),
        "min_age": text(activity.min_age),
        "max_age": text(activity.max_age),
        "min_year": text(activity.min_year),
        "max_year": text(activity.max_year),
        "type_id": text(activity.type_id),
        "destinations": [d.text for d in activity.destinations],
        "allowed_genders": [g.strip() for g in (activity.allowed_genders or "").split(",")
                            if g.strip()],
    }


def _values_from_form(form, base=None):
    """What the user typed, over `base`, so a refused form keeps its input."""
    values = dict(base or {name: "" for name in _FORM_TEXT_FIELDS})
    values.setdefault("destinations", [])
    values.setdefault("allowed_genders", [])
    values.update({name: form.get(name) for name in _FORM_TEXT_FIELDS if name in form})
    if "destinations_sent" in form:
        values["destinations"] = form.getlist("destination")
    if "allowed_genders_sent" in form:
        values["allowed_genders"] = form.getlist("allowed_genders")
    return values


def _render_form(activity, values, status=200):
    """The create form (activity=None) or the edit form for `activity`."""
    finalized = _finalized(activity) if activity else set()
    locked = {column for column, group in FIELD_GROUPS.items() if group in finalized}
    return render_template(
        "main/activity_form.html",
        activity=activity,
        values=values,
        locked=locked,
        activity_types=activity_types.all_types(),
        school_years=SCHOOL_YEARS,
        genders=GENDER_OPTIONS,
    ), status


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
    user = flask_login.current_user
    query = _university_scoped(
        db.select(model.Activity).where(model.Activity.status == model.STATUS_OPEN)
    ).order_by(model.Activity.created_at.desc()).limit(4)
    latest_activities = db.session.execute(query).scalars().all()

    # The viewer's own activities that are still going ahead, soonest first.
    my_activities = db.session.execute(
        _university_scoped(db.select(model.Activity))
        .join(model.ActivityMembership)
        .where(model.ActivityMembership.user_id == user.id,
               model.Activity.status.not_in(ActivityStatusMachine.TERMINAL))
        .order_by(model.Activity.start_at)
    ).scalars().all()

    return render_template("main/index.html", latest_activities=latest_activities,
                           my_activities=my_activities)


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

    # Activity times are the wall-clock times the creator typed, stored without
    # a zone (SQLite drops it; see ActivityService._same), so compare them with
    # local naive time. model.utcnow() is aware and cannot be compared at all.
    now = datetime.now()
    past_activities = [m.activity for m in memberships
                       if m.activity.end_at.replace(tzinfo=None) < now]
    upcoming_activities = [m.activity for m in memberships
                           if m.activity.end_at.replace(tzinfo=None) >= now]

    if viewer.id == user.id:
        follow_button = None  # viewing their own profile, no follow button
    else:
        follow = db.session.get(model.Follow, (viewer.id, user.id))
        follow_button = "unfollow" if follow else "follow"

    # FR-PROF-005: counted here rather than with len(user.followers) so that a
    # PENDING follow on a private account is not counted as a follower, and so
    # the page does not load every Follow row to count them.
    def accepted(column, value):
        return db.session.execute(
            db.select(db.func.count())
            .select_from(model.Follow)
            .where(column == value, model.Follow.status == model.FOLLOW_ACCEPTED)
        ).scalar()

    follower_count = accepted(model.Follow.followee_id, user.id)
    following_count = accepted(model.Follow.follower_id, user.id)

    return render_template(
        "main/profile.html",
        user=user,
        past_activities=past_activities,
        upcoming_activities=upcoming_activities,
        follow_button=follow_button,
        follower_count=follower_count,
        following_count=following_count,
    )


# --- FR-ACE-001 to 004 activity creation -------------------------------------

# FR-ACE-001 supporting route: renders the activity creation form.
@bp.route("/form")
@flask_login.login_required
def form():
    return _render_form(None, _values_from_form({}))


# FR-ACE-001 create an activity; FR-ACE-002 defaults to OPEN;
# FR-ACE-003 creator becomes the Admin; FR-ACE-004 field validation. All four
# are ActivityService.create's; a refused form is shown again with its input.
# GAP (NFR-SECU-008): the image is saved before the activity is validated, so
#   a refused form can leave an unused image behind.
@bp.route("/activity", methods=["POST"])
@flask_login.login_required
def new_activity():
    errors, fields = _activity_fields(request.form, request.files)
    if not errors:
        errors, activity = ActivityService.create(flask_login.current_user, fields)
    if errors:
        _flash_all(errors)
        return _render_form(None, _values_from_form(request.form), status=400)

    flash("Activity created.", "success")
    return redirect(url_for("main.activity", activity_id=activity.id))


# --- FR-ACE-005, 008, 009 editing --------------------------------------------

# FR-ACE-005 the edit form, for Admin and Editors. Finalized fields render
# disabled, so the browser does not post them (FR-ACE-008).
@bp.route("/activity/<activity_id>/edit")
@flask_login.login_required
def edit_activity_form(activity_id):
    activity = _visible_activity_or_404(activity_id)
    if not RolePolicy.can(_role_in(activity, flask_login.current_user), "edit_fields"):
        flash("You do not have permission to edit this activity.", "error")
        return redirect(url_for("main.activity", activity_id=activity_id))
    return _render_form(activity, _values_from_activity(activity))


# FR-ACE-005 update non-finalized fields; FR-ACE-004 the merged result is
# validated as a whole; FR-PE-010 seats added are filled from the waitlist.
@bp.route("/edit_activity/<activity_id>", methods=["POST"])
@flask_login.login_required
def edit_activity(activity_id):
    user = flask_login.current_user
    errors, changes = _activity_fields(request.form, request.files)
    if not errors:
        errors, result = ActivityService.update(activity_id, user, changes)
    if errors:
        if NOT_FOUND in errors:
            abort(404)
        _flash_all(errors)
        activity = _visible_activity_or_404(activity_id)
        values = _values_from_form(request.form, base=_values_from_activity(activity))
        return _render_form(activity, values, status=400)

    if result.seats_added:
        _, promoted = ParticipationService.fill_seats(activity_id, user)
        if promoted:
            flash(f"{len(promoted)} from the waitlist joined the new seats.", "success")

    flash("Activity updated.", "success")
    return redirect(url_for("main.activity", activity_id=activity_id))


# FR-ACE-009 status changes through the transition table, with its side
# effects (pending requests denied, waitlist cleared, every field finalized).
@bp.route("/activity/<activity_id>/status", methods=["POST"])
@flask_login.login_required
def change_status(activity_id):
    errors, _ = ActivityService.change_status(
        activity_id, flask_login.current_user, request.form.get("status"))
    return _back(activity_id, errors, "Status updated.")


# FR-ACE-008 finalize fields, one-way.
@bp.route("/activity/<activity_id>/finalize", methods=["POST"])
@flask_login.login_required
def finalize_fields(activity_id):
    names = request.form.getlist("field")
    if not names:
        return _back(activity_id, ["Choose at least one field to finalize."])

    errors, added = ActivityService.finalize(activity_id, flask_login.current_user, names)
    finalized = ", ".join(FIELD_LABELS.get(name, name) for name in added or [])
    return _back(activity_id, errors, f"Finalized: {finalized}." if finalized else None)


# --- FR-ACE-006, 007 roles ---------------------------------------------------

# FR-ACE-006 grant or revoke Editor, Admin only. Audit-logged (NFR-SECU-012).
@bp.route("/make_editor/<activity_id>/<participant_id>", methods=["POST"])
@flask_login.login_required
def make_editor(activity_id, participant_id):
    errors, _ = ActivityService.set_editor(
        activity_id, flask_login.current_user, participant_id, grant=True)
    return _back(activity_id, errors, "Editor access granted.")


@bp.route("/revoke_editor/<activity_id>/<participant_id>", methods=["POST"])
@flask_login.login_required
def revoke_editor(activity_id, participant_id):
    errors, _ = ActivityService.set_editor(
        activity_id, flask_login.current_user, participant_id, grant=False)
    return _back(activity_id, errors, "Editor access removed.")


# FR-ACE-007 hand the Admin role to another member; the old Admin becomes an
# Editor.
@bp.route("/activity/<activity_id>/transfer_admin/<participant_id>", methods=["POST"])
@flask_login.login_required
def transfer_admin(activity_id, participant_id):
    errors, _ = ActivityService.transfer_admin(
        activity_id, flask_login.current_user, participant_id)
    return _back(activity_id, errors, "You handed over the Admin role.")


# FR-ACE-007 remove a member, Admin only; the freed seat is filled from the
# waitlist (FR-PE-010).
@bp.route("/activity/<activity_id>/remove/<participant_id>", methods=["POST"])
@flask_login.login_required
def remove_member(activity_id, participant_id):
    errors, _ = ParticipationService.remove_member(
        activity_id, flask_login.current_user, participant_id)
    return _back(activity_id, errors, "Member removed.")


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

    join_requests = []
    if RolePolicy.can(role, "review_requests"):
        join_requests = db.session.execute(
            db.select(model.JoinRequest)
            .where(model.JoinRequest.activity_id == activity.id,
                   model.JoinRequest.status == model.REQUEST_PENDING)
            .order_by(model.JoinRequest.created_at.asc())
        ).scalars().all()

    my_request = db.session.execute(
        db.select(model.JoinRequest).where(
            model.JoinRequest.activity_id == activity.id,
            model.JoinRequest.user_id == user.id,
            model.JoinRequest.status == model.REQUEST_PENDING)
    ).scalar_one_or_none()

    # FR-PE-010: position is computed from created_at, never stored.
    waitlist = sorted(activity.waitlist, key=lambda e: (e.created_at, e.user_id))
    waitlist_position = next(
        (i for i, entry in enumerate(waitlist, start=1) if entry.user_id == user.id), None)

    members = sorted(activity.memberships,
                     key=lambda m: (_ROLE_ORDER.get(m.role, 9), m.joined_at))

    # FR-ACE-007: an Admin leaving while others remain names a successor.
    successors = (MembershipManager.others(activity, user.id)
                  if role == model.ROLE_ADMIN else [])

    finalized = _finalized(activity)
    status_targets = [
        target for target in ActivityStatusMachine.targets(activity.status)
        if RolePolicy.can(role, "cancel" if target == model.STATUS_CANCELLED
                          else "change_status")
    ] if "status" not in finalized else []

    # FR-MESS-004: the board is for participants only.
    forum_topics = []
    active_topic = None
    active_forum_messages = []

    if RolePolicy.can(role, "view"):
        forum_topics = sorted(activity.topics, key=lambda t: (t.name != DEFAULT_FORUM_TOPIC,
                                                               t.created_at))
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
        # FR-ACE-007: the template needs the role to decide which controls to
        # show. The services remain the access control (NFR-SECU-004); this
        # only drives the UI.
        role=role,
        can=lambda action: RolePolicy.can(role, action),
        finalized=finalized,
        finalizable=[f for f in model.FINALIZABLE_FIELDS if f not in finalized],
        field_labels=FIELD_LABELS,
        status_targets=status_targets,
        terminal=ActivityStatusMachine.is_terminal(activity.status),
        members=members,
        successors=successors,
        join_requests=join_requests,
        my_request=my_request,
        waitlist=waitlist,
        waitlist_position=waitlist_position,
        forum_topics=forum_topics,
        active_topic=active_topic,
        active_forum_messages=active_forum_messages,
    )


# --- FR-PE-001 to 010 joining, requests and leaving --------------------------

# FR-PE-001 join an OPEN activity, or its waitlist when full (FR-PE-010).
# Eligibility (FR-PE-003 to 009, FR-PROF-009) and the row lock that stops two
# people taking the last seat (NFR-REL-005) are ParticipationService.join's.
@bp.route("/activity/<activity_id>/join", methods=["POST"])
@flask_login.login_required
def join_activity(activity_id):
    errors, outcome = ParticipationService.join(activity_id, flask_login.current_user)
    return _back(activity_id, errors,
                 "You joined the activity!" if outcome == JOINED
                 else "The activity is full, so you are on the waitlist.")


# FR-PE-010 give up a place on the waitlist.
@bp.route("/activity/<activity_id>/leave_waitlist", methods=["POST"])
@flask_login.login_required
def leave_waitlist(activity_id):
    errors, _ = ParticipationService.leave_waitlist(activity_id, flask_login.current_user)
    return _back(activity_id, errors, "You left the waitlist.")


# FR-PE-002 request to join an APPROVAL_REQUIRED activity, with an optional
# message to the Admin and Editors.
@bp.route("/activity/<activity_id>/request_join", methods=["POST"])
@flask_login.login_required
def request_join_activity(activity_id):
    errors, _ = ParticipationService.request_to_join(
        activity_id, flask_login.current_user, request.form.get("request_message"))
    return _back(activity_id, errors, "Your request was sent.")


# FR-PE-002 approve or deny, Admin and Editor only (FR-ACE-007); eligibility
# is re-checked at approval time.
@bp.route("/activity/<activity_id>/update_join_request/<request_id>", methods=["POST"])
@flask_login.login_required
def update_join_request(activity_id, request_id):
    approve = request.form.get("approval_status") == "approve"
    errors, _ = ParticipationService.decide_request(
        activity_id, flask_login.current_user, request_id, approve)
    return _back(activity_id, errors, "Request approved." if approve else "Request denied.")


# FR-PE-002 supporting route: withdraw one's own pending request.
@bp.route("/activity/<activity_id>/cancel_join", methods=["POST"])
@flask_login.login_required
def cancel_join_request(activity_id):
    errors, _ = ParticipationService.withdraw_request(activity_id, flask_login.current_user)
    return _back(activity_id, errors, "Your request was withdrawn.")


# FR-PE-006 leave, whatever the role. An Admin with others remaining names a
# successor (FR-ACE-007); the last member leaving cancels the activity; a
# freed seat is filled from the waitlist (FR-PE-010). The template asks for
# confirmation first (NFR-REL-002).
@bp.route("/activity/<activity_id>/leave", methods=["POST"])
@flask_login.login_required
def leave_activity(activity_id):
    errors, result = ParticipationService.leave(
        activity_id, flask_login.current_user,
        successor_id=request.form.get("successor_id") or None)
    if not errors and result.cancelled:
        return _back(activity_id, errors,
                     "You left the activity. Nobody else was in it, so it was cancelled.")
    return _back(activity_id, errors, "You left the activity.")


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


# --- FR-PROF-005 follows -----------------------------------------------------

# FR-PROF-005 follow a user. A follow on a private account is created as
# PENDING until the owner accepts.
# GAP (FR-PROF-005): nothing accepts a PENDING follow yet, so a follow on a
#   private account stays PENDING for good.
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


# FR-PROF-005: the follower and following lists behind the profile page's two
# counts. They answer JSON, not a page, because the profile opens them in a
# modal fetched when the count is clicked; that also keeps one source of truth
# for the membership rules below.
def _follow_list(user_id, count_on, list_side):
    """Accepted follows of `user_id`, as the users on `list_side`.

    `count_on` is the Follow column that equals the profile's owner and
    `list_side` the column holding the other end: followers are the rows whose
    followee is the owner, so the people listed are their followers.
    """
    target = db.get_or_404(model.User, user_id)
    viewer = flask_login.current_user

    # FR-PROF-009: the same university boundary the profile itself applies.
    if target.university_id != viewer.university_id:
        abort(404)

    # FR-PROF-005: on a private account the lists belong to the owner and to
    # the followers it accepted. Everyone else keeps the counts and no names,
    # which is what makes a PENDING follow worth anything.
    if not target.account_public and viewer.id != target.id:
        follow = db.session.get(model.Follow, (viewer.id, target.id))
        if follow is None or follow.status != model.FOLLOW_ACCEPTED:
            return jsonify(private=True, users=[]), 403

    users = db.session.execute(
        db.select(model.User)
        .join(model.Follow, list_side == model.User.id)
        .where(count_on == target.id,
               model.Follow.status == model.FOLLOW_ACCEPTED)
        .order_by(model.User.first_name, model.User.last_name)
    ).scalars().all()

    # NFR-SECU-009: name, year and photo only. Email and birthday never leave
    # the owner's own pages.
    return jsonify(private=False, users=[
        {
            "id": user.id,
            "name": user.name,
            "school_year": user.school_year,
            "photo_url": url_for("static", filename="resources/" + user.profile_photo),
            "profile_url": url_for("main.profile", user_id=user.id),
        }
        for user in users
    ])


@bp.route("/profile/<user_id>/followers")
@flask_login.login_required
def user_followers(user_id):
    return _follow_list(user_id, model.Follow.followee_id, model.Follow.follower_id)


@bp.route("/profile/<user_id>/following")
@flask_login.login_required
def user_following(user_id):
    return _follow_list(user_id, model.Follow.follower_id, model.Follow.followee_id)


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



# --- FR-AD-002, FR-AD-003 browse ---------------------------------------------

# FR-AD-002 browse with filters; FR-AD-003 age eligibility computed from the
# viewer's birthday.
# GAP (FR-AD-002): the school-year range filter the requirement lists is not
#   implemented, though the schema has the columns. Filters on origin and
#   destination are not in the requirement.
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

    type_id = request.args.get("type_id", type=int)
    if type_id:
        activities_query = activities_query.where(model.Activity.type_id == type_id)

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

    return render_template("main/browse_activities.html", activities=activities,
                           activity_types=activity_types.all_types(), filters=request.args)
