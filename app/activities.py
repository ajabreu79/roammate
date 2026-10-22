"""Activity module: ActivityService and MembershipManager.

REQUIREMENTS TRACEABILITY
FR-ACE-001 to 009 (creation, editing, roles, finalization, status), plus the
Activity half of NFR-REL-005 (multi-record changes commit together) and
NFR-SECU-012 (privileged actions are audit-logged).

The rules themselves live in app/activity_rules.py. This module applies them
to the database: it fetches the activity scoped to the viewer's university,
checks the rule, writes, and commits once.

CONVENTIONS, matching app/services.py:
  - Methods are static and return (errors, result). Errors are worked out
    before anything is written; on any error the session is rolled back,
    which also releases the row lock.
  - An activity outside the viewer's university, or one that does not exist,
    is reported as [NOT_FOUND] so the controller can answer 404 for both
    (NFR-SECU-010).
  - ActivityService owns the transaction and commits once per public method.
    MembershipManager and _audit never commit: they run inside whichever
    transaction called them. MembershipManager raises ValueError for calls
    that would break an invariant, because its callers check first and a
    failure there is a bug, not a user error.
  - This module must never import app.participation, which imports it.

DEVIATIONS from the design doc:
  - The Main forum topic is created here, in create(). The design has
    Messaging create it on ActivityCreated, which create() now publishes;
    move the topic there once the Messaging module subscribes.
  - change_status() writes JoinRequest and WaitlistEntry rows directly for the
    CLOSED and CANCELLED side effects, although those tables belong to the
    Participation module. Becomes an ActivityStatusChanged event later.
"""

from collections import namedtuple
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.exc import IntegrityError

from . import db
from . import events
from . import model
from .events import EventBus
from .activity_rules import (
    ADMIN, ATTENDEE, CANCELLED, CLOSED, EDITOR, FINALIZED, OPEN, SCHOOL_YEARS,
    ActivityStatusMachine, ActivityValidator, RolePolicy,
)

NOT_FOUND = "This activity could not be found."
CONFLICT = "Someone else changed this activity at the same time. Please try again."

# FR-MESS-003: the topic every activity starts with. main.DEFAULT_FORUM_TOPIC
# should import this rather than define its own once the routes move over.
DEFAULT_FORUM_TOPIC = "Main"

# FR-ACE-008: which finalizable group each editable column belongs to. A
# change to a column is refused when its group is finalized.
FIELD_GROUPS = {
    "meeting_point": "meeting_point",
    "destinations": "destinations",
    "start_at": "dates",
    "end_at": "dates",
    "budget": "budget",
    "max_attendees": "max_attendees",
    "allowed_genders": "allowed_genders",
    "type_id": "activity_type",
    "min_age": "age_range",
    "max_age": "age_range",
    "min_year": "year_range",
    "max_year": "year_range",
}

# Columns that are always editable while the activity is not terminal.
UNFINALIZABLE = ("title", "description", "image_key")

EDITABLE = UNFINALIZABLE + tuple(FIELD_GROUPS)

# Optional free-text columns, where an empty string means "no value".
_OPTIONAL_TEXT = ("min_year", "max_year", "allowed_genders", "image_key")

_FORM_DATETIME = "%Y-%m-%dT%H:%M"

# update()'s result: seats_added tells the controller to run waitlist
# promotion (FR-PE-010), which this module cannot call.
UpdateResult = namedtuple("UpdateResult", "activity seats_added")


