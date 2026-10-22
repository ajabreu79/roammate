"""The design doc's EventBus: a transactional outbox (NFR-REL-004).

publish() adds an OutboxEvent row to the caller's transaction, so an event
exists exactly when the change it describes was committed: a rolled-back join
leaves no JoinRequestDecided behind, and a committed one cannot lose its
event. dispatch_pending() delivers committed events to subscribers later, in
its own transactions, so a failing subscriber (an email service outage, say)
never blocks or undoes the publisher.

Delivery is at least once. A subscriber can see the same event twice -- a
crash between the handler finishing and the row being marked processed -- so
handlers should be idempotent, keyed on event.id.

Who publishes what, from the design doc's Notification event table:

  ActivityCreated           Activity       Messaging makes the main topic
  JoinRequestCreated        Participation  in-app to Admin and Editors
  JoinRequestDecided        Participation  in-app and email to the requester
  WaitlistPromoted          Participation  in-app and email
  AdminSuccessionRequired   Participation  in-app prompt to the leaving Admin
  InviteCreated             Participation  email
  MemberLeft                Participation  Messaging drops the user's sockets
  UserMentioned             Messaging      in-app

EmailVerificationRequested and PasswordChangeRequested are in the design's
table too, but Clerk sends both emails itself (FR-AUTH-014), so nothing here
publishes them.

Nothing subscribes yet. An event whose type has no subscriber stays
unprocessed rather than being marked done, so the Notification module will
receive everything published before it existed.
"""

from datetime import timedelta, timezone

from flask import current_app

from . import db
from . import model

ACTIVITY_CREATED = "ActivityCreated"
JOIN_REQUEST_CREATED = "JoinRequestCreated"
JOIN_REQUEST_DECIDED = "JoinRequestDecided"
WAITLIST_PROMOTED = "WaitlistPromoted"
ADMIN_SUCCESSION_REQUIRED = "AdminSuccessionRequired"
INVITE_CREATED = "InviteCreated"
MEMBER_LEFT = "MemberLeft"
USER_MENTIONED = "UserMentioned"

TYPES = (
    ACTIVITY_CREATED, JOIN_REQUEST_CREATED, JOIN_REQUEST_DECIDED, WAITLIST_PROMOTED,
    ADMIN_SUCCESSION_REQUIRED, INVITE_CREATED, MEMBER_LEFT, USER_MENTIONED,
)

# A failed delivery is retried after 30s, then 90s, 210s and 450s after the
# event was created (base * (2**attempts - 1)), and given up after five
# attempts. Given-up rows stay in the table, unprocessed, for someone to look at.
MAX_ATTEMPTS = 5
RETRY_BASE = timedelta(seconds=30)


def _as_utc(moment):
    """SQLite hands timestamps back naive; they were written as UTC."""
    return moment.replace(tzinfo=timezone.utc) if moment.tzinfo is None else moment


class EventBus:
    """publish() inside a transaction; dispatch_pending() after commit."""

    _subscribers = {}

    @staticmethod
    def publish(event_type, **payload):
        """Add an event to the current transaction. Returns the OutboxEvent.

        Never commits: the event commits or rolls back with the change it
        describes. Payload values must be JSON-serialisable, so pass ids, not
        model instances -- and flush first if an id is assigned at insert.
        """
        if event_type not in TYPES:
            raise ValueError(f"Unknown event type: {event_type}")

        event = model.OutboxEvent(type=event_type, payload=payload)
        db.session.add(event)
        return event

    @classmethod
    def subscribe(cls, event_type):
        """Decorator registering handler(event) for one event type.

        The handler gets the OutboxEvent row and runs inside the dispatch
        transaction, so rows it writes (a Notification, say) commit with the
        event being marked processed. Raising marks the attempt as failed.
        """
        if event_type not in TYPES:
            raise ValueError(f"Unknown event type: {event_type}")

        def register(handler):
            cls._subscribers.setdefault(event_type, []).append(handler)
            return handler
        return register

    @classmethod
    def clear_subscribers(cls):
        """Forget every subscriber. For tests."""
        cls._subscribers = {}

    @staticmethod
    def _due(event, now):
        return _as_utc(event.created_at) + RETRY_BASE * (2 ** event.attempts - 1) <= now

    @classmethod
    def dispatch_pending(cls, limit=100):
        """Deliver due, unprocessed events to their subscribers. Returns how
        many were delivered.

        One transaction per event: the row is locked (SKIP LOCKED on
        PostgreSQL, so two dispatchers never deliver the same event at once),
        its handlers run, and it is marked processed. If a handler raises,
        that transaction is rolled back and the failure is counted in a
        fresh one.
        """
        subscribed = [t for t, handlers in cls._subscribers.items() if handlers]
        if not subscribed:
            return 0

        now = model.utcnow()
        candidates = db.session.execute(
            db.select(model.OutboxEvent)
            .where(model.OutboxEvent.processed_at.is_(None),
                   model.OutboxEvent.attempts < MAX_ATTEMPTS,
                   model.OutboxEvent.type.in_(subscribed))
            .order_by(model.OutboxEvent.created_at)
            .limit(limit)
        ).scalars().all()
        due = [event.id for event in candidates if cls._due(event, now)]
        db.session.rollback()

        delivered = 0
        for event_id in due:
            event = db.session.execute(
                db.select(model.OutboxEvent)
                .where(model.OutboxEvent.id == event_id,
                       model.OutboxEvent.processed_at.is_(None))
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if event is None:
                db.session.rollback()        # another dispatcher has it
                continue

            try:
                for handler in cls._subscribers.get(event.type, []):
                    handler(event)
                event.processed_at = model.utcnow()
                db.session.commit()
                delivered += 1
            except Exception as error:
                db.session.rollback()
                event = db.session.get(model.OutboxEvent, event_id)
                event.attempts += 1
                db.session.commit()
                log = (current_app.logger.error if event.attempts >= MAX_ATTEMPTS
                       else current_app.logger.warning)
                log("Event %s (%s) failed, attempt %d of %d: %s",
                    event_id, event.type, event.attempts, MAX_ATTEMPTS, error)

        return delivered
