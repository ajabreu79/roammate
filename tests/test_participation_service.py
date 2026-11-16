"""Functional tests for app/participation.py (issue #17).

Covers FR-PE-001..010, FR-ACE-007 and the NFR-REL-005 invariants:
after every operation, attendee_count == number of membership rows and a
non-empty activity has exactly one ADMIN.

The factories near the top mirror tests/test_model.py; `app` and `db` come from
conftest.py. The suite runs on SQLite, where FOR UPDATE is a no-op, so these tests
check the logic and the invariants, not the concurrent-last-seat guarantee itself.
"""

import datetime
import itertools
from datetime import timedelta

import pytest

from app import db, model
from app.activities import MembershipManager
from app.activity_rules import ADMIN, APPROVAL_REQUIRED, CANCELLED
from .conftest import years_ago
from app.participation import (
    JOINED, WAITLISTED, ParticipationService as PS, WaitlistService,
)

# ------------------------------------------------------------------ factories
# Same shape as the helpers in test_model.py. `app` and `db` come from conftest.py.
OPEN = model.STATUS_OPEN
CLOSED = "CLOSED"          # TODO: confirm these two names against activity_rules
FINALIZED = "FINALIZED"
FRESHMAN, SENIOR = "Freshman", "Senior"      # TODO: confirm stored school_year values
JUNIOR = "Junior"
WOMAN, MAN = "Woman", "Man"

_ids = itertools.count(1)


def birthday_for(age):
    """A birthday that makes the user `age` today, a month clear of the boundary."""
    return datetime.date.fromisoformat(years_ago(age, days=-30))


@pytest.fixture
def university(db):
    return db.session.execute(
        db.select(model.University).where(
            model.University.name == "Case Western Reserve University")
    ).scalar_one()


@pytest.fixture
def make_user(db, university):
    def _make(age=20, **overrides):
        n = next(_ids)
        fields = dict(
            university_id=university.id, auth_provider_id=f"user_{n}",
            email=f"user{n}@case.edu", first_name="Test", last_name=f"User{n}",
            birthday=birthday_for(age), gender=WOMAN, school_year=JUNIOR,
        )
        fields.update(overrides)
        user = model.User(**fields)
        db.session.add(user)
        db.session.commit()
        return user
    return _make


@pytest.fixture
def make_activity(db, university, make_user):
    """Creates an activity with its creator as the one ADMIN member."""
    def _make(admin=None, status=OPEN, max_attendees=3, **overrides):
        admin = admin or make_user()
        fields = dict(
            university_id=university.id, creator_id=admin.id, title="Concert",
            description="Come along.", meeting_point="Thwing Center",
            start_at=datetime.datetime(2026, 12, 1, 18),
            end_at=datetime.datetime(2026, 12, 1, 22),
            max_attendees=max_attendees, status=status,
        )
        fields.update(overrides)
        activity = model.Activity(**fields)
        db.session.add(activity)
        db.session.flush()
        MembershipManager.add(activity, admin.id)
        # TODO: if MembershipManager.add takes a role, pass ADMIN there instead.
        next(m for m in activity.memberships if m.user_id == admin.id).role = ADMIN
        db.session.commit()
        return activity, admin
    return _make


# ------------------------------------------------------------------ helpers
def reload(activity):
    db.session.expire_all()
    return db.session.get(model.Activity, activity.id)


def member_ids(activity):
    return {m.user_id for m in reload(activity).memberships}


def waitlist_ids(activity):
    return [e.user_id for e in
            sorted(reload(activity).waitlist, key=lambda e: e.created_at)]


def add_member(activity, user):
    MembershipManager.add(activity, user.id)
    db.session.commit()


def enqueue(activity, user, minutes):
    """Put `user` on the waitlist at a fixed time so ordering is deterministic."""
    base = model.utcnow()
    db.session.add(model.WaitlistEntry(
        activity_id=activity.id, user_id=user.id,
        created_at=base + timedelta(minutes=minutes)))
    db.session.commit()


def fill_to_capacity(activity, make_user):
    """Add members until no seats are left. Returns the users added."""
    added = []
    while reload(activity).seats_left > 0:
        u = make_user()
        add_member(activity, u)
        added.append(u)
    return added


def assert_invariants(activity):
    """NFR-REL-005: counts match rows, exactly one ADMIN while anyone is left."""
    a = reload(activity)
    assert a.attendee_count == len(a.memberships)
    admins = [m for m in a.memberships if m.role == ADMIN]
    assert len(admins) == (1 if a.memberships else 0)
    ids = [m.user_id for m in a.memberships]
    assert len(ids) == len(set(ids)), "duplicate membership rows"


