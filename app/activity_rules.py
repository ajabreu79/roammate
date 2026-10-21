"""Activity rules, isolated from the database so they can be unit-tested.

The design doc keeps these in small dedicated classes -- ActivityValidator,
ActivityStatusMachine, RolePolicy, EligibilityChecker -- for two reasons:
NFR-SOFT-002 wants this logic testable without a database, and NFR-SOFT-009
wants a new status or role to touch one place per layer.

Nothing here imports the model, so none of it depends on the Activity schema
being defined yet. Callers pass plain values and get back a list of failures.
"""

from datetime import date, datetime

# Roles, highest privilege first (FR-ACE-007).
ADMIN = "ADMIN"
EDITOR = "EDITOR"
ATTENDEE = "ATTENDEE"

# Statuses (FR-ACE-009).
OPEN = "OPEN"
APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
CLOSED = "CLOSED"
FINALIZED = "FINALIZED"
CANCELLED = "CANCELLED"

MAX_DESCRIPTION_LENGTH = 500


class RolePolicy:
    """The FR-ACE-007 permission matrix, exactly as the design doc states it."""

    _MATRIX = {
        "view":              {ADMIN, EDITOR, ATTENDEE},
        "post_message":      {ADMIN, EDITOR, ATTENDEE},
        "create_topic":      {ADMIN, EDITOR, ATTENDEE},
        "edit_fields":       {ADMIN, EDITOR},
        "finalize_field":    {ADMIN, EDITOR},
        "change_status":     {ADMIN, EDITOR},
        "cancel":            {ADMIN},
        "review_requests":   {ADMIN, EDITOR},
        "invite":            {ADMIN, EDITOR},
        "grant_editor":      {ADMIN},
        "transfer_admin":    {ADMIN},
        "remove_member":     {ADMIN},
        "leave":             {ADMIN, EDITOR, ATTENDEE},
    }

    @classmethod
    def can(cls, role, action):
        """Whether `role` may perform `action`. Unknown role or action is a no."""
        return role in cls._MATRIX.get(action, set())

    @classmethod
    def actions(cls):
        return sorted(cls._MATRIX)


class ActivityStatusMachine:
    """The FR-ACE-009 transition table."""

    _TRANSITIONS = {
        OPEN: {APPROVAL_REQUIRED, CLOSED, CANCELLED},
        APPROVAL_REQUIRED: {OPEN, CLOSED, CANCELLED},
        CLOSED: {FINALIZED, CANCELLED},
        FINALIZED: set(),
        CANCELLED: set(),
    }

    # FR-PE-008: joining is blocked outside these two.
    _JOINABLE = {OPEN, APPROVAL_REQUIRED}

    TERMINAL = {FINALIZED, CANCELLED}

    @classmethod
    def is_terminal(cls, status):
        return status in cls.TERMINAL

    @classmethod
    def is_joinable(cls, status):
        return status in cls._JOINABLE

    @classmethod
    def accepts_direct_join(cls, status):
        """FR-PE-001: only OPEN admits a member without approval."""
        return status == OPEN

    @classmethod
    def check_transition(cls, current, target, role, status_finalized=False):
        """Return the reasons this transition is refused; empty means allowed."""
        errors = []

        if current not in cls._TRANSITIONS:
            return [f"Unknown activity status: {current}."]
        if target not in cls._TRANSITIONS:
            return [f"Unknown target status: {target}."]
        if current == target:
            return []

        # FR-ACE-008: a finalized status field is immutable.
        if status_finalized:
            errors.append("The status of this activity has been finalized and cannot change.")
        if cls.is_terminal(current):
            errors.append(f"{current} is a terminal status and cannot change.")
        elif target not in cls._TRANSITIONS[current]:
            errors.append(f"An activity cannot move from {current} to {target}.")

        # Cancelling is Admin-only; every other transition is Admin or Editor.
        action = "cancel" if target == CANCELLED else "change_status"
        if not RolePolicy.can(role, action):
            errors.append("You do not have permission to change the status of this activity.")

        return errors


