"""FR-MESS-009: the board's poll endpoint and JSON posting.

What is worth guarding here is who may read the board, since the poll is
the subscription: FR-MESS-004 is re-checked on every request, so a member
who leaves (MemberLeft) stops receiving messages on their next poll. Also
the `after` cursor, and that the JSON path leaves the plain form post alone.
"""

import datetime

import pytest

from app import db, model
from app.activities import MembershipManager
from app.activity_rules import ADMIN
from app.participation import ParticipationService

from .conftest import complete_profile_form


def _me(client):
    """Sign in and finish registration; returns the signed-in user."""
    client.post("/auth/signup2", data=complete_profile_form())
    return db.session.execute(
        db.select(model.User).where(model.User.email == "student@case.edu")
    ).scalar_one()


def _make_user(email, university_id):
    user = model.User(
        auth_provider_id="user_clerk_" + email, email=email,
        first_name="Grace", last_name="Hopper", email_verified=True,
        gender="Woman", school_year="Senior", university_id=university_id,
        birthday=datetime.date(2000, 1, 1), description="Peer account.",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _make_activity(admin, university_id):
    """An open activity with `admin` as its Admin and a Main topic."""
    activity = model.Activity(
        university_id=university_id, creator_id=admin.id, title="Study Session",
        description="Come along.", meeting_point="Thwing Center",
        start_at=datetime.datetime(2026, 12, 1, 18),
        end_at=datetime.datetime(2026, 12, 1, 22),
        max_attendees=5, status=model.STATUS_OPEN,
    )
    db.session.add(activity)
    db.session.flush()
    MembershipManager.add(activity, admin.id, role=ADMIN)
    topic = model.ForumTopic(activity_id=activity.id, name="Main", created_by=admin.id)
    db.session.add(topic)
    db.session.commit()
    return activity, topic


def _post(topic, author, body):
    message = model.Message(topic_id=topic.id, author_id=author.id, body=body)
    db.session.add(message)
    db.session.commit()
    return message


def _poll_url(activity, topic, after=0):
    return f"/activity/{activity.id}/topic/{topic.id}/messages?after={after}"


@pytest.fixture
def me(client):
    return _me(client)


@pytest.fixture
def board(me):
    """Someone else's activity with three messages, and `me` as an attendee.

    The Admin is another user so that `me` leaving does not cancel the
    activity, which is what happens when its last member leaves.
    """
    admin = _make_user("admin@case.edu", me.university_id)
    activity, topic = _make_activity(admin, me.university_id)
    MembershipManager.add(activity, me.id)
    db.session.commit()
    messages = [_post(topic, admin, text) for text in ("one", "two", "three")]
    return activity, topic, messages


def test_a_member_gets_only_the_messages_after_the_cursor(client, board):
    activity, topic, (one, two, three) = board

    body = client.get(_poll_url(activity, topic, after=one.id)).get_json()

    assert [m["id"] for m in body["messages"]] == [two.id, three.id]
    assert body["messages"][0]["body"] == "two"
    assert body["messages"][0]["author_name"] == "Grace Hopper"


def test_after_leaving_the_poll_is_refused(client, board, me):
    activity, topic, _ = board
    assert client.get(_poll_url(activity, topic)).status_code == 200

    errors, _ = ParticipationService.leave(activity.id, me)
    assert errors == []

    assert client.get(_poll_url(activity, topic)).status_code == 403


def test_a_bad_cursor_is_read_as_zero(client, board):
    activity, topic, messages = board

    body = client.get(_poll_url(activity, topic, after="abc")).get_json()

    assert [m["id"] for m in body["messages"]] == [m.id for m in messages]


def test_a_non_member_cannot_poll(client, me):
    admin = _make_user("stranger@case.edu", me.university_id)
    activity, topic = _make_activity(admin, me.university_id)
    _post(topic, admin, "members only")

    response = client.get(_poll_url(activity, topic))

    assert response.status_code == 403
    assert "messages" not in response.get_json()


def test_another_universitys_board_is_not_found(client, me):
    other = db.session.execute(
        db.select(model.University).where(model.University.id != me.university_id).limit(1)
    ).scalar_one()
    admin = _make_user("admin@other.edu", other.id)
    activity, topic = _make_activity(admin, other.id)

    assert client.get(_poll_url(activity, topic)).status_code == 404


def test_a_topic_from_another_activity_is_not_found(client, board, me):
    activity, _, _ = board
    # `me` is a member of both, so only the topic check can refuse this.
    admin = _make_user("other-admin@case.edu", me.university_id)
    other_activity, other_topic = _make_activity(admin, me.university_id)
    MembershipManager.add(other_activity, me.id)
    db.session.commit()
    _post(other_topic, admin, "not on this board")

    assert client.get(_poll_url(activity, other_topic)).status_code == 404


def test_a_json_post_returns_the_message(client, board):
    activity, topic, (_, _, three) = board

    response = client.post(f"/activity/{activity.id}/message",
                           data={"message-content": "hi", "forum_topic": "Main"},
                           headers={"Accept": "application/json"})

    assert response.status_code == 201
    message = response.get_json()
    assert message["body"] == "hi"
    assert message["id"] > three.id
    # The poller sees it exactly as the post returned it.
    polled = client.get(_poll_url(activity, topic, after=three.id)).get_json()
    assert polled["messages"] == [message]


def test_an_empty_json_post_is_a_400(client, board):
    activity, topic, (_, _, three) = board

    response = client.post(f"/activity/{activity.id}/message",
                           data={"message-content": "   ", "forum_topic": "Main"},
                           headers={"Accept": "application/json"})

    assert response.status_code == 400
    assert response.get_json() == {"error": "Message cannot be empty"}
    polled = client.get(_poll_url(activity, topic, after=three.id)).get_json()
    assert polled["messages"] == []


def test_a_plain_form_post_still_redirects(client, board):
    activity, topic, (_, _, three) = board

    response = client.post(f"/activity/{activity.id}/message",
                           data={"message-content": "hi", "forum_topic": "Main"})

    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/activity/{activity.id}/forum/Main")
    polled = client.get(_poll_url(activity, topic, after=three.id)).get_json()
    assert [m["body"] for m in polled["messages"]] == ["hi"]


def test_a_signed_out_poll_gets_a_401_not_the_refresh_page(app, client, board):
    activity, topic, _ = board
    app.config["_SIGNED_IN"]["signed_in"] = False
    # clerk-js's cookie: without the 401, this is what sends a page to
    # /auth/refresh and counts against the refresh limit (FR-AUTH-008).
    client.set_cookie("__client_uat", "1700000000")

    response = client.get(_poll_url(activity, topic))

    assert response.status_code == 401
    assert response.get_json() == {"error": "Not signed in."}
    with client.session_transaction() as session:
        assert "auth_refreshes" not in session