# ------------------------------------------------- FR-PE-001 / FR-PE-003 join
def test_join_open_activity(make_activity, make_user):
    activity, _ = make_activity()
    user = make_user()

    errors, outcome = PS.join(activity.id, user)

    assert errors == []
    assert outcome == JOINED
    assert user.id in member_ids(activity)
    assert_invariants(activity)


def test_joining_twice_is_rejected(make_activity, make_user):
    activity, _ = make_activity()
    user = make_user()
    PS.join(activity.id, user)

    errors, outcome = PS.join(activity.id, user)

    assert errors
    assert outcome is None
    assert len([m for m in reload(activity).memberships if m.user_id == user.id]) == 1
    assert_invariants(activity)


def test_approval_required_activity_refuses_direct_join(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED)
    user = make_user()

    errors, _ = PS.join(activity.id, user)

    assert errors
    assert user.id not in member_ids(activity)
    assert_invariants(activity)


# --------------------------------------------- FR-PE-004 / FR-PE-010 waitlist
def test_join_full_activity_goes_to_waitlist(make_activity, make_user):
    activity, _ = make_activity(max_attendees=2)
    fill_to_capacity(activity, make_user)
    late = make_user()

    errors, outcome = PS.join(activity.id, late)

    assert errors == []
    assert outcome == WAITLISTED
    assert late.id not in member_ids(activity)
    assert waitlist_ids(activity) == [late.id]
    assert_invariants(activity)


def test_joining_waitlist_twice_is_rejected(make_activity, make_user):
    activity, _ = make_activity(max_attendees=1)
    late = make_user()
    PS.join(activity.id, late)

    errors, _ = PS.join(activity.id, late)

    assert errors
    assert waitlist_ids(activity) == [late.id]


def test_leave_waitlist(make_activity, make_user):
    activity, _ = make_activity(max_attendees=1)
    late = make_user()
    PS.join(activity.id, late)

    errors, _ = PS.leave_waitlist(activity.id, late)

    assert errors == []
    assert waitlist_ids(activity) == []


def test_leave_waitlist_when_not_queued(make_activity, make_user):
    activity, _ = make_activity(max_attendees=1)

    errors, _ = PS.leave_waitlist(activity.id, make_user())

    assert errors


# ------------------------------------ FR-PE-005 / FR-PE-009 eligibility rules
@pytest.mark.parametrize("rule, user_fields", [
    ({"min_age": 18}, {"age": 16}),
    ({"max_age": 25}, {"age": 30}),
    ({"min_year": SENIOR}, {"school_year": FRESHMAN}),
    ({"max_year": FRESHMAN}, {"school_year": SENIOR}),
    ({"allowed_genders": MAN}, {"gender": WOMAN}),
], ids=["too-young", "too-old", "year-too-low", "year-too-high", "wrong-gender"])
def test_ineligible_user_cannot_join(make_activity, make_user, rule, user_fields):
    activity, _ = make_activity(**rule)
    user = make_user(**user_fields)

    errors, outcome = PS.join(activity.id, user)

    assert errors
    assert outcome is None
    assert user.id not in member_ids(activity)
    assert waitlist_ids(activity) == []
    assert_invariants(activity)


def test_ineligible_user_is_not_waitlisted_when_full(make_activity, make_user):
    activity, _ = make_activity(max_attendees=1, min_age=18)
    young = make_user(age=16)

    errors, _ = PS.join(activity.id, young)

    assert errors
    assert waitlist_ids(activity) == []


# ----------------------------------------------------------- FR-PE-008 status
@pytest.mark.parametrize("status", [CLOSED, FINALIZED, CANCELLED])
def test_join_blocked_for_terminal_or_closed_status(make_activity, make_user, status):
    activity, _ = make_activity(status=status)
    user = make_user()

    errors, outcome = PS.join(activity.id, user)

    # Depending on visibility rules this is "not found" or an eligibility
    # message; either way the user must not get in.
    assert errors
    assert outcome is None
    assert user.id not in member_ids(activity)


# ------------------------------------------------------ FR-PE-002 join requests
def test_request_then_approve_adds_member(make_activity, make_user):
    activity, admin = make_activity(status=APPROVAL_REQUIRED)
    user = make_user()

    errors, req = PS.request_to_join(activity.id, user, "Please let me in")
    assert errors == []
    assert req.status == model.REQUEST_PENDING
    assert user.id not in member_ids(activity)

    errors, decided = PS.decide_request(activity.id, admin, req.id, approve=True)

    assert errors == []
    assert decided.status == model.REQUEST_APPROVED
    assert decided.reviewed_by == admin.id
    assert decided.decided_at is not None
    assert user.id in member_ids(activity)
    assert_invariants(activity)