class ActivityValidator:
    """FR-ACE-004 field rules, plus the FR-ACE-001 required fields."""

    @staticmethod
    def _as_number(value, label, errors, minimum=None, integer=False):
        if value in (None, ""):
            return None
        try:
            number = int(value) if integer else float(value)
        except (TypeError, ValueError):
            errors.append(f"{label} must be a number.")
            return None
        if minimum is not None and number < minimum:
            errors.append(f"{label} must be {minimum} or more.")
            return None
        return number

    @staticmethod
    def _as_datetime(value, label, errors):
        if not value:
            errors.append(f"{label} is required.")
            return None
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        errors.append(f"{label} is not a valid date and time.")
        return None

    @classmethod
    def validate(cls, title=None, description=None, meeting_point=None,
                 start=None, end=None, budget=None, max_members=None,
                 min_age=None, max_age=None, today=None):
        """Return (errors, cleaned). `cleaned` holds the parsed values."""
        errors = []

        # FR-ACE-001 required fields.
        for value, label in ((title, "Title"), (description, "Description"),
                             (meeting_point, "Meeting point")):
            if not (value or "").strip():
                errors.append(f"{label} is required.")

        if description and len(description) > MAX_DESCRIPTION_LENGTH:
            errors.append(f"Description must be {MAX_DESCRIPTION_LENGTH} characters or fewer.")

        start_at = cls._as_datetime(start, "Start", errors)
        end_at = cls._as_datetime(end, "End", errors)

        # FR-ACE-004: end on or after start, and not in the past.
        if start_at and end_at and end_at < start_at:
            errors.append("End must be on or after start.")
        if start_at and start_at.date() < (today or date.today()):
            errors.append("Start cannot be in the past.")

        budget_value = cls._as_number(budget, "Budget", errors, minimum=0)

        max_members_value = cls._as_number(max_members, "Max attendees", errors, integer=True)
        if max_members_value is not None and max_members_value < 1:
            errors.append("Max attendees must be more than 0.")
            max_members_value = None

        min_age_value = cls._as_number(min_age, "Minimum age", errors, minimum=0, integer=True)
        max_age_value = cls._as_number(max_age, "Maximum age", errors, minimum=0, integer=True)
        if min_age_value is not None and max_age_value is not None and min_age_value > max_age_value:
            errors.append("Minimum age cannot be above maximum age.")

        cleaned = {
            "start": start_at, "end": end_at, "budget": budget_value,
            "max_members": max_members_value,
            "min_age": min_age_value, "max_age": max_age_value,
        }
        return errors, cleaned


class EligibilityChecker:
    """FR-PE-003 to 009. Returns every failed rule, not just the first."""

    @classmethod
    def check(cls, status, already_member=False, attendee_count=0, max_members=None,
              viewer_age=None, min_age=None, max_age=None,
              viewer_school_year=None, allowed_years=None,
              viewer_gender=None, allowed_genders=None,
              viewer_university=None, activity_university=None):
        errors = []

        # FR-PROF-009: same university, always, not only when finalized.
        if activity_university and viewer_university != activity_university:
            errors.append("This activity is for students at another university.")

        # FR-PE-008
        if not ActivityStatusMachine.is_joinable(status):
            errors.append("This activity is not open for joining.")

        # FR-PE-003
        if already_member:
            errors.append("You are already part of this activity.")

        # FR-PE-004
        if max_members is not None and attendee_count >= max_members:
            errors.append("This activity is already full.")

        # FR-PE-005 age. Applies whether or not the range is finalized.
        if min_age is not None or max_age is not None:
            if viewer_age is None:
                errors.append("Your birthday is needed to check eligibility.")
            else:
                if min_age is not None and viewer_age < min_age:
                    errors.append("You are below the minimum age for this activity.")
                if max_age is not None and viewer_age > max_age:
                    errors.append("You are above the maximum age for this activity.")

        # FR-PE-005 school year.
        if allowed_years and viewer_school_year not in allowed_years:
            errors.append("Your school year is not eligible for this activity.")

        # FR-PE-009 gender preference.
        if allowed_genders and viewer_gender not in allowed_genders:
            errors.append("This activity is limited to other gender preferences.")

        return errors

    @classmethod
    def is_eligible(cls, **kwargs):
        return not cls.check(**kwargs)
