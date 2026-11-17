"""Functional tests for app/feed.py (FR-PROF-006, FR-AD-001).

The feed ranks; it never writes. So each test builds a small world, asks
FeedService.for_user for the viewer's feed, and checks the order and the
reasons. `now` is passed in so the fixed activity dates never go stale.
"""

import datetime
import itertools

import pytest

from app import db, model
from app.activities import MembershipManager
from app.activity_rules import ADMIN
from app.feed import FeedService

from .conftest import years_ago

NOW = datetime.datetime(2026, 11, 1, 12)
_ids = itertools.count(1)


def at(day, hour=18):
    return datetime.datetime(2026, 12, day, hour)


@pytest.fixture
def university(db):
    return db.session.execute(
        db.select(model.University).where(
            model.University.name == "Case Western Reserve University")
    ).scalar_one()


@pytest.fixture
def types(db):
    rows = {name: model.ActivityType(name=name) for name in ("Hiking", "Concert", "Dining")}
    db.session.add_all(rows.values())
    db.session.commit()
    return rows


@pytest.fixture
def make_user(db, university):
    def _make(**overrides):
        n = next(_ids)
        fields = dict(
            university_id=university.id, auth_provider_id=f"feed_user_{n}",
            email=f"feed{n}@case.edu", first_name=f"User{n}", last_name="Test",
            birthday=datetime.date.fromisoformat(years_ago(20, days=-30)),
            gender="Woman", school_year="Junior",
        )
        fields.update(overrides)
        user = model.User(**fields)
        db.session.add(user)
        db.session.commit()
        return user
    return _make


@pytest.fixture
def make_activity(db, university, make_user):
    def _make(creator=None, start_at=None, destinations=(), **overrides):
        creator = creator or make_user()
        start_at = start_at or at(1)
        fields = dict(
            university_id=university.id, creator_id=creator.id, title="Plan",
            description="Come along.", meeting_point="Thwing Center",
            start_at=start_at, end_at=start_at + datetime.timedelta(hours=3),
            max_attendees=5, status=model.STATUS_OPEN,
        )
        fields.update(overrides)
        activity = model.Activity(**fields)
        db.session.add(activity)
        for position, text in enumerate(destinations):
            activity.destinations.append(
                model.ActivityDestination(position=position, text=text))
        db.session.flush()
        MembershipManager.add(activity, creator.id, role=ADMIN)
        db.session.commit()
        return activity
    return _make


def follow(follower, followee, status=model.FOLLOW_ACCEPTED):
    db.session.add(model.Follow(follower_id=follower.id, followee_id=followee.id,
                                status=status))
    db.session.commit()


def join(activity, user):
    MembershipManager.add(activity, user.id)
    db.session.commit()


def feed_ids(user, **kwargs):
    return [item.activity.id for item in FeedService.for_user(user, now=NOW, **kwargs)]


# ------------------------------------------------------------------ candidates

def test_cold_start_lists_soonest_first(make_user, make_activity):
    viewer = make_user()
    later = make_activity(start_at=at(10))
    sooner = make_activity(start_at=at(2))
    assert feed_ids(viewer) == [sooner.id, later.id]


def test_excludes_own_full_past_closed_and_other_university(
        db, make_user, make_activity):
    viewer = make_user()
    shown = make_activity()
    make_activity(creator=viewer)                                   # own
    full = make_activity(max_attendees=1)                           # full
    make_activity(start_at=datetime.datetime(2026, 10, 1, 18))      # past
    make_activity(status=model.STATUS_CLOSED)                       # closed
    other_uni = db.session.execute(
        db.select(model.University).where(
            model.University.id != viewer.university_id).limit(1)).scalar_one()
    stranger = make_user(university_id=other_uni.id)
    make_activity(creator=stranger, university_id=other_uni.id)     # elsewhere

    assert full.seats_left == 0
    assert feed_ids(viewer) == [shown.id]


def test_excludes_activities_the_viewer_is_not_eligible_for(make_user, make_activity):
    viewer = make_user()                     # 20 years old
    make_activity(min_age=21)
    ok = make_activity(min_age=18)
    assert feed_ids(viewer) == [ok.id]


# ------------------------------------------------------------------ social

def test_followed_creator_ranks_first_with_reason(make_user, make_activity):
    viewer, friend = make_user(), make_user(first_name="Grace", last_name="Hopper")
    follow(viewer, friend)
    stranger_plan = make_activity(start_at=at(1))
    friend_plan = make_activity(creator=friend, start_at=at(20))

    items = FeedService.for_user(viewer, now=NOW)
    assert [i.activity.id for i in items] == [friend_plan.id, stranger_plan.id]
    assert "Created by Grace Hopper" in items[0].reasons


def test_followed_members_raise_rank(make_user, make_activity):
    viewer, friend = make_user(), make_user()
    follow(viewer, friend)
    plain = make_activity(start_at=at(1))
    joined = make_activity(start_at=at(20))
    join(joined, friend)

    assert feed_ids(viewer) == [joined.id, plain.id]


def test_pending_follow_does_not_count(make_user, make_activity):
    viewer, private = make_user(), make_user(account_public=False)
    follow(viewer, private, status=model.FOLLOW_PENDING)
    sooner = make_activity(start_at=at(1))
    theirs = make_activity(creator=private, start_at=at(20))

    assert feed_ids(viewer) == [sooner.id, theirs.id]


# ------------------------------------------------------------------ similarity

def test_same_type_as_history_ranks_higher(make_user, make_activity, types):
    viewer = make_user()
    past = make_activity(type_id=types["Hiking"].id, start_at=at(1, 9))
    join(past, viewer)
    dining = make_activity(type_id=types["Dining"].id, start_at=at(2, 9))
    hiking = make_activity(type_id=types["Hiking"].id, start_at=at(20, 9))

    items = FeedService.for_user(viewer, now=NOW)
    assert [i.activity.id for i in items] == [hiking.id, dining.id]
    assert "You like hiking" in items[0].reasons


def test_familiar_place_ranks_higher(make_user, make_activity):
    viewer = make_user()
    past = make_activity(meeting_point="Tink Hall", destinations=["Cedar Point"])
    join(past, viewer)
    elsewhere = make_activity(meeting_point="Wade Lagoon", start_at=at(2))
    familiar = make_activity(meeting_point="Wade Lagoon",
                             destinations=["cedar point"], start_at=at(20))

    assert feed_ids(viewer) == [familiar.id, elsewhere.id]


def test_time_of_day_breaks_otherwise_equal_scores(make_user, make_activity):
    viewer = make_user()
    join(make_activity(start_at=at(1, 19), meeting_point="A"), viewer)
    morning = make_activity(start_at=at(2, 9), meeting_point="B")
    evening = make_activity(start_at=at(20, 19), meeting_point="C")

    assert feed_ids(viewer) == [evening.id, morning.id]


def test_social_outweighs_a_single_type_match(make_user, make_activity, types):
    viewer, friend = make_user(), make_user()
    follow(viewer, friend)
    join(make_activity(type_id=types["Concert"].id, meeting_point="A"), viewer)
    similar = make_activity(type_id=types["Concert"].id, meeting_point="B", start_at=at(2))
    friends = make_activity(creator=friend, meeting_point="C", start_at=at(20))

    assert feed_ids(viewer) == [friends.id, similar.id]


def test_limit(make_user, make_activity):
    viewer = make_user()
    for day in range(1, 6):
        make_activity(start_at=at(day))
    assert len(feed_ids(viewer, limit=3)) == 3
