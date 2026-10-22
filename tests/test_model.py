"""Schema tests: the design doc's tables, and the invariants it relies on.

The point of most of these is that the database refuses the bad state even if
a service forgets to, which is what NFR-REL-005 asks for.
"""

import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from app import create_app, db as _db, model, universities


@pytest.fixture
def app():
    application = create_app({
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "TESTING": True,
    })
    with application.app_context():
        _db.create_all()
        universities.seed()
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def db(app):
    return _db


@pytest.fixture
def university(db):
    return db.session.execute(
        db.select(model.University).where(model.University.name == "Case Western Reserve University")
    ).scalar_one()


def make_user(db, university, email="a@case.edu", sub="u1"):
    user = model.User(
        university_id=university.id, auth_provider_id=sub, email=email,
        first_name="Ada", last_name="Lovelace", birthday=datetime.date(2000, 1, 1),
    )
    db.session.add(user)
    db.session.flush()
    return user


def make_activity(db, university, creator, **overrides):
    fields = dict(
        university_id=university.id, creator_id=creator.id, title="Concert",
        description="Come along.", meeting_point="Thwing Center",
        start_at=datetime.datetime(2026, 12, 1, 18),
        end_at=datetime.datetime(2026, 12, 1, 22),
        max_attendees=4,
    )
    fields.update(overrides)
    activity = model.Activity(**fields)
    db.session.add(activity)
    db.session.flush()
    return activity


# --- the design's table list -------------------------------------------------

def test_all_twenty_one_design_tables_exist(db):
    from sqlalchemy import inspect

    expected = {
        "universities", "users", "user_preferences", "external_links", "follows",
        "activity_types", "activities", "activity_destinations",
        "activity_memberships", "finalized_fields", "join_requests",
        "waitlist_entries", "invitations", "saved_activities", "forum_topics",
        "messages", "message_mentions", "notifications", "outbox_events",
        "verification_tokens", "audit_logs",
    }
    assert len(expected) == 21
    assert expected <= set(inspect(db.engine).get_table_names())


def test_the_user_table_has_no_password_column(db):
    """FR-AUTH-002, and the design doc states it explicitly."""
    assert "password" not in model.User.__table__.columns
    assert "password_hash" not in model.User.__table__.columns


def test_age_is_derived_not_stored(db):
    """FR-PROF-003."""
    assert "age" not in model.User.__table__.columns


def test_waitlist_position_is_not_stored(db):
    """The design computes position from created_at so removals never renumber."""
    assert "position" not in model.WaitlistEntry.__table__.columns


# --- FR-ACE-007: exactly one Admin -------------------------------------------

def test_one_admin_is_accepted(db, university):
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.commit()


def test_a_second_admin_is_rejected_by_the_database(db, university):
    creator = make_user(db, university)
    other = make_user(db, university, "b@case.edu", "u2")
    activity = make_activity(db, university, creator)

    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.commit()

    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=other.id, role=model.ROLE_ADMIN))
    with pytest.raises(IntegrityError):
        db.session.commit()


def test_an_admin_and_an_editor_coexist(db, university):
    creator = make_user(db, university)
    other = make_user(db, university, "b@case.edu", "u2")
    activity = make_activity(db, university, creator)

    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=other.id, role=model.ROLE_EDITOR))
    db.session.commit()


