"""Database-backed tests for app/activities.py."""

import datetime

import pytest

from app import db, model
from app.activities import ActivityService as AS, MembershipManager
from app.activity_rules import (
    ADMIN, EDITOR, ATTENDEE, OPEN, CLOSED, FINALIZED, CANCELLED,
)


# ---------------------------------------------------------------
# Fixtures and factories
# ---------------------------------------------------------------

@pytest.fixture
def university(db):
    return db.session.execute(
        db.select(model.University).where(
            model.University.name == "Case Western Reserve University"
        )
    ).scalar_one()


@pytest.fixture
def make_user(db, university):
    counter = {"value": 0}

    def _make():
        counter["value"] += 1
        n = counter["value"]

        user = model.User(
            university_id=university.id,
            auth_provider_id=f"activity_test_{n}",
            email=f"activity_test_{n}@case.edu",
            first_name="Test",
            last_name=f"User{n}",
            birthday=datetime.date(2000, 1, 1),
            gender="Woman",
            school_year="Junior",
        )

        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture
def valid_fields():
    return {
        "title": "Study Group",
        "description": "Studying for finals together.",
        "meeting_point": "Thwing Center",
        "start_at": "2099-12-01T18:00",
        "end_at": "2099-12-01T20:00",
        "budget": "10",
        "max_attendees": 5,
        "destinations": ["Thwing Center", "Library"],
    }


@pytest.fixture
def make_activity(make_user, valid_fields):
    def _make(**overrides):
        admin = make_user()
        fields = {**valid_fields, **overrides}

        errors, activity = AS.create(admin, fields)

        assert errors == []
        assert activity is not None

        return activity, admin

    return _make


def add_member(activity, user):
    """Add a regular member while keeping attendee_count correct."""
    MembershipManager.add(activity, user.id, ATTENDEE)
    db.session.commit()


def get_audit_logs(activity, action=None):
    query = db.select(model.AuditLog).where(
        model.AuditLog.target_id == activity.id
    )

    if action:
        query = query.where(model.AuditLog.action == action)

    return db.session.execute(query).scalars().all()


# ---------------------------------------------------------------
# FR-ACE-001 to 004: Activity creation and validation
# ---------------------------------------------------------------

def test_create_activity(make_user, valid_fields):
    creator = make_user()

    errors, activity = AS.create(creator, valid_fields)

    assert errors == []
    assert activity is not None
    assert activity.creator_id == creator.id
    assert activity.status == OPEN
    assert activity.attendee_count == 1

    assert len(activity.memberships) == 1
    assert activity.memberships[0].user_id == creator.id
    assert activity.memberships[0].role == ADMIN

    assert len(activity.topics) == 1
    assert activity.topics[0].name == "Main"

    assert [d.text for d in activity.destinations] == [
        "Thwing Center", "Library"
    ]


def test_create_activity_without_university_is_rejected(make_user):
    creator = make_user()
    creator.university_id = None
    db.session.commit()

    errors, activity = AS.create(creator, {
        "title": "Study Group",
    })

    assert errors
    assert activity is None


@pytest.mark.parametrize("overrides", [
    {"end_at": "2099-12-01T17:00"},
    {"start_at": "2020-01-01T10:00"},
    {"budget": "-5"},
    {"max_attendees": 0},
    {"min_age": 40, "max_age": 20},
])
def test_invalid_activity_creation_is_rejected(
    make_user, valid_fields, overrides
):
    creator = make_user()
    fields = {**valid_fields, **overrides}

    errors, activity = AS.create(creator, fields)

    assert errors
    assert activity is None


# ---------------------------------------------------------------
# FR-ACE-005: Editing permissions
# ---------------------------------------------------------------

def test_admin_can_edit_activity(make_activity):
    activity, admin = make_activity()

    errors, result = AS.update(
        activity.id, admin, {"title": "New Study Group"}
    )

    assert errors == []
    assert result.activity.title == "New Study Group"


def test_editor_can_edit_activity(make_activity, make_user):
    activity, admin = make_activity()
    editor = make_user()
    add_member(activity, editor)

    errors, _ = AS.set_editor(
        activity.id, admin, editor.id, grant=True
    )
    assert errors == []

    errors, result = AS.update(
        activity.id, editor, {"title": "Edited by Editor"}
    )

    assert errors == []
    assert result.activity.title == "Edited by Editor"


def test_attendee_cannot_edit_activity(make_activity, make_user):
    activity, _ = make_activity()
    attendee = make_user()
    add_member(activity, attendee)

    errors, result = AS.update(
        activity.id, attendee, {"title": "Unauthorized Edit"}
    )

    assert errors
    assert result is None
    assert activity.title == "Study Group"


def test_updating_activity_to_invalid_values_is_rejected(make_activity):
    activity, admin = make_activity()

    errors, result = AS.update(
        activity.id, admin, {"budget": "-100"}
    )

    assert errors
    assert result is None
    assert float(activity.budget) == 10.0


def test_increasing_capacity_reports_seats_added(make_activity):
    activity, admin = make_activity()

    errors, result = AS.update(
        activity.id, admin, {"max_attendees": 10}
    )

    assert errors == []
    assert result.seats_added is True
    assert activity.max_attendees == 10


def test_decreasing_capacity_below_attendance_is_rejected(
    make_activity, make_user
):
    activity, admin = make_activity()
    attendee = make_user()
    add_member(activity, attendee)

    errors, result = AS.update(
        activity.id, admin, {"max_attendees": 1}
    )

    assert errors
    assert result is None
    assert activity.max_attendees == 5


# ---------------------------------------------------------------
# FR-ACE-008: Finalized fields
# ---------------------------------------------------------------