def test_deny_does_not_add_member_and_keeps_row(make_activity, make_user):
    activity, admin = make_activity(status=APPROVAL_REQUIRED)
    user = make_user()
    _, req = PS.request_to_join(activity.id, user)

    errors, decided = PS.decide_request(activity.id, admin, req.id, approve=False)

    assert errors == []
    assert decided.status == model.REQUEST_DENIED
    assert user.id not in member_ids(activity)
    assert db.session.get(model.JoinRequest, req.id) is not None
    assert_invariants(activity)


def test_cannot_decide_a_request_twice(make_activity, make_user):
    activity, admin = make_activity(status=APPROVAL_REQUIRED)
    _, req = PS.request_to_join(activity.id, make_user())
    PS.decide_request(activity.id, admin, req.id, approve=False)

    errors, _ = PS.decide_request(activity.id, admin, req.id, approve=True)

    assert errors


def test_duplicate_pending_request_is_rejected(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED)
    user = make_user()
    PS.request_to_join(activity.id, user)

    errors, _ = PS.request_to_join(activity.id, user)

    assert errors


def test_request_on_open_activity_is_rejected(make_activity, make_user):
    activity, _ = make_activity()

    errors, _ = PS.request_to_join(activity.id, make_user())

    assert errors


def test_overlong_request_message_is_rejected(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED)

    errors, _ = PS.request_to_join(
        activity.id, make_user(), "x" * (model.MAX_DESCRIPTION_LENGTH + 1))

    assert errors


def test_eligibility_is_rechecked_at_approval(make_activity, make_user):
    activity, admin = make_activity(status=APPROVAL_REQUIRED, max_attendees=3)
    user = make_user()
    _, req = PS.request_to_join(activity.id, user)

    # The activity fills up while the request waits.
    fill_to_capacity(activity, make_user)

    errors, _ = PS.decide_request(activity.id, admin, req.id, approve=True)

    assert errors
    assert user.id not in member_ids(activity)
    assert db.session.get(model.JoinRequest, req.id).status == model.REQUEST_PENDING
    assert_invariants(activity)


def test_non_reviewer_cannot_decide(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED, max_attendees=5)
    plain_member = make_user()
    add_member(activity, plain_member)
    _, req = PS.request_to_join(activity.id, make_user())

    errors, _ = PS.decide_request(activity.id, plain_member, req.id, approve=True)

    assert errors
    assert db.session.get(model.JoinRequest, req.id).status == model.REQUEST_PENDING


def test_withdraw_pending_request(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED)
    user = make_user()
    _, req = PS.request_to_join(activity.id, user)

    errors, _ = PS.withdraw_request(activity.id, user)

    assert errors == []
    assert db.session.get(model.JoinRequest, req.id) is None


def test_withdraw_without_pending_request(make_activity, make_user):
    activity, _ = make_activity(status=APPROVAL_REQUIRED)

    errors, _ = PS.withdraw_request(activity.id, make_user())

    assert errors


# --------------------------------------------- FR-PE-006 / FR-PE-010 leaving
def test_regular_member_leaves(make_activity, make_user):
    activity, admin = make_activity()
    user = make_user()
    add_member(activity, user)

    errors, result = PS.leave(activity.id, user)

    assert errors == []
    assert result.cancelled is False
    assert result.promoted == []
    assert member_ids(activity) == {admin.id}
    assert_invariants(activity)


def test_leaving_when_not_a_member(make_activity, make_user):
    activity, _ = make_activity()

    errors, _ = PS.leave(activity.id, make_user())

    assert errors


def test_leave_promotes_earliest_waitlist_entry(make_activity, make_user):
    activity, admin = make_activity(max_attendees=2)
    member = make_user()
    add_member(activity, member)
    first, second = make_user(), make_user()
    enqueue(activity, second, minutes=5)
    enqueue(activity, first, minutes=1)

    errors, result = PS.leave(activity.id, member)

    assert errors == []
    assert result.promoted == [first.id]
    assert member_ids(activity) == {admin.id, first.id}
    assert waitlist_ids(activity) == [second.id]
    assert_invariants(activity)


def test_promotion_skips_ineligible_entry_but_keeps_its_place(make_activity, make_user):
    activity, admin = make_activity(max_attendees=2)
    member = make_user()
    add_member(activity, member)
    too_young, eligible = make_user(age=16), make_user(age=20)
    enqueue(activity, too_young, minutes=1)    # earlier, but no longer eligible
    enqueue(activity, eligible, minutes=2)
    activity = reload(activity)
    activity.min_age = 18
    db.session.commit()

    errors, result = PS.leave(activity.id, member)

    assert errors == []
    assert result.promoted == [eligible.id]
    assert waitlist_ids(activity) == [too_young.id]
    assert_invariants(activity)


