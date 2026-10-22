"""Unit tests for the activity rules, no database involved (NFR-SOFT-002)."""

import datetime

import pytest

from app.activity_rules import (
    ADMIN, APPROVAL_REQUIRED, ATTENDEE, CANCELLED, CLOSED, EDITOR, FINALIZED, OPEN,
    ActivityStatusMachine, ActivityValidator, EligibilityChecker, RolePolicy,
)

TODAY = datetime.date(2026, 10, 21)
FUTURE = "2026-12-01T18:00"
LATER = "2026-12-01T22:00"


# --- FR-ACE-007 role permissions ---------------------------------------------

def test_an_attendee_cannot_edit_fields():
    """The defect this replaces let any attendee edit an activity."""
    assert not RolePolicy.can(ATTENDEE, "edit_fields")
    assert RolePolicy.can(EDITOR, "edit_fields")
    assert RolePolicy.can(ADMIN, "edit_fields")


def test_only_admin_may_grant_editor_or_cancel():
    for action in ("grant_editor", "cancel", "transfer_admin", "remove_member"):
        assert RolePolicy.can(ADMIN, action)
        assert not RolePolicy.can(EDITOR, action), action
        assert not RolePolicy.can(ATTENDEE, action), action


def test_everyone_may_view_post_and_leave():
    for role in (ADMIN, EDITOR, ATTENDEE):
        assert RolePolicy.can(role, "view")
        assert RolePolicy.can(role, "post_message")
        assert RolePolicy.can(role, "leave")


def test_an_unknown_role_or_action_is_refused():
    assert not RolePolicy.can("SUPERUSER", "edit_fields")
    assert not RolePolicy.can(ADMIN, "delete_everything")
    assert not RolePolicy.can(None, "view")


# --- FR-ACE-009 status transitions -------------------------------------------

@pytest.mark.parametrize("current, target", [
    (OPEN, APPROVAL_REQUIRED), (APPROVAL_REQUIRED, OPEN),
    (OPEN, CLOSED), (APPROVAL_REQUIRED, CLOSED), (CLOSED, FINALIZED),
])
def test_allowed_transitions(current, target):
    assert ActivityStatusMachine.check_transition(current, target, EDITOR) == []


@pytest.mark.parametrize("current, target", [
    (OPEN, FINALIZED), (CLOSED, OPEN), (FINALIZED, OPEN), (CANCELLED, OPEN),
    (FINALIZED, CANCELLED), (APPROVAL_REQUIRED, FINALIZED),
])
def test_refused_transitions(current, target):
    assert ActivityStatusMachine.check_transition(current, target, ADMIN) != []


def test_terminal_statuses_are_terminal():
    assert ActivityStatusMachine.is_terminal(FINALIZED)
    assert ActivityStatusMachine.is_terminal(CANCELLED)
    assert not ActivityStatusMachine.is_terminal(CLOSED)


def test_only_admin_may_cancel():
    assert ActivityStatusMachine.check_transition(OPEN, CANCELLED, ADMIN) == []
    assert ActivityStatusMachine.check_transition(OPEN, CANCELLED, EDITOR) != []


def test_an_attendee_cannot_change_status():
    assert ActivityStatusMachine.check_transition(OPEN, CLOSED, ATTENDEE) != []


def test_a_finalized_status_field_is_immutable():
    """FR-ACE-008: finalization is one-way."""
    errors = ActivityStatusMachine.check_transition(OPEN, CLOSED, ADMIN, status_finalized=True)
    assert any("finalized" in error for error in errors)


def test_a_no_op_transition_is_allowed():
    assert ActivityStatusMachine.check_transition(OPEN, OPEN, ATTENDEE) == []


def test_joinable_statuses():
    assert ActivityStatusMachine.is_joinable(OPEN)
    assert ActivityStatusMachine.is_joinable(APPROVAL_REQUIRED)
    for status in (CLOSED, FINALIZED, CANCELLED):
        assert not ActivityStatusMachine.is_joinable(status), status
    # FR-PE-001 vs FR-PE-002: only OPEN admits without approval.
    assert ActivityStatusMachine.accepts_direct_join(OPEN)
    assert not ActivityStatusMachine.accepts_direct_join(APPROVAL_REQUIRED)


# --- FR-ACE-004 validation ---------------------------------------------------

def valid_fields(**overrides):
    fields = {
        "title": "Concert downtown", "description": "Come along.",
        "meeting_point": "Thwing Center", "start": FUTURE, "end": LATER,
        "budget": "20", "max_members": "8", "min_age": "18", "max_age": "30",
        "today": TODAY,
    }
    fields.update(overrides)
    return fields


def test_a_valid_activity_passes():
    errors, cleaned = ActivityValidator.validate(**valid_fields())
    assert errors == []
    assert cleaned["max_members"] == 8 and cleaned["budget"] == 20.0


@pytest.mark.parametrize("field", ["title", "description", "meeting_point"])
def test_required_fields(field):
    errors, _ = ActivityValidator.validate(**valid_fields(**{field: "   "}))
    assert any("required" in error for error in errors)