class MembershipManager:
    """The only code that writes ActivityMembership or attendee_count.

    Never commits. Called by ActivityService and ParticipationService inside
    their own transaction, so a membership change and everything that goes
    with it (a waitlist promotion, an admin hand-off) commit together.
    """

    @staticmethod
    def _get(activity, user_id):
        return next((m for m in activity.memberships if m.user_id == user_id), None)

    @staticmethod
    def add(activity, user_id, role=ATTENDEE):
        """Insert a membership and increment attendee_count.

        Does not check eligibility or capacity; the caller already has. The
        attendee_count <= max_attendees CHECK is the backstop.
        Returns the new ActivityMembership.
        """
        if MembershipManager._get(activity, user_id) is not None:
            raise ValueError(f"User {user_id} is already a member of {activity.id}.")

        membership = model.ActivityMembership(user_id=user_id, role=role)
        activity.memberships.append(membership)
        activity.attendee_count = (activity.attendee_count or 0) + 1
        return membership

    @staticmethod
    def remove(activity, user_id):
        """Delete a membership and decrement attendee_count.

        Refuses to remove the Admin: move the role with transfer_admin first,
        so the activity is never without one (FR-ACE-007).
        """
        membership = MembershipManager._get(activity, user_id)
        if membership is None:
            raise ValueError(f"User {user_id} is not a member of {activity.id}.")
        if membership.role == ADMIN:
            raise ValueError("Transfer the Admin role before removing the Admin.")

        # Removing from the collection lets delete-orphan issue the DELETE,
        # and keeps activity.memberships accurate for the rest of the
        # transaction.
        activity.memberships.remove(membership)
        activity.attendee_count -= 1

    @staticmethod
    def remove_last_admin(activity, user_id):
        """Remove the Admin when they are the only member left.

        The one case where an activity may end up with no Admin, because it
        ends up with no members at all; the caller cancels it (FR-PE-006).
        """
        membership = MembershipManager._get(activity, user_id)
        if membership is None or membership.role != ADMIN:
            raise ValueError(f"User {user_id} is not the Admin of {activity.id}.")
        if len(activity.memberships) != 1:
            raise ValueError("Other members remain; transfer the Admin role first.")

        activity.memberships.remove(membership)
        activity.attendee_count -= 1

    @staticmethod
    def set_role(activity, user_id, role):
        """Switch a member between ATTENDEE and EDITOR.

        ADMIN is refused here; transfer_admin is the only way to make one.
        """
        if role not in (ATTENDEE, EDITOR):
            raise ValueError(f"set_role only assigns {ATTENDEE} or {EDITOR}, not {role}.")

        membership = MembershipManager._get(activity, user_id)
        if membership is None:
            raise ValueError(f"User {user_id} is not a member of {activity.id}.")
        if membership.role == ADMIN:
            raise ValueError("The Admin's role changes only through transfer_admin.")

        membership.role = role
        return membership

    @staticmethod
    def transfer_admin(activity, from_user_id, to_user_id):
        """Move the Admin role from one member to another (FR-ACE-007).

        Demote the old Admin to EDITOR and flush before promoting the new one:
        uq_activity_single_admin allows at most one Admin, so promoting first
        would briefly have two and fail.
        """
        old = MembershipManager._get(activity, from_user_id)
        new = MembershipManager._get(activity, to_user_id)
        if old is None or old.role != ADMIN:
            raise ValueError(f"User {from_user_id} is not the Admin of {activity.id}.")
        if new is None:
            raise ValueError(f"User {to_user_id} is not a member of {activity.id}.")
        if old is new:
            raise ValueError("The Admin cannot transfer the role to themselves.")

        old.role = EDITOR
        db.session.flush()
        new.role = ADMIN
        return new

    @staticmethod
    def others(activity, user_id):
        """Every member except `user_id`, earliest joined_at first.

        Automatic succession takes the first; the "choose a successor" list
        shows them all.
        """
        return sorted(
            (m for m in activity.memberships if m.user_id != user_id),
            key=lambda m: (m.joined_at or model.utcnow(), m.user_id),
        )


