"""FR-PROF-001 / FR-PROF-005: viewing another member's profile.

Three defects in main/profile.html made this a 500 for every profile but your
own, and the first one hid the other two:

  1. A Jinja comment opened with `{#` and closed with a bare `}`. Everything
     from there to the next `#}` -- the whole follower/following block -- was
     swallowed as comment text, so the counts never rendered on any profile
     and the two dead endpoints inside it never had a chance to raise.
  2. The follow form posted to `main.toggle_follow`, which does not exist.
     There is no toggle route; `main.follow` and `main.unfollow` are separate.
     Being inside `current_user.id != user.id`, it only raised for other
     people's profiles.
  3. The button read `is_following`, which the view never passes -- it passes
     `follow_button`. Undefined is falsy, so the button always said "Follow".

The existing suite never fetched one user's profile as another user, which is
the only way to reach any of it.
"""

import datetime

import pytest

from app import db, model

from .conftest import complete_profile_form


@pytest.fixture
def peer(app):
    """A second member of the same university, with no Clerk session."""
    university = db.session.execute(
        db.select(model.University).limit(1)).scalar_one()
    user = model.User(
        auth_provider_id="user_clerk_2", email="peer@case.edu",
        first_name="Grace", last_name="Hopper", email_verified=True,
        gender="Woman", school_year="Senior", university_id=university.id,
        birthday=datetime.date(2000, 1, 1),
        description="Peer account.",
    )
    db.session.add(user)
    db.session.commit()
    return user


def _me(client):
    client.post("/auth/signup2", data=complete_profile_form())


def test_another_members_profile_renders(client, peer):
    """The regression: this was a 500."""
    _me(client)
    assert client.get(f"/profile/{peer.id}").status_code == 200


def test_the_follow_button_posts_to_a_real_endpoint(client, peer):
    _me(client)
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert f'action="/follow/{peer.id}"' in body
    assert "toggle_follow" not in body


def test_the_button_flips_to_unfollow_once_following(client, peer):
    _me(client)
    client.post(f"/follow/{peer.id}")
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert f'action="/unfollow/{peer.id}"' in body
    assert "Unfollow" in body


def test_no_follow_button_on_your_own_profile(client, app):
    _me(client)
    user = db.session.execute(db.select(model.User)).scalars().first()
    body = client.get(f"/profile/{user.id}").get_data(as_text=True)
    assert "follow-btn" not in body


def test_the_follower_block_actually_renders(client, peer):
    """Guards defect 1: the broken comment deleted this silently."""
    _me(client)
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert "Followers" in body and "Following" in body


def test_follower_counts_are_real(client, peer):
    _me(client)
    before = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert "<strong>0</strong> Followers" in before
    client.post(f"/follow/{peer.id}")
    after = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert "<strong>1</strong> Followers" in after


def test_a_pending_follow_is_not_counted(client, peer, app):
    """FR-PROF-005: a private account's follow stays PENDING until accepted."""
    _me(client)
    me = db.session.execute(
        db.select(model.User).where(model.User.email == "student@case.edu")
    ).scalar_one()
    db.session.add(model.Follow(follower_id=me.id, followee_id=peer.id,
                                status=model.FOLLOW_PENDING))
    db.session.commit()
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    assert "<strong>0</strong> Followers" in body


def test_no_dead_endpoint_references_remain(client, peer):
    """url_for on a missing endpoint raises, so these must stay out."""
    body = client.get(f"/profile/{peer.id}").get_data(as_text=True)
    for dead in ("user_followers", "user_following", "toggle_follow"):
        assert dead not in body