def test_end_before_start_is_refused():
    errors, _ = ActivityValidator.validate(**valid_fields(start=LATER, end=FUTURE))
    assert any("on or after start" in error for error in errors)


def test_end_equal_to_start_is_allowed():
    errors, _ = ActivityValidator.validate(**valid_fields(start=FUTURE, end=FUTURE))
    assert errors == []


def test_a_start_in_the_past_is_refused():
    errors, _ = ActivityValidator.validate(**valid_fields(start="2026-10-20T10:00",
                                                          end="2026-10-20T12:00"))
    assert any("in the past" in error for error in errors)


def test_a_negative_budget_is_refused():
    errors, _ = ActivityValidator.validate(**valid_fields(budget="-1"))
    assert any("Budget" in error for error in errors)


def test_a_zero_budget_is_allowed():
    errors, _ = ActivityValidator.validate(**valid_fields(budget="0"))
    assert errors == []


def test_max_members_must_exceed_zero():
    errors, _ = ActivityValidator.validate(**valid_fields(max_members="0"))
    assert any("more than 0" in error for error in errors)


def test_min_age_above_max_age_is_refused():
    errors, _ = ActivityValidator.validate(**valid_fields(min_age="30", max_age="20"))
    assert any("Minimum age" in error for error in errors)


def test_a_description_over_the_cap_is_refused():
    errors, _ = ActivityValidator.validate(**valid_fields(description="x" * 501))
    assert any("500 characters" in error for error in errors)


def test_nonsense_numbers_are_a_validation_error_not_a_crash():
    errors, _ = ActivityValidator.validate(**valid_fields(budget="free", max_members="lots"))
    assert len(errors) == 2


def test_a_malformed_date_is_a_validation_error_not_a_crash():
    errors, _ = ActivityValidator.validate(**valid_fields(start="31/12/2026"))
    assert any("not a valid date" in error for error in errors)


def test_every_failure_is_reported_together():
    errors, _ = ActivityValidator.validate(title="", description="", meeting_point="",
                                           start="", end="", budget="-5", max_members="0",
                                           min_age="40", max_age="20", today=TODAY)
    assert len(errors) >= 7


# --- FR-PE-003 to 009 eligibility --------------------------------------------

def eligible(**overrides):
    state = {
        "status": OPEN, "already_member": False, "attendee_count": 2, "max_members": 8,
        "viewer_age": 22, "min_age": 18, "max_age": 30,
        "viewer_university": "Case Western Reserve University",
        "activity_university": "Case Western Reserve University",
    }
    state.update(overrides)
    return state


def test_an_eligible_user_passes():
    assert EligibilityChecker.check(**eligible()) == []


def test_an_existing_member_cannot_join_again():
    assert EligibilityChecker.check(**eligible(already_member=True)) != []


def test_a_full_activity_is_refused():
    assert EligibilityChecker.check(**eligible(attendee_count=8, max_members=8)) != []


def test_the_last_seat_is_still_joinable():
    assert EligibilityChecker.check(**eligible(attendee_count=7, max_members=8)) == []


@pytest.mark.parametrize("status", [CLOSED, FINALIZED, CANCELLED])
def test_unjoinable_statuses_are_refused(status):
    """FR-PE-008."""
    assert EligibilityChecker.check(**eligible(status=status)) != []


def test_age_is_enforced_regardless_of_finalization():
    """The defect this replaces only checked age if the range was finalized."""
    assert EligibilityChecker.check(**eligible(viewer_age=17)) != []
    assert EligibilityChecker.check(**eligible(viewer_age=31)) != []
    assert EligibilityChecker.check(**eligible(viewer_age=18)) == []
    assert EligibilityChecker.check(**eligible(viewer_age=30)) == []


def test_a_missing_age_cannot_pass_an_age_gate():
    assert EligibilityChecker.check(**eligible(viewer_age=None)) != []


def test_another_university_is_always_refused():
    """FR-PROF-009: not conditional on a finalized flag or a query parameter."""
    assert EligibilityChecker.check(**eligible(viewer_university="Ohio State University")) != []


def test_school_year_is_enforced():
    assert EligibilityChecker.check(
        **eligible(allowed_years=["Junior", "Senior"], viewer_school_year="First year")) != []
    assert EligibilityChecker.check(
        **eligible(allowed_years=["Junior", "Senior"], viewer_school_year="Senior")) == []


def test_gender_preference_is_enforced_when_set():
    """FR-PE-009."""
    assert EligibilityChecker.check(
        **eligible(allowed_genders=["Woman"], viewer_gender="Man")) != []
    assert EligibilityChecker.check(
        **eligible(allowed_genders=["Woman"], viewer_gender="Woman")) == []
    # Unset means no restriction.
    assert EligibilityChecker.check(**eligible(allowed_genders=None, viewer_gender="Man")) == []


def test_all_failures_are_returned_not_just_the_first():
    errors = EligibilityChecker.check(**eligible(
        status=CLOSED, already_member=True, attendee_count=9, max_members=8, viewer_age=12))
    assert len(errors) >= 4