class ActivityService:
    """Creation, editing, status, finalization and role changes.

    Every public method: fetch scoped (locked if it writes), check permission
    and collect every error, roll back and return them if any, otherwise
    write, commit once and return ([], result).
    """

    # --- helpers -------------------------------------------------------------

    @staticmethod
    def get_visible(activity_id, viewer, lock=False):
        """The activity if it is in the viewer's university, else None.

        Replaces main._visible_activity_or_404. Returns None rather than
        aborting, so the controller decides on the 404 (NFR-SECU-010: a
        cross-university id must look like a missing one).

        lock=True adds with_for_update() and populate_existing(), so the row
        is locked on PostgreSQL and reloaded rather than read from the
        identity map. SQLAlchemy omits FOR UPDATE on SQLite.
        """
        if viewer is None or getattr(viewer, "university_id", None) is None:
            return None

        query = db.select(model.Activity).where(
            model.Activity.id == activity_id,
            model.Activity.university_id == viewer.university_id,
        )
        if lock:
            query = query.with_for_update().execution_options(populate_existing=True)
        return db.session.execute(query).scalar_one_or_none()

    @staticmethod
    def _role(activity, user):
        """The user's role in this activity, or None if not a member."""
        if user is None:
            return None
        membership = MembershipManager._get(activity, user.id)
        return membership.role if membership else None

    @staticmethod
    def _require(activity, user, action):
        """[] if RolePolicy allows `action` for the user's role, else one
        permission error (NFR-SECU-004)."""
        if RolePolicy.can(ActivityService._role(activity, user), action):
            return []
        return ["You do not have permission to do that in this activity."]

    @staticmethod
    def _finalized(activity):
        """The set of finalized field names (FR-ACE-008)."""
        return {row.field_name for row in activity.finalized_fields}

    @staticmethod
    def _audit(actor, action, target_type, target_id, metadata=None):
        """Add an AuditLog row to the current transaction (NFR-SECU-012).

        Does not commit, so the audit row rolls back with the change it
        records. The column is AuditLog.metadata_json.
        """
        db.session.add(model.AuditLog(
            actor_id=actor.id if actor else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            metadata_json=metadata,
        ))

    @staticmethod
    def _fail(errors):
        """Roll back, releasing any row lock, and return (errors, None)."""
        db.session.rollback()
        return errors, None

    @staticmethod
    def _commit():
        """Commit; [] on success, else [CONFLICT].

        The schema's CHECKs and partial unique indexes are the backstop
        behind the service's own checks (NFR-REL-005), so a violation here
        means a concurrent change got there first.
        """
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return [CONFLICT]
        return []

    @staticmethod
    def _as_input(value):
        """A stored value in the form ActivityValidator parses."""
        if isinstance(value, datetime):
            return value.strftime(_FORM_DATETIME)
        if isinstance(value, Decimal):
            return str(value)
        return value

    @staticmethod
    def _clean(values, today=None):
        """Validate and type a full set of column values. Returns (errors, cleaned).

        `values` holds every editable column, raw from the form or as stored.
        """
        errors, parsed = ActivityValidator.validate(
            title=values.get("title"),
            description=values.get("description"),
            meeting_point=values.get("meeting_point"),
            start=ActivityService._as_input(values.get("start_at")),
            end=ActivityService._as_input(values.get("end_at")),
            budget=ActivityService._as_input(values.get("budget")),
            max_members=values.get("max_attendees"),
            min_age=values.get("min_age"),
            max_age=values.get("max_age"),
            today=today,
        )

        # The validator leaves a blank max_attendees as None; the column is
        # required (FR-ACE-001).
        if values.get("max_attendees") in (None, ""):
            errors.append("Max attendees is required.")

        cleaned = {
            "title": (values.get("title") or "").strip(),
            "description": (values.get("description") or "").strip(),
            "meeting_point": (values.get("meeting_point") or "").strip(),
            "start_at": parsed["start"],
            "end_at": parsed["end"],
            "budget": parsed["budget"],
            "max_attendees": parsed["max_members"],
            "min_age": parsed["min_age"],
            "max_age": parsed["max_age"],
            "destinations": [text.strip() for text in values.get("destinations") or []
                             if text and text.strip()],
        }

        for column in _OPTIONAL_TEXT:
            text = values.get(column)
            cleaned[column] = text.strip() if isinstance(text, str) and text.strip() else None

        type_id = values.get("type_id")
        if type_id in (None, ""):
            cleaned["type_id"] = None
        else:
            try:
                cleaned["type_id"] = int(type_id)
            except (TypeError, ValueError):
                cleaned["type_id"] = None
            if cleaned["type_id"] is None or db.session.get(
                    model.ActivityType, cleaned["type_id"]) is None:
                errors.append("Choose an activity type from the list.")

        # FR-ACE-004: the school-year range, like the age range, must be in
        # order. Each bound must be one of the years the profile form offers.
        for column, label in (("min_year", "Minimum school year"),
                              ("max_year", "Maximum school year")):
            if cleaned[column] is not None and cleaned[column] not in SCHOOL_YEARS:
                errors.append(f"{label} must be one of: {', '.join(SCHOOL_YEARS)}.")
        if (cleaned["min_year"] in SCHOOL_YEARS and cleaned["max_year"] in SCHOOL_YEARS
                and SCHOOL_YEARS.index(cleaned["min_year"])
                > SCHOOL_YEARS.index(cleaned["max_year"])):
            errors.append("Minimum school year cannot be above maximum school year.")

        return errors, cleaned

    @staticmethod
    def _current(activity):
        """Every editable column as stored, in the shape _clean accepts."""
        values = {column: getattr(activity, column)
                  for column in EDITABLE if column != "destinations"}
        values["destinations"] = [d.text for d in activity.destinations]
        return values

    @staticmethod
    def _same(stored, new):
        """Whether a stored value equals a cleaned one.

        Datetimes compare without their timezone: SQLite returns them naive,
        PostgreSQL aware, and the validator always produces naive values.
        Budget compares as a number, since it is stored as Decimal.
        """
        if isinstance(stored, datetime) and isinstance(new, datetime):
            return stored.replace(tzinfo=None) == new.replace(tzinfo=None)
        if isinstance(stored, Decimal) and new is not None:
            return stored == Decimal(str(new))
        return stored == new

    @staticmethod
    def _replace_destinations(activity, texts):
        """FR-ACE-001: destinations are ordered rows, not a joined string."""
        activity.destinations.clear()
        # Flush the deletes first, so the new rows cannot collide with the
        # old ones on uq_destination_position.
        db.session.flush()
        for position, text in enumerate(texts):
            activity.destinations.append(
                model.ActivityDestination(position=position, text=text))

    # --- FR-ACE-001 to 004 creation ----------------------------------------

    @staticmethod
    def create(creator, fields):
        """Create an activity. Returns (errors, activity).

        `fields` uses model column names; the controller maps form names.
          1. ActivityValidator.validate; return every error.
          2. Build the Activity as OPEN (FR-ACE-002) with attendee_count 0.
          3. Add destinations.
          4. MembershipManager.add(..., ADMIN) for the creator (FR-ACE-003).
          5. Create the Main ForumTopic (FR-MESS-003; see DEVIATIONS).
          6. Publish ActivityCreated.
          7. Commit.
        """
        if creator is None or creator.university_id is None:
            return ["Finish your profile before creating an activity."], None

        errors, cleaned = ActivityService._clean(fields)
        if errors:
            return ActivityService._fail(errors)

        destinations = cleaned.pop("destinations")
        activity = model.Activity(
            university_id=creator.university_id,
            creator_id=creator.id,
            status=OPEN,
            # MembershipManager.add is the only thing that moves the count,
            # so it starts at 0 and the Admin's membership makes it 1.
            attendee_count=0,
            **cleaned,
        )
        db.session.add(activity)

        for position, text in enumerate(destinations):
            activity.destinations.append(
                model.ActivityDestination(position=position, text=text))

        MembershipManager.add(activity, creator.id, ADMIN)

        activity.topics.append(
            model.ForumTopic(name=DEFAULT_FORUM_TOPIC, created_by=creator.id))

        # The id is assigned at insert, and the event needs it.
        db.session.flush()
        EventBus.publish(events.ACTIVITY_CREATED,
                         activity_id=activity.id, creator_id=creator.id)

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], activity

    # --- FR-ACE-005, 008 editing --------------------------------------------

    @staticmethod
    def update(activity_id, actor, changes):
        """Edit fields that are not finalized. Returns (errors, UpdateResult).

          1. Locked fetch; refuse a terminal status or no edit_fields.
          2. Merge the changes over the current values and run
             ActivityValidator on the merged result (FR-ACE-004).
          3. A change to a column whose FIELD_GROUPS group is finalized is an
             error, not silently dropped. A column sent with its current
             value is not a change, so a form may post every field.
          4. max_attendees below attendee_count is a clear error.
          5. Write and commit.
          6. seats_added is True when max_attendees went up, so the
             controller can run waitlist promotion.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return ActivityService._fail([NOT_FOUND])

        if ActivityStatusMachine.is_terminal(activity.status):
            return ActivityService._fail(
                ["This activity has been finalized or cancelled and cannot be edited."])

        errors = ActivityService._require(activity, actor, "edit_fields")
        if errors:
            return ActivityService._fail(errors)

        unknown = sorted(set(changes) - set(EDITABLE))
        if unknown:
            return ActivityService._fail(
                [f"{column} cannot be edited." for column in unknown])

        # Start cannot move into the past, but an activity that has already
        # started stays editable: the earliest allowed start is the earlier
        # of today and the current start.
        today = min(date.today(), activity.start_at.date())

        merged = ActivityService._current(activity)
        merged.update(changes)
        errors, cleaned = ActivityService._clean(merged, today=today)
        if errors:
            return ActivityService._fail(errors)

        current = ActivityService._current(activity)
        changed = [column for column in changes
                   if not ActivityService._same(current[column], cleaned[column])]

        finalized = ActivityService._finalized(activity)
        locked = sorted({FIELD_GROUPS[c] for c in changed
                         if FIELD_GROUPS.get(c) in finalized})
        if locked:
            return ActivityService._fail(
                [f"{group.replace('_', ' ').capitalize()} has been finalized and cannot change."
                 for group in locked])

        if "max_attendees" in changed and cleaned["max_attendees"] < activity.attendee_count:
            return ActivityService._fail([
                f"Max attendees cannot be below the {activity.attendee_count} "
                "people already in this activity."])

        seats_added = ("max_attendees" in changed
                       and cleaned["max_attendees"] > activity.max_attendees)

        for column in changed:
            if column == "destinations":
                ActivityService._replace_destinations(activity, cleaned["destinations"])
            else:
                setattr(activity, column, cleaned[column])

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], UpdateResult(activity, seats_added)

    @staticmethod
    def finalize(activity_id, actor, field_names):
        """Finalize fields, one-way (FR-ACE-008). Returns (errors, finalized).

        Requires finalize_field. Names outside model.FINALIZABLE_FIELDS are
        errors; names already finalized are skipped. `finalized` is the list
        of names this call finalized.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return ActivityService._fail([NOT_FOUND])

        if ActivityStatusMachine.is_terminal(activity.status):
            return ActivityService._fail(
                ["This activity has been finalized or cancelled and cannot change."])

        errors = ActivityService._require(activity, actor, "finalize_field")
        errors += [f"{name} is not a field that can be finalized."
                   for name in field_names if name not in model.FINALIZABLE_FIELDS]
        if errors:
            return ActivityService._fail(errors)

        already = ActivityService._finalized(activity)
        added = [name for name in dict.fromkeys(field_names) if name not in already]
        for name in added:
            activity.finalized_fields.append(
                model.FinalizedField(field_name=name, finalized_by=actor.id))

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], added

    # --- FR-ACE-009 status ---------------------------------------------------

    @staticmethod
    def change_status(activity_id, actor, target, system=False):
        """Move the activity through the status machine. Returns (errors, activity).

        Runs ActivityStatusMachine.check_transition, then the side effects:
          CLOSED     pending join requests denied
          CANCELLED  pending join requests denied, waitlist cleared
          FINALIZED  every field finalized

        system=True skips the role check, for the automatic cancellation when
        the last member leaves. Only services pass it, never a route. The
        actor is still needed to scope the fetch to a university.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return ActivityService._fail([NOT_FOUND])

        if target == activity.status:
            db.session.rollback()
            return [], activity

        errors = ActivityService.apply_status(activity, actor, target, system=system)
        if errors:
            return ActivityService._fail(errors)

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], activity

    @staticmethod
    def apply_status(activity, actor, target, system=False):
        """change_status without the fetch or the commit. Returns errors.

        For a caller that already holds the activity's row lock inside its
        own transaction: ParticipationService cancels an activity when its
        last member leaves, in the same commit as the leave (NFR-REL-005).
        """
        if target == activity.status:
            return []

        finalized = ActivityService._finalized(activity)
        # The Admin may make every transition, so a system change checks as
        # one; the transition table and the finalized-status rule still apply.
        role = ADMIN if system else ActivityService._role(activity, actor)
        errors = ActivityStatusMachine.check_transition(
            activity.status, target, role, status_finalized="status" in finalized)
        if errors:
            return errors

        activity.status = target
        decided_by = None if system else actor.id

        if target in (CLOSED, CANCELLED):
            pending = db.session.execute(
                db.select(model.JoinRequest).where(
                    model.JoinRequest.activity_id == activity.id,
                    model.JoinRequest.status == model.REQUEST_PENDING)
            ).scalars().all()
            now = model.utcnow()
            for join_request in pending:
                join_request.status = model.REQUEST_DENIED
                join_request.reviewed_by = decided_by
                join_request.decided_at = now
                # FR-PE-002: the requester is told, whatever denied it.
                EventBus.publish(events.JOIN_REQUEST_DECIDED,
                                 request_id=join_request.id, activity_id=activity.id,
                                 user_id=join_request.user_id, approved=False,
                                 reason=target.lower())

        if target == CANCELLED:
            activity.waitlist.clear()

        if target == FINALIZED:
            for name in model.FINALIZABLE_FIELDS:
                if name not in finalized:
                    activity.finalized_fields.append(
                        model.FinalizedField(field_name=name, finalized_by=decided_by))

        ActivityService._audit(
            None if system else actor, "status_changed", "activity", activity.id,
            {"to": target, "system": system})
        return []

    # --- FR-ACE-006, 007 roles -----------------------------------------------

    @staticmethod
    def set_editor(activity_id, actor, member_id, grant):
        """Grant (grant=True) or revoke Editor. Returns (errors, membership).

        Requires grant_editor; the target must be a member and not the Admin.
        Audit-logged as editor_granted / editor_revoked.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return ActivityService._fail([NOT_FOUND])

        errors = ActivityService._require(activity, actor, "grant_editor")
        if errors:
            return ActivityService._fail(errors)

        membership = MembershipManager._get(activity, member_id)
        if membership is None:
            return ActivityService._fail(["Only a member of this activity can be an editor."])
        if membership.role == ADMIN:
            return ActivityService._fail(["The Admin's role cannot be changed here."])

        role = EDITOR if grant else ATTENDEE
        if membership.role == role:
            db.session.rollback()
            return [], membership

        MembershipManager.set_role(activity, member_id, role)
        ActivityService._audit(
            actor, "editor_granted" if grant else "editor_revoked",
            "activity_membership", activity.id, {"user_id": member_id})

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], membership

    @staticmethod
    def transfer_admin(activity_id, actor, member_id):
        """Hand the Admin role to another member. Returns (errors, membership).

        Requires transfer_admin. The old Admin becomes an Editor.
        Audit-logged as admin_transferred.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return ActivityService._fail([NOT_FOUND])

        errors = ActivityService._require(activity, actor, "transfer_admin")
        if errors:
            return ActivityService._fail(errors)

        if member_id == actor.id:
            return ActivityService._fail(["You are already the Admin."])
        if MembershipManager._get(activity, member_id) is None:
            return ActivityService._fail(["The new Admin must be a member of this activity."])

        membership = MembershipManager.transfer_admin(activity, actor.id, member_id)
        ActivityService._audit(
            actor, "admin_transferred", "activity_membership", activity.id,
            {"from": actor.id, "to": member_id})

        errors = ActivityService._commit()
        if errors:
            return errors, None
        return [], membership
