"""Discover module: FeedService, the personalized "For you" feed.

REQUIREMENTS TRACEABILITY
FR-PROF-006 activities connected to people the viewer follows are
prioritized; FR-AD-001 the home feed. Closes the FR-PROF-006 GAP on
main.index.

Read-only, so none of the (errors, result) or commit conventions of
activities.py apply: for_user() returns a ranked list and never writes.

RANKING. Every candidate gets a score from two families of signals, and
each signal that fires adds a human-readable reason for the card.

  Social (who you follow, ACCEPTED follows only):
    created by someone you follow             +3.0
    each followee who is a member             +1.5, counting at most 3

  Similarity to the activities you belong to (your history):
    same activity type                        +2.0 x share of history
    same time of day (morning/afternoon/...)  +1.0 x share of history
    a place you have been (meeting point or   +1.0
    destination, compared case-insensitively)

Ties, including the cold-start case where every score is 0, go to the
activity that starts soonest, so the feed is never empty for a new user.

Candidates are what browse_activities would show: same university, OPEN or
APPROVAL_REQUIRED, not full, not started, the viewer not already a member,
and the viewer eligible (age, year, gender).
"""

from collections import Counter, namedtuple

from sqlalchemy.orm import selectinload

from . import db
from . import model
from .participation import _eligibility

FeedItem = namedtuple("FeedItem", "activity score reasons")

CREATOR_FOLLOWED = 3.0
MEMBER_FOLLOWED = 1.5
MAX_FOLLOWED_MEMBERS = 3
SAME_TYPE = 2.0
SAME_TIME_OF_DAY = 1.0
SAME_PLACE = 1.0


def _time_of_day(when):
    hour = when.hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    if 17 <= hour < 22:
        return "evening"
    return "night"


def _places(activity):
    """Meeting point and destinations, normalized for comparison."""
    texts = [activity.meeting_point] + [d.text for d in activity.destinations]
    return {t.strip().lower() for t in texts if t and t.strip()}


def _naive(value):
    """SQLite returns naive datetimes, PostgreSQL aware ones."""
    return value.replace(tzinfo=None) if value is not None else None


class FeedService:

    @staticmethod
    def _followee_names(user):
        rows = db.session.execute(
            db.select(model.User)
            .join(model.Follow, model.Follow.followee_id == model.User.id)
            .where(model.Follow.follower_id == user.id,
                   model.Follow.status == model.FOLLOW_ACCEPTED)
        ).scalars().all()
        return {u.id: u.name for u in rows}

    @staticmethod
    def _history(user):
        """Activities the user belongs to, past and present."""
        return db.session.execute(
            db.select(model.Activity)
            .join(model.ActivityMembership)
            .where(model.ActivityMembership.user_id == user.id)
            .options(selectinload(model.Activity.destinations))
        ).scalars().all()

    @staticmethod
    def _candidates(user, now):
        query = (
            db.select(model.Activity)
            .where(
                model.Activity.university_id == user.university_id,
                model.Activity.status.in_(
                    [model.STATUS_OPEN, model.STATUS_APPROVAL_REQUIRED]),
                model.Activity.attendee_count < model.Activity.max_attendees,
                model.Activity.start_at > now,
                ~model.Activity.memberships.any(
                    model.ActivityMembership.user_id == user.id),
            )
            .options(selectinload(model.Activity.memberships),
                     selectinload(model.Activity.destinations))
        )
        activities = db.session.execute(query).scalars().all()
        return [a for a in activities if not _eligibility(a, user)]

    @staticmethod
    def for_user(user, limit=20, now=None):
        """The viewer's ranked feed: a list of FeedItem, best first."""
        if user is None or user.university_id is None:
            return []
        now = now or model.utcnow()

        followees = FeedService._followee_names(user)
        history = FeedService._history(user)
        type_counts = Counter(a.type_id for a in history if a.type_id is not None)
        time_counts = Counter(_time_of_day(a.start_at) for a in history)
        past_places = set().union(*(_places(a) for a in history)) if history else set()

        items = []
        for activity in FeedService._candidates(user, now):
            score, reasons = 0.0, []

            if activity.creator_id in followees:
                score += CREATOR_FOLLOWED
                reasons.append(f"Created by {followees[activity.creator_id]}")

            going = [followees[m.user_id] for m in activity.memberships
                     if m.user_id in followees and m.user_id != activity.creator_id]
            if going:
                score += MEMBER_FOLLOWED * min(len(going), MAX_FOLLOWED_MEMBERS)
                others = len(going) - 1
                reasons.append(f"{going[0]} is going" if not others
                               else f"{going[0]} and {others} more you follow are going")

            if history:
                if activity.type_id in type_counts:
                    score += SAME_TYPE * type_counts[activity.type_id] / len(history)
                    reasons.append(f"You like {activity.activity_type.name.lower()}")
                bucket = _time_of_day(activity.start_at)
                if bucket in time_counts:
                    score += SAME_TIME_OF_DAY * time_counts[bucket] / len(history)
                if _places(activity) & past_places:
                    score += SAME_PLACE
                    reasons.append("Somewhere you've been before")

            items.append(FeedItem(activity, round(score, 3), reasons))

        items.sort(key=lambda i: (-i.score, _naive(i.activity.start_at), i.activity.id))
        return items[:limit]