def test_admin_can_finalize_activity_field(make_activity):
    activity, admin = make_activity()

    errors, finalized = AS.finalize(
        activity.id, admin, ["budget"]
    )

    assert errors == []
    assert "budget" in finalized

    finalized_names = {
        row.field_name for row in activity.finalized_fields
    }
    assert "budget" in finalized_names


def test_finalized_field_cannot_be_edited(make_activity):
    activity, admin = make_activity()

    errors, _ = AS.finalize(activity.id, admin, ["budget"])
    assert errors == []

    errors, result = AS.update(
        activity.id, admin, {"budget": "50"}
    )

    assert errors
    assert result is None
    assert float(activity.budget) == 10.0


def test_invalid_field_cannot_be_finalized(make_activity):
    activity, admin = make_activity()

    errors, result = AS.finalize(
        activity.id, admin, ["not_a_real_field"]
    )

    assert errors
    assert result is None


# ---------------------------------------------------------------
# FR-ACE-009: Status changes
# ---------------------------------------------------------------

def test_admin_can_close_activity(make_activity):
    activity, admin = make_activity()

    errors, result = AS.change_status(
        activity.id, admin, CLOSED
    )

    assert errors == []
    assert result.status == CLOSED


def test_editor_cannot_cancel_activity(make_activity, make_user):
    activity, admin = make_activity()
    editor = make_user()
    add_member(activity, editor)

    errors, _ = AS.set_editor(
        activity.id, admin, editor.id, grant=True
    )
    assert errors == []

    errors, result = AS.change_status(
        activity.id, editor, CANCELLED
    )

    assert errors
    assert result is None
    assert activity.status == OPEN


def test_admin_can_cancel_activity(make_activity):
    activity, admin = make_activity()

    errors, result = AS.change_status(
        activity.id, admin, CANCELLED
    )

    assert errors == []
    assert result.status == CANCELLED


def test_finalized_activity_cannot_change_status(make_activity):
    activity, admin = make_activity()

    errors, _ = AS.change_status(
        activity.id, admin, CLOSED
    )
    assert errors == []

    errors, result = AS.change_status(
        activity.id, admin, FINALIZED
    )
    assert errors == []

    errors, result = AS.change_status(
        activity.id, admin, OPEN
    )

    assert errors
    assert result is None
    assert activity.status == FINALIZED


# ---------------------------------------------------------------
# FR-ACE-006 / FR-ACE-007: Editor and Admin management
# ---------------------------------------------------------------

def test_admin_can_grant_and_revoke_editor(make_activity, make_user):
    activity, admin = make_activity()
    member = make_user()
    add_member(activity, member)

    errors, membership = AS.set_editor(
        activity.id, admin, member.id, grant=True
    )

    assert errors == []
    assert membership.role == EDITOR

    errors, membership = AS.set_editor(
        activity.id, admin, member.id, grant=False
    )

    assert errors == []
    assert membership.role == ATTENDEE


def test_non_admin_cannot_grant_editor(make_activity, make_user):
    activity, admin = make_activity()
    member = make_user()
    another_member = make_user()

    add_member(activity, member)
    add_member(activity, another_member)

    errors, _ = AS.set_editor(
        activity.id, member, another_member.id, grant=True
    )

    assert errors
    assert another_member.id in [
        m.user_id for m in activity.memberships
    ]


def test_admin_cannot_grant_editor_to_non_member(make_activity, make_user):
    activity, admin = make_activity()
    outsider = make_user()

    errors, result = AS.set_editor(
        activity.id, admin, outsider.id, grant=True
    )

    assert errors
    assert result is None


def test_admin_can_transfer_admin_role(make_activity, make_user):
    activity, admin = make_activity()
    new_admin = make_user()
    add_member(activity, new_admin)

    errors, membership = AS.transfer_admin(
        activity.id, admin, new_admin.id
    )

    assert errors == []
    assert membership.role == ADMIN

    roles = {m.user_id: m.role for m in activity.memberships}

    assert roles[admin.id] == EDITOR
    assert roles[new_admin.id] == ADMIN
    assert sum(role == ADMIN for role in roles.values()) == 1


def test_admin_cannot_transfer_role_to_non_member(make_activity, make_user):
    activity, admin = make_activity()
    outsider = make_user()

    errors, result = AS.transfer_admin(
        activity.id, admin, outsider.id
    )

    assert errors
    assert result is None

    roles = {m.user_id: m.role for m in activity.memberships}
    assert roles[admin.id] == ADMIN


# ---------------------------------------------------------------
# NFR-SECU-012: Audit logging
# ---------------------------------------------------------------

def test_status_change_creates_audit_log(make_activity):
    activity, admin = make_activity()

    errors, _ = AS.change_status(
        activity.id, admin, CLOSED
    )

    assert errors == []

    logs = get_audit_logs(activity, "status_changed")

    assert len(logs) == 1
    assert logs[0].actor_id == admin.id
    assert logs[0].target_type == "activity"


def test_granting_editor_creates_audit_log(make_activity, make_user):
    activity, admin = make_activity()
    member = make_user()
    add_member(activity, member)

    errors, _ = AS.set_editor(
        activity.id, admin, member.id, grant=True
    )

    assert errors == []

    logs = get_audit_logs(activity, "editor_granted")

    assert len(logs) == 1
    assert logs[0].actor_id == admin.id


def test_transferring_admin_creates_audit_log(make_activity, make_user):
    activity, admin = make_activity()
    new_admin = make_user()
    add_member(activity, new_admin)

    errors, _ = AS.transfer_admin(
        activity.id, admin, new_admin.id
    )

    assert errors == []

    logs = get_audit_logs(activity, "admin_transferred")

    assert len(logs) == 1
    assert logs[0].actor_id == admin.id
