"""FR-PROF-005: the follower and following lists behind the profile counts.

The counts were the only thing the profile exposed; the requirement's "access
their own (or others') following and follower lists" needs the names too.
They are served as JSON (main.user_followers / main.user_following) because
the page opens them in a modal rather than navigating.

What is worth guarding here is not the JSON shape but the three rules that
decide who may read it: the university boundary (FR-PROF-009), PENDING
follows staying out of the list exactly as they stay out of the count, and a
private account's graph being visible only to the owner and to accepted
followers.
"""

import datetime

import pytest

from app import db, model

from .conftest import complete_profile_form


def _make_user(email, first_name, university_id, **kwargs):
    user = model.User(
        auth_provider_id="user_clerk_" + email, email=email,
        first_name=first_name, last_name="Hopper", email_verified=True,
        gender="Woman", school_year="Senior", university_id=university_id,
        birthday=datetime.date(2000, 1, 1), description="Peer account.",
        **kwargs
    )
    db.session.add(user)
    db.session.commit()
    return user


@pytest.fixture
def university(app):
    return db.session.execute(db.select(model.University).limit(1)).scalar_one()


@pytest.fixture
def peer(university):
    return _make_user("peer@case.edu", "Grace", university.id)


def _me(client):
    """Sign in and finish registration; returns the signed-in user."""
    client.post("/auth/signup2", data=complete_profile_form())
    return db.session.execute(
        db.select(model.User).where(model.User.email == "student@case.edu")
    ).scalar_one()


def test_a_follower_appears_in_the_followers_list(client, peer):
    me = _me(client)
    client.post(f"/follow/{peer.id}")

    body = client.get(f"/profile/{peer.id}/followers").get_json()

    assert [u["id"] for u in body["users"]] == [me.id]
    assert body["users"][0]["name"] == me.name


def test_the_following_list_is_the_other_direction(client, peer):
    me = _me(client)
    client.post(f"/follow/{peer.id}")

    assert client.get(f"/profile/{peer.id}/following").get_json()["users"] == []
    mine = client.get(f"/profile/{me.id}/following").get_json()["users"]
    assert [u["id"] for u in mine] == [peer.id]


def test_an_empty_list_is_an_empty_list_not_a_404(client, peer):
    _me(client)
    response = client.get(f"/profile/{peer.id}/followers")
    assert response.status_code == 200
    assert response.get_json()["users"] == []


def test_a_pending_follow_is_not_listed(client, peer):
    """It is not counted either (test_profile_page); the two must agree."""
    me = _me(client)
    db.session.add(model.Follow(follower_id=me.id, followee_id=peer.id,
                                status=model.FOLLOW_PENDING))
    db.session.commit()

    assert client.get(f"/profile/{peer.id}/followers").get_json()["users"] == []


def test_the_list_carries_no_email_or_birthday(client, peer):
    """NFR-SECU-009: only what the profile card already shows."""
    _me(client)
    client.post(f"/follow/{peer.id}")
    body = client.get(f"/profile/{peer.id}/followers").get_data(as_text=True)

    assert "student@case.edu" not in body
    assert "2000-01-01" not in body


def test_another_universitys_lists_are_not_reachable(client, app):
    """FR-PROF-009: the same boundary the profile itself applies."""
    _me(client)
    other = model.University(name="Elsewhere Tech", country="US")
    db.session.add(other)
    db.session.commit()
    stranger = _make_user("stranger@elsewhere.edu", "Ida", other.id)

    assert client.get(f"/profile/{stranger.id}/followers").status_code == 404
    assert client.get(f"/profile/{stranger.id}/following").status_code == 404


def test_a_private_accounts_lists_are_closed_to_a_stranger(client, university):
    _me(client)
    private = _make_user("private@case.edu", "Hedy", university.id,
                         account_public=False)

    response = client.get(f"/profile/{private.id}/followers")

    assert response.status_code == 403
    assert response.get_json() == {"private": True, "users": []}


def test_a_private_accounts_lists_open_to_an_accepted_follower(client, university):
    me = _me(client)
    private = _make_user("private@case.edu", "Hedy", university.id,
                         account_public=False)
    db.session.add(model.Follow(follower_id=me.id, followee_id=private.id,
                                status=model.FOLLOW_ACCEPTED))
    db.session.commit()

    response = client.get(f"/profile/{private.id}/followers")

    assert response.status_code == 200
    assert [u["id"] for u in response.get_json()["users"]] == [me.id]


def test_a_pending_follower_of_a_private_account_is_still_a_stranger(
        client, university):
    """What a PENDING follow is for: it does not grant access yet."""
    me = _me(client)
    private = _make_user("private@case.edu", "Hedy", university.id,
                         account_public=False)
    db.session.add(model.Follow(follower_id=me.id, followee_id=private.id,
                                status=model.FOLLOW_PENDING))
    db.session.commit()

    assert client.get(f"/profile/{private.id}/followers").status_code == 403


def test_your_own_lists_are_yours_even_when_private(client):
    me = _me(client)
    me.account_public = False
    db.session.commit()

    assert client.get(f"/profile/{me.id}/following").status_code == 200


def test_the_profile_renders_the_counts_as_modal_buttons(client, peer):
    """The page must reach the new routes, and carry the modal to fill."""
    _me(client)
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)

    assert f'data-url="/profile/{peer.id}/followers"' in body
    assert f'data-url="/profile/{peer.id}/following"' in body
    assert 'id="follow-list-modal"' in body
    assert "js/follow_list.js" in body


def test_the_lists_need_a_session(client, app, peer):
    app.config["_SIGNED_IN"]["signed_in"] = False
    assert client.get(f"/profile/{peer.id}/followers").status_code in (302, 401)