def test_two_activities_may_each_have_an_admin(db, university):
    """The unique index is per activity, not global."""
    creator = make_user(db, university)
    for _ in range(2):
        activity = make_activity(db, university, creator)
        db.session.add(model.ActivityMembership(
            activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.commit()


def test_an_unknown_role_is_rejected(db, university):
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    with pytest.raises(IntegrityError):
        db.session.add(model.ActivityMembership(
            activity_id=activity.id, user_id=creator.id, role="OWNER"))
        db.session.commit()


# --- FR-ACE-004 invariants, enforced in the schema ---------------------------

@pytest.mark.parametrize("overrides, label", [
    (dict(start_at=datetime.datetime(2026, 12, 2, 18),
          end_at=datetime.datetime(2026, 12, 1, 18)), "end before start"),
    (dict(budget=-5), "negative budget"),
    (dict(max_attendees=0), "zero max attendees"),
    (dict(min_age=30, max_age=20), "min age above max age"),
    (dict(status="SORT_OF_OPEN"), "unknown status"),
    (dict(attendee_count=5, max_attendees=4), "attendee count over capacity"),
    (dict(attendee_count=-1), "negative attendee count"),
])
def test_activity_constraints(db, university, overrides, label):
    creator = make_user(db, university)
    # The constraint fires on flush, so the write itself is what must raise.
    with pytest.raises(IntegrityError):
        make_activity(db, university, creator, **overrides)
        db.session.commit()


def test_a_new_activity_defaults_to_open(db, university):
    """FR-ACE-002."""
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    db.session.commit()
    assert activity.status == model.STATUS_OPEN
    assert activity.attendee_count == 0


def test_a_zero_budget_is_allowed(db, university):
    creator = make_user(db, university)
    make_activity(db, university, creator, budget=0)
    db.session.commit()


# --- FR-PROF-005 -------------------------------------------------------------

def test_a_self_follow_is_rejected(db, university):
    user = make_user(db, university)
    with pytest.raises(IntegrityError):
        db.session.add(model.Follow(follower_id=user.id, followee_id=user.id))
        db.session.commit()


def test_an_unknown_follow_status_is_rejected(db, university):
    a = make_user(db, university)
    b = make_user(db, university, "b@case.edu", "u2")
    with pytest.raises(IntegrityError):
        db.session.add(model.Follow(follower_id=a.id, followee_id=b.id, status="MAYBE"))
        db.session.commit()


# --- FR-AUTH-004, 006, FR-MESS-002 lengths and uniqueness --------------------

def test_a_duplicate_email_is_rejected(db, university):
    make_user(db, university, "dup@case.edu", "u1")
    with pytest.raises(IntegrityError):
        make_user(db, university, "dup@case.edu", "u2")
        db.session.commit()


def test_a_duplicate_provider_id_is_rejected(db, university):
    make_user(db, university, "a@case.edu", "same")
    with pytest.raises(IntegrityError):
        make_user(db, university, "b@case.edu", "same")
        db.session.commit()


def test_a_description_over_five_hundred_characters_is_rejected(db, university):
    """FR-AUTH-006, enforced in the schema as well as the validator."""
    user = make_user(db, university)
    with pytest.raises(IntegrityError):
        user.description = "x" * 501
        db.session.commit()


def test_a_message_body_must_be_one_to_five_hundred_characters(db, university):
    """FR-MESS-002."""
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    topic = model.ForumTopic(activity_id=activity.id, name="Main", created_by=creator.id)
    db.session.add(topic)
    db.session.flush()

    db.session.add(model.Message(topic_id=topic.id, author_id=creator.id, body="x" * 501))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()

    db.session.add(model.Message(topic_id=topic.id, author_id=creator.id, body=""))
    with pytest.raises(IntegrityError):
        db.session.commit()


def test_an_unfinalizable_field_name_is_rejected(db, university):
    """FR-ACE-008: field_name is limited to the requirement's list."""
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    with pytest.raises(IntegrityError):
        db.session.add(model.FinalizedField(activity_id=activity.id, field_name="title"))
        db.session.commit()


def test_one_pending_join_request_per_user_per_activity(db, university):
    """FR-PE-002, via the partial unique index."""
    creator = make_user(db, university)
    other = make_user(db, university, "b@case.edu", "u2")
    activity = make_activity(db, university, creator)

    db.session.add(model.JoinRequest(activity_id=activity.id, user_id=other.id))
    db.session.commit()

    db.session.add(model.JoinRequest(activity_id=activity.id, user_id=other.id))
    with pytest.raises(IntegrityError):
        db.session.commit()


def test_a_decided_request_does_not_block_a_new_one(db, university):
    """The index is partial, so history does not prevent re-applying."""
    creator = make_user(db, university)
    other = make_user(db, university, "b@case.edu", "u2")
    activity = make_activity(db, university, creator)

    denied = model.JoinRequest(activity_id=activity.id, user_id=other.id,
                               status=model.REQUEST_DENIED)
    db.session.add(denied)
    db.session.commit()

    db.session.add(model.JoinRequest(activity_id=activity.id, user_id=other.id))
    db.session.commit()


# --- FR-AUTH-017 deletion cascades -------------------------------------------

def test_deleting_a_user_keeps_their_messages_under_the_placeholder(db, university):
    """The design's retention rule: author_id goes null, the message stays."""
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    topic = model.ForumTopic(activity_id=activity.id, name="Main", created_by=creator.id)
    db.session.add(topic)
    db.session.flush()
    db.session.add(model.Message(topic_id=topic.id, author_id=creator.id, body="Hello"))
    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.commit()

    db.session.delete(creator)
    db.session.commit()

    message = db.session.execute(db.select(model.Message)).scalar_one()
    assert message.body == "Hello"
    assert message.author_id is None
    assert message.author_name == model.DELETED_USER_NAME
    # The membership is removed with the user.
    assert db.session.execute(db.select(model.ActivityMembership)).scalars().all() == []


def test_deleting_a_user_removes_their_follows_and_notifications(db, university):
    a = make_user(db, university)
    b = make_user(db, university, "b@case.edu", "u2")
    db.session.add(model.Follow(follower_id=a.id, followee_id=b.id))
    db.session.add(model.Notification(user_id=a.id, type="TEST"))
    db.session.add(model.UserPreference(user_id=a.id, min_age=18))
    db.session.commit()

    db.session.delete(a)
    db.session.commit()

    assert db.session.execute(db.select(model.Follow)).scalars().all() == []
    assert db.session.execute(db.select(model.Notification)).scalars().all() == []
    assert db.session.execute(db.select(model.UserPreference)).scalars().all() == []


# --- FR-AUTH-012, FR-ACE-001 defaults ----------------------------------------

def test_a_user_with_no_photo_key_gets_the_default(db, university):
    user = make_user(db, university)
    assert user.profile_photo_key is None
    assert user.profile_photo == model.DEFAULT_PROFILE_PIC


def test_an_activity_with_no_image_key_gets_the_placeholder(db, university):
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    assert activity.image == model.DEFAULT_ACTIVITY_IMAGE


# --- FR-ACE-001 destinations -------------------------------------------------

def test_destinations_are_ordered_rows_so_commas_survive(db, university):
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    activity.destinations.append(model.ActivityDestination(position=0, text="Cleveland, OH"))
    activity.destinations.append(model.ActivityDestination(position=1, text="Akron"))
    db.session.commit()

    assert [d.text for d in activity.destinations] == ["Cleveland, OH", "Akron"]


def test_two_destinations_cannot_share_a_position(db, university):
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    with pytest.raises(IntegrityError):
        activity.destinations.append(model.ActivityDestination(position=0, text="A"))
        activity.destinations.append(model.ActivityDestination(position=0, text="B"))
        db.session.commit()


# --- FR-AUTH-013 -------------------------------------------------------------

def test_universities_are_seeded_and_searchable(db):
    from app import universities

    assert universities.get_by_name("Case Western Reserve University") is not None
    assert universities.is_known("Totally Real University") is False
    assert [row["name"] for row in universities.search(query="case")] == \
        ["Case Western Reserve University"]
    assert {row["country"] for row in universities.search(country="Japan")} == {"Japan"}
    assert "United States" in universities.countries()


def test_seeding_twice_adds_nothing(db):
    from app import universities

    assert universities.seed() == 0


def test_an_activity_survives_its_creators_deletion(db, university):
    """The design's NOT NULL creator_id cannot coexist with its own retention
    rule that deletion removes the profile row. See the comment on
    Activity.creator_id."""
    creator = make_user(db, university)
    activity = make_activity(db, university, creator)
    db.session.add(model.ActivityMembership(
        activity_id=activity.id, user_id=creator.id, role=model.ROLE_ADMIN))
    db.session.commit()

    db.session.delete(creator)
    db.session.commit()

    activity = db.session.execute(db.select(model.Activity)).scalar_one()
    assert activity.creator_id is None
    assert activity.creator_name == model.DELETED_USER_NAME