def test_fill_seats_after_capacity_increase(make_activity, make_user):
    activity, admin = make_activity(max_attendees=1)
    waiting = make_user()
    PS.join(activity.id, waiting)
    a = reload(activity)
    a.max_attendees = 2
    db.session.commit()

    errors, promoted = PS.fill_seats(activity.id, admin)

    assert errors == []
    assert promoted == [waiting.id]
    assert member_ids(activity) == {admin.id, waiting.id}
    assert_invariants(activity)


# ----------------------------------------------------------- FR-ACE-007 Admin
def test_admin_leaving_must_name_successor(make_activity, make_user):
    activity, admin = make_activity()
    other = make_user()
    add_member(activity, other)

    errors, result = PS.leave(activity.id, admin)

    assert errors
    assert result is None
    assert member_ids(activity) == {admin.id, other.id}   # rolled back
    assert_invariants(activity)


def test_successor_must_be_a_member(make_activity, make_user):
    activity, admin = make_activity()
    add_member(activity, make_user())
    outsider = make_user()

    errors, _ = PS.leave(activity.id, admin, successor_id=outsider.id)

    assert errors
    assert admin.id in member_ids(activity)
    assert_invariants(activity)


def test_admin_leaves_with_successor(make_activity, make_user):
    activity, admin = make_activity()
    other = make_user()
    add_member(activity, other)

    errors, result = PS.leave(activity.id, admin, successor_id=other.id)

    assert errors == []
    assert result.cancelled is False
    a = reload(activity)
    assert {m.user_id: m.role for m in a.memberships} == {other.id: ADMIN}
    assert_invariants(activity)


# --------------------------------------------------------- FR-PE-006 last one
def test_last_member_leaving_cancels_activity(make_activity):
    activity, admin = make_activity()

    errors, result = PS.leave(activity.id, admin)

    assert errors == []
    assert result.cancelled is True
    a = reload(activity)
    assert a.status == CANCELLED
    assert a.memberships == []
    assert_invariants(activity)


# ------------------------------------------------------------- remove_member
def test_admin_removes_member_and_waitlist_fills(make_activity, make_user):
    activity, admin = make_activity(max_attendees=2)
    victim, waiting = make_user(), make_user()
    add_member(activity, victim)
    enqueue(activity, waiting, minutes=1)

    errors, promoted = PS.remove_member(activity.id, admin, victim.id)

    assert errors == []
    assert promoted == [waiting.id]
    assert member_ids(activity) == {admin.id, waiting.id}
    assert_invariants(activity)


def test_non_admin_cannot_remove_member(make_activity, make_user):
    activity, admin = make_activity(max_attendees=4)
    a, b = make_user(), make_user()
    add_member(activity, a)
    add_member(activity, b)

    errors, _ = PS.remove_member(activity.id, a, b.id)

    assert errors
    assert member_ids(activity) == {admin.id, a.id, b.id}
    assert_invariants(activity)


def test_admin_cannot_remove_self_via_remove_member(make_activity, make_user):
    activity, admin = make_activity()
    add_member(activity, make_user())

    errors, _ = PS.remove_member(activity.id, admin, admin.id)

    assert errors
    assert admin.id in member_ids(activity)


def test_remove_non_member(make_activity, make_user):
    activity, admin = make_activity()

    errors, _ = PS.remove_member(activity.id, admin, make_user().id)

    assert errors


# ------------------------------------------------- FR-AUTH-017 account deletion
def test_leave_all_hands_over_admin_and_cancels_empty_activities(make_activity, make_user):
    user = make_user()
    shared, _ = make_activity(admin=user)
    heir = make_user()
    add_member(shared, heir)
    solo, _ = make_activity(admin=user)

    errors = PS.leave_all(user)
    db.session.commit()   # AccountDeletionService does this in production

    assert errors == []
    shared_after = reload(shared)
    assert {m.user_id: m.role for m in shared_after.memberships} == {heir.id: ADMIN}
    assert reload(solo).status == CANCELLED
    assert_invariants(shared)
    assert_invariants(solo)


# -------------------------------------------------------------- unit: promote
def test_promote_drops_entries_for_existing_members(make_activity, make_user):
    activity, _ = make_activity(max_attendees=3)
    user = make_user()
    add_member(activity, user)
    enqueue(activity, user, minutes=1)   # stale entry for someone already in

    WaitlistService.promote(reload(activity))
    db.session.commit()

    assert waitlist_ids(activity) == []
    assert_invariants(activity)