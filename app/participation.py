"""Participation module: ParticipationService, WaitlistService, SuccessionService.

REQUIREMENTS TRACEABILITY
FR-PE-001 to 010 (joining, requests, eligibility, leaving, waitlists),
FR-ACE-007 admin succession, and the participation half of NFR-REL-005:
every join, leave, approval and promotion runs in one transaction that first
locks the activity row, so two users cannot take the last seat.

CONVENTIONS are those of app/activities.py: static methods returning
(errors, result), one commit per public method, rollback on any error.
Membership rows and attendee_count are only ever written through
MembershipManager. The rollback and commit helpers are ActivityService's, so
both modules report a lost race the same way (activities.CONFLICT).

leave_all() is the exception to one-commit-per-method: account deletion
calls it and commits afterwards, once the Clerk identity is gone.

Events go to the outbox (app/events.py) in the same transaction as the change
they describe: JoinRequestCreated, JoinRequestDecided, WaitlistPromoted and
MemberLeft.

DEVIATION: AdminSuccessionRequired is not published. The design sends it as
an in-app prompt when an Admin tries to leave without a successor, but that
leave is refused and rolled back, taking the outbox row with it. The refusal
itself is the prompt: leave() returns it to the Admin, who is right there.

Not here yet:
  GAP (FR-PE-011): invitations. They need the email half of the
    Notification module.
"""

from collections import namedtuple

from . import db
from . import events
from . import model
from .activities import NOT_FOUND, ActivityService, MembershipManager
from .events import EventBus
from .activity_rules import (
    ADMIN, APPROVAL_REQUIRED, CANCELLED,
    ActivityStatusMachine, EligibilityChecker, years_between,
)

JOINED = "JOINED"
WAITLISTED = "WAITLISTED"

# leave()'s result: whether the activity was cancelled because nobody was
# left, and who was promoted from the waitlist into the freed seat.
LeaveResult = namedtuple("LeaveResult", "cancelled promoted")

_fail = ActivityService._fail
_commit = ActivityService._commit


def _membership(activity, user_id):
    return next((m for m in activity.memberships if m.user_id == user_id), None)


def _waitlist_entry(activity, user_id):
    return next((e for e in activity.waitlist if e.user_id == user_id), None)


def _eligibility(activity, user, capacity=True):
    """FR-PE-003 to 009 and FR-PROF-009 for one user. Returns every failure.

    capacity=False leaves out the seat check, so a join can tell "full but
    otherwise eligible" (the waitlist) from "not eligible".
    """
    return EligibilityChecker.check(
        status=activity.status,
        already_member=_membership(activity, user.id) is not None,
        attendee_count=activity.attendee_count,
        max_members=activity.max_attendees if capacity else None,
        viewer_age=user.age,
        min_age=activity.min_age,
        max_age=activity.max_age,
        viewer_school_year=user.school_year,
        allowed_years=years_between(activity.min_year, activity.max_year),
        viewer_gender=user.gender,
        allowed_genders=([g.strip() for g in activity.allowed_genders.split(",") if g.strip()]
                         if activity.allowed_genders else None),
        viewer_university=user.university_id,
        activity_university=activity.university_id,
    )


class WaitlistService:
    """FR-PE-010: first come, first served, ordered by created_at."""

    @staticmethod
    def promote(activity):
        """Fill free seats from the waitlist. Returns the promoted user ids.

        Never commits; the caller holds the activity's row lock. Users who
        are no longer eligible are skipped and keep their place, since an
        edit to the activity can make them eligible again. Anyone already a
        member is dropped from the queue.
        """
        promoted = []
        queue = sorted(activity.waitlist, key=lambda e: (e.created_at, e.user_id))
        for entry in queue:
            if activity.seats_left <= 0:
                break
            if _membership(activity, entry.user_id) is not None:
                activity.waitlist.remove(entry)
                continue
            if _eligibility(activity, entry.user, capacity=False):
                continue
            activity.waitlist.remove(entry)
            MembershipManager.add(activity, entry.user_id)
            promoted.append(entry.user_id)
            EventBus.publish(events.WAITLIST_PROMOTED,
                             activity_id=activity.id, user_id=entry.user_id)
        return promoted


class SuccessionService:
    """FR-ACE-007: who becomes Admin when the Admin leaves."""

    @staticmethod
    def automatic(activity, leaving_user_id):
        """The member who joined earliest, or None if nobody else is left.

        Used when the Admin cannot be asked, i.e. on account deletion. A
        voluntary leave names its successor instead.
        """
        others = MembershipManager.others(activity, leaving_user_id)
        return others[0] if others else None


class ParticipationService:
    """Join, request, approve, leave and the waitlist, each under a row lock."""

    # --- FR-PE-001 joining ---------------------------------------------------

    @staticmethod
    def join(activity_id, user):
        """Join an OPEN activity, or its waitlist when full. Returns (errors, outcome).

        `outcome` is JOINED or WAITLISTED. An APPROVAL_REQUIRED activity is
        refused here; request_to_join is the way in.
        """
        activity = ActivityService.get_visible(activity_id, user, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        errors = _eligibility(activity, user, capacity=False)
        if not errors and not ActivityStatusMachine.accepts_direct_join(activity.status):
            errors = ["This activity needs approval. Send a request to join instead."]

        on_waitlist = _waitlist_entry(activity, user.id)
        full = activity.seats_left <= 0
        if not errors and full and on_waitlist:
            errors = ["You are already on the waitlist for this activity."]
        if errors:
            return _fail(errors)

        if full:
            activity.waitlist.append(model.WaitlistEntry(user_id=user.id))
            outcome = WAITLISTED
        else:
            # A free seat with a queue means nobody queued is eligible for
            # it, so joining directly does not jump anyone.
            if on_waitlist:
                activity.waitlist.remove(on_waitlist)
            MembershipManager.add(activity, user.id)
            outcome = JOINED

        errors = _commit()
        if errors:
            return errors, None
        return [], outcome

    @staticmethod
    def leave_waitlist(activity_id, user):
        """Give up a place on the waitlist. Returns (errors, None)."""
        activity = ActivityService.get_visible(activity_id, user, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        entry = _waitlist_entry(activity, user.id)
        if entry is None:
            return _fail(["You are not on the waitlist for this activity."])

        activity.waitlist.remove(entry)
        return _commit(), None

    @staticmethod
    def fill_seats(activity_id, actor):
        """Promote from the waitlist after seats were added. Returns (errors, promoted).

        For the controller to call when ActivityService.update reports
        seats_added, since activities.py cannot call this module.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        promoted = WaitlistService.promote(activity)
        errors = _commit()
        if errors:
            return errors, None
        return [], promoted

    # --- FR-PE-002 requests --------------------------------------------------

    @staticmethod
    def request_to_join(activity_id, user, message=None):
        """Ask to join an APPROVAL_REQUIRED activity. Returns (errors, join_request)."""
        activity = ActivityService.get_visible(activity_id, user, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        errors = _eligibility(activity, user)
        if not errors and activity.status != APPROVAL_REQUIRED:
            errors = ["This activity is open. Join it directly instead."]

        pending = db.session.execute(
            db.select(model.JoinRequest).where(
                model.JoinRequest.activity_id == activity.id,
                model.JoinRequest.user_id == user.id,
                model.JoinRequest.status == model.REQUEST_PENDING)
        ).scalar_one_or_none()
        if pending is not None:
            errors.append("You already have a pending request for this activity.")

        message = (message or "").strip() or None
        if message and len(message) > model.MAX_DESCRIPTION_LENGTH:
            errors.append(
                f"Your message must be {model.MAX_DESCRIPTION_LENGTH} characters or fewer.")

        if errors:
            return _fail(errors)

        join_request = model.JoinRequest(
            activity_id=activity.id, user_id=user.id, message=message)
        db.session.add(join_request)
        # The id is assigned at insert, and the event needs it. The Admin and
        # Editors are looked up at delivery, so a role change in between is
        # respected.
        db.session.flush()
        EventBus.publish(events.JOIN_REQUEST_CREATED, request_id=join_request.id,
                         activity_id=activity.id, user_id=user.id)

        errors = _commit()
        if errors:
            return errors, None
        return [], join_request

    @staticmethod
    def withdraw_request(activity_id, user):
        """Withdraw one's own pending request. Returns (errors, None)."""
        activity = ActivityService.get_visible(activity_id, user, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        pending = db.session.execute(
            db.select(model.JoinRequest).where(
                model.JoinRequest.activity_id == activity.id,
                model.JoinRequest.user_id == user.id,
                model.JoinRequest.status == model.REQUEST_PENDING)
        ).scalar_one_or_none()
        if pending is None:
            return _fail(["You have no pending request for this activity."])

        db.session.delete(pending)
        return _commit(), None

    @staticmethod
    def decide_request(activity_id, actor, request_id, approve):
        """Approve or deny a pending request. Returns (errors, join_request).

        Requires review_requests. Approval re-runs eligibility under the
        lock, because seats or the requester may have changed while the
        request waited. A full activity refuses the approval.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        errors = ActivityService._require(activity, actor, "review_requests")
        if errors:
            return _fail(errors)

        join_request = db.session.get(model.JoinRequest, request_id)
        if join_request is None or join_request.activity_id != activity.id:
            return _fail(["That join request could not be found."])
        if join_request.status != model.REQUEST_PENDING:
            return _fail(["That join request has already been decided."])

        if approve:
            errors = _eligibility(activity, join_request.user)
            if errors:
                return _fail(errors)
            entry = _waitlist_entry(activity, join_request.user_id)
            if entry is not None:
                activity.waitlist.remove(entry)
            MembershipManager.add(activity, join_request.user_id)
            join_request.status = model.REQUEST_APPROVED
        else:
            join_request.status = model.REQUEST_DENIED

        # FR-PE-002: the row is kept with its decision, so the requester can
        # be told and the decision is auditable.
        join_request.reviewed_by = actor.id
        join_request.decided_at = model.utcnow()
        EventBus.publish(events.JOIN_REQUEST_DECIDED, request_id=join_request.id,
                         activity_id=activity.id, user_id=join_request.user_id,
                         approved=bool(approve), reason="reviewed")

        errors = _commit()
        if errors:
            return errors, None
        return [], join_request

    # --- FR-PE-006 leaving ---------------------------------------------------

    @staticmethod
    def _leave_locked(activity, user, successor_id=None, automatic=False):
        """Remove `user` from a locked activity, without committing.

        Returns (errors, LeaveResult). In order:
          1. An Admin with others remaining hands over first: to
             successor_id, or with automatic=True to the earliest-joined
             member (FR-ACE-007).
          2. The membership is removed.
          3. Nobody left: the activity is cancelled, unless already terminal
             (FR-PE-006). Otherwise the freed seat is filled from the
             waitlist (FR-PE-010).
        """
        membership = _membership(activity, user.id)
        if membership is None:
            return ["You are not a member of this activity."], None

        if membership.role == ADMIN:
            others = MembershipManager.others(activity, user.id)
            if not others:
                MembershipManager.remove_last_admin(activity, user.id)
            else:
                if automatic:
                    successor_id = SuccessionService.automatic(activity, user.id).user_id
                elif successor_id is None:
                    # FR-ACE-007's prompt; see the DEVIATION in the docstring.
                    return ["Choose a member to take over as Admin before you leave."], None
                elif successor_id not in {m.user_id for m in others}:
                    return ["The new Admin must be a member of this activity."], None

                MembershipManager.transfer_admin(activity, user.id, successor_id)
                ActivityService._audit(
                    user, "admin_transferred", "activity_membership", activity.id,
                    {"from": user.id, "to": successor_id,
                     "reason": "account_deleted" if automatic else "left"})
                MembershipManager.remove(activity, user.id)
        else:
            MembershipManager.remove(activity, user.id)

        # Messaging drops the user's subscriptions to this activity's board.
        EventBus.publish(events.MEMBER_LEFT, activity_id=activity.id, user_id=user.id,
                         reason="account_deleted" if automatic else "left")

        if not activity.memberships:
            if ActivityStatusMachine.is_terminal(activity.status):
                return [], LeaveResult(False, [])
            errors = ActivityService.apply_status(activity, user, CANCELLED, system=True)
            if errors:
                return errors, None
            return [], LeaveResult(True, [])

        return [], LeaveResult(False, WaitlistService.promote(activity))

    @staticmethod
    def leave(activity_id, user, successor_id=None):
        """Leave an activity, whatever the role. Returns (errors, LeaveResult).

        An Admin leaving while others remain must name successor_id. The
        confirmation step before this (NFR-REL-002) belongs to the UI.
        """
        activity = ActivityService.get_visible(activity_id, user, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        errors, result = ParticipationService._leave_locked(activity, user, successor_id)
        if errors:
            return _fail(errors)

        errors = _commit()
        if errors:
            return errors, None
        return [], result

    @staticmethod
    def remove_member(activity_id, actor, member_id):
        """Remove another member, Admin only. Returns (errors, promoted).

        Placed here rather than in ActivityService, where the design puts
        DELETE /activities/{id}/members/{userId}, because the freed seat is
        filled from the waitlist.
        """
        activity = ActivityService.get_visible(activity_id, actor, lock=True)
        if activity is None:
            return _fail([NOT_FOUND])

        errors = ActivityService._require(activity, actor, "remove_member")
        if errors:
            return _fail(errors)
        if member_id == actor.id:
            return _fail(["To remove yourself, leave the activity instead."])
        if _membership(activity, member_id) is None:
            return _fail(["That user is not a member of this activity."])

        MembershipManager.remove(activity, member_id)
        EventBus.publish(events.MEMBER_LEFT, activity_id=activity.id, user_id=member_id,
                         reason="removed")
        promoted = WaitlistService.promote(activity)
        ActivityService._audit(actor, "member_removed", "activity_membership", activity.id,
                               {"user_id": member_id})

        errors = _commit()
        if errors:
            return errors, None
        return [], promoted

    @staticmethod
    def leave_all(user):
        """Leave every activity, for account deletion (FR-AUTH-017). Returns errors.

        Does NOT commit: AccountDeletionService commits once the Clerk
        identity is deleted, and rolls back if that fails. Succession is
        automatic, because the user is not there to choose.
        """
        errors = []
        for membership in list(user.memberships):
            activity = ActivityService.get_visible(membership.activity_id, user, lock=True)
            if activity is None:
                continue
            leave_errors, _ = ParticipationService._leave_locked(
                activity, user, automatic=True)
            errors += leave_errors

        # The loop loaded user.memberships, and removing a membership from the
        # activity side does not take it out of this list. Left stale, the
        # user's own delete then tries to blank user_id on rows that are
        # already gone. Flush the removals and forget the list.
        db.session.flush()
        db.session.expire(user, ["memberships"])
        return errors
