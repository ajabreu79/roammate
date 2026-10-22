"""The PostgreSQL schema from the design doc's Data Design section.

All 21 tables, with the constraints the design states. Business invariants the
database can check are enforced here as well as in the services, so a service
bug cannot corrupt a seat count or create a second admin (NFR-REL-005).

Portability notes, since the dev database is SQLite and production is
PostgreSQL:
  - The design specifies UUID ids except on University, ActivityType and
    Message, which are sequential. UUIDs are stored as 36-character strings so
    the same mapping runs on both.
  - Partial unique indexes carry both sqlite_where and postgresql_where.
  - Enumerations are stored as text with a CHECK constraint rather than a
    native enum type, so adding a value is a constraint change in one place
    (NFR-SOFT-009).
  - The design's trigram index on University.name and the functional index on
    lower(email) are PostgreSQL-only; see the comments at those tables.
"""

import uuid
from datetime import date, datetime, timezone

import flask_login
from sqlalchemy import CheckConstraint, Index, UniqueConstraint

from . import db
from . import activity_rules

DEFAULT_PROFILE_PIC = "Defaultpfp.png"
DEFAULT_ACTIVITY_IMAGE = "Defaultactivity.png"
DELETED_USER_NAME = "Deleted user"

MAX_DESCRIPTION_LENGTH = activity_rules.MAX_DESCRIPTION_LENGTH
MAX_MESSAGE_LENGTH = 500

# FR-ACE-007 roles and FR-ACE-009 statuses are defined once, in
# activity_rules, which holds the policy that interprets them. Re-exported
# here so the schema's CHECK constraints and the rules cannot drift apart.
ROLE_ADMIN = activity_rules.ADMIN
ROLE_EDITOR = activity_rules.EDITOR
ROLE_ATTENDEE = activity_rules.ATTENDEE
ROLES = (ROLE_ADMIN, ROLE_EDITOR, ROLE_ATTENDEE)

STATUS_OPEN = activity_rules.OPEN
STATUS_APPROVAL_REQUIRED = activity_rules.APPROVAL_REQUIRED
STATUS_CLOSED = activity_rules.CLOSED
STATUS_FINALIZED = activity_rules.FINALIZED
STATUS_CANCELLED = activity_rules.CANCELLED
STATUSES = (STATUS_OPEN, STATUS_APPROVAL_REQUIRED, STATUS_CLOSED,
            STATUS_FINALIZED, STATUS_CANCELLED)

# FR-PROF-005 follow states.
FOLLOW_PENDING = "PENDING"
FOLLOW_ACCEPTED = "ACCEPTED"
FOLLOW_STATES = (FOLLOW_PENDING, FOLLOW_ACCEPTED)

# FR-PE-002 join request states.
REQUEST_PENDING = "PENDING"
REQUEST_APPROVED = "APPROVED"
REQUEST_DENIED = "DENIED"
REQUEST_STATES = (REQUEST_PENDING, REQUEST_APPROVED, REQUEST_DENIED)

# FR-ACE-008: the fields that may be finalized.
FINALIZABLE_FIELDS = (
    "meeting_point", "destinations", "dates", "budget", "max_attendees",
    "allowed_genders", "activity_type", "age_range", "year_range", "status",
)


def utcnow():
    return datetime.now(timezone.utc)


def new_uuid():
    return str(uuid.uuid4())


def age_from_birthday(birthday, today=None):
    """Derive an age from a birthday (FR-PROF-003).

    The design doc's AgeCalculator: age is computed at request time and never
    stored, so it cannot drift. Shared by the 18+ check (FR-AUTH-005), the
    discovery filter (FR-AD-003) and join eligibility (FR-PE-005).
    """
    if birthday is None:
        return None
    today = today or date.today()
    return today.year - birthday.year - (
        (today.month, today.day) < (birthday.month, birthday.day)
    )


def _uuid_pk():
    return db.Column(db.String(36), primary_key=True, default=new_uuid)


# --- Users and social (design Figure 16) -------------------------------------

class University(db.Model):
    """FR-AUTH-013. Sequential id per the design's table definitions."""

    __tablename__ = "universities"
    __table_args__ = (
        UniqueConstraint("name", "country", name="uq_university_name_country"),
        # PostgreSQL: add a trigram index on name for the searchable dropdown
        #   CREATE INDEX ix_university_name_trgm ON universities
        #     USING gin (name gin_trgm_ops);
        # SQLite has no trigram support, so the dev filter is a LIKE scan.
        Index("ix_university_name", "name"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), nullable=False)
    country = db.Column(db.String(100), nullable=False)

    def __repr__(self):
        return f"<University {self.name}, {self.country}>"


class User(db.Model, flask_login.UserMixin):
    """FR-AUTH-001 to 006, 012; FR-PROF-001 to 004.

    FR-AUTH-002: there is deliberately no password column. Credentials live
    with the external auth provider and never reach this database.
    """

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            f"length(description) <= {MAX_DESCRIPTION_LENGTH}",
            name="ck_user_description_length",
        ),
        # Email is unique case-insensitively. Addresses are normalized to
        # lower case before they are stored (RegistrationValidator), so a
        # plain unique constraint is enough on SQLite. On PostgreSQL add
        #   CREATE UNIQUE INDEX uq_user_email_lower ON users (lower(email));
        # to make it true regardless of the caller.
        Index("ix_user_university", "university_id"),
    )

    id = _uuid_pk()

    # FR-PROF-009: the isolation boundary. Every user belongs to one university.
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), nullable=True)

    # The external provider's user id (Clerk's `sub`). The real identity.
    auth_provider_id = db.Column(db.String(255), unique=True, nullable=False, index=True)

    # FR-AUTH-004: one account per address.
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)

    first_name = db.Column(db.String(80), default="", nullable=False)
    last_name = db.Column(db.String(80), default="", nullable=False)

    # FR-AUTH-005, FR-PROF-003, FR-PROF-004: stored once at registration and
    # never editable; age is derived from it.
    birthday = db.Column(db.Date)

    gender = db.Column(db.String(40))
    school_year = db.Column(db.String(40))

    # FR-AUTH-006
    description = db.Column(db.String(MAX_DESCRIPTION_LENGTH), default="", nullable=False)

    # FR-AUTH-012: null means the system-wide default photo.
    profile_photo_key = db.Column(db.String(255))

    # FR-PROF-005
    account_public = db.Column(db.Boolean, default=True, nullable=False)

    # FR-AUTH-014
    email_verified = db.Column(db.Boolean, default=False, nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    university = db.relationship("University")

    @property
    def name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def age(self):
        """FR-PROF-003: derived, never a column."""
        return age_from_birthday(self.birthday)

    @property
    def profile_photo(self):
        """FR-AUTH-012: the default photo when none was uploaded."""
        return self.profile_photo_key or DEFAULT_PROFILE_PIC

    @property
    def university_name(self):
        return self.university.name if self.university else None

    @property
    def profile_complete(self):
        """Whether step 2 of registration has been answered (FR-AUTH-001)."""
        return all([self.gender, self.school_year, self.university_id, self.birthday])

    def __repr__(self):
        return f"<User {self.id} {self.email}>"


class UserPreference(db.Model):
    """FR-PROF-007. One row per user."""

    __tablename__ = "user_preferences"

    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        primary_key=True)
    preferred_genders = db.Column(db.String(200))
    min_age = db.Column(db.Integer)
    max_age = db.Column(db.Integer)
    min_year = db.Column(db.String(40))
    max_year = db.Column(db.String(40))
    interests = db.Column(db.Text)

    user = db.relationship("User", backref=db.backref("preference", uselist=False, passive_deletes=True))


class ExternalLink(db.Model):
    """FR-PROF-008."""

    __tablename__ = "external_links"
    __table_args__ = (Index("ix_external_link_user", "user_id"),)

    id = _uuid_pk()
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    platform = db.Column(db.String(80), nullable=False)
    url_or_handle = db.Column(db.String(500), nullable=False)

    user = db.relationship("User", backref=db.backref("external_links", passive_deletes=True))


class Follow(db.Model):
    """FR-PROF-005. A self-relationship on User."""

    __tablename__ = "follows"
    __table_args__ = (
        CheckConstraint("follower_id <> followee_id", name="ck_follow_not_self"),
        CheckConstraint(
            "status IN ('" + "', '".join(FOLLOW_STATES) + "')",
            name="ck_follow_status",
        ),
        Index("ix_follow_followee", "followee_id"),
    )

    follower_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                            primary_key=True)
    followee_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                            primary_key=True)
    # A follow on a private account stays PENDING until the owner accepts.
    status = db.Column(db.String(20), default=FOLLOW_ACCEPTED, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    follower = db.relationship("User", foreign_keys=[follower_id],
                               backref=db.backref("following", passive_deletes=True))
    followee = db.relationship("User", foreign_keys=[followee_id],
                               backref=db.backref("followers", passive_deletes=True))


# --- Activities (design Figure 17) -------------------------------------------

class ActivityType(db.Model):
    """FR-ACE-001. A new type is a row, not a schema change (NFR-SOFT-009)."""

    __tablename__ = "activity_types"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)

    def __repr__(self):
        return f"<ActivityType {self.name}>"


class Activity(db.Model):
    """FR-ACE-001 to 004, FR-ACE-009; NFR-PERF-007 indexes."""

    __tablename__ = "activities"
    __table_args__ = (
        CheckConstraint("end_at >= start_at", name="ck_activity_end_after_start"),
        CheckConstraint("budget IS NULL OR budget >= 0", name="ck_activity_budget"),
        CheckConstraint("max_attendees > 0", name="ck_activity_max_attendees"),
        CheckConstraint(
            "attendee_count >= 0 AND attendee_count <= max_attendees",
            name="ck_activity_attendee_count",
        ),
        CheckConstraint("min_age IS NULL OR max_age IS NULL OR min_age <= max_age",
                        name="ck_activity_age_range"),
        CheckConstraint(
            "status IN ('" + "', '".join(STATUSES) + "')",
            name="ck_activity_status",
        ),
        Index("ix_activity_university_status_start", "university_id", "status", "start_at"),
        Index("ix_activity_university_type_start", "university_id", "type_id", "start_at"),
        Index("ix_activity_creator_created", "creator_id", "created_at"),
    )

    id = _uuid_pk()

    # FR-PROF-009 / NFR-SECU-010: every activity traces to one university.
    university_id = db.Column(db.Integer, db.ForeignKey("universities.id"), nullable=False)
    # DEVIATION from the design doc, which has creator_id NOT NULL with no
    # ON DELETE rule. That is impossible alongside its own retention rule that
    # account deletion "removes the user's profile": the row cannot go while a
    # NOT NULL foreign key points at it, so deleting anyone who ever created
    # an activity would fail. Nulled on delete and surfaced as
    # Activity.creator_name, the same treatment the design gives Message.
    creator_id = db.Column(db.String(36),
                           db.ForeignKey("users.id", ondelete="SET NULL"),
                           nullable=True)
    type_id = db.Column(db.Integer, db.ForeignKey("activity_types.id"), nullable=True)

    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(MAX_DESCRIPTION_LENGTH), nullable=False)

    # NFR-REL-001: free text only. No device location is stored, ever.
    meeting_point = db.Column(db.String(300), nullable=False)

    start_at = db.Column(db.DateTime(timezone=True), nullable=False)
    end_at = db.Column(db.DateTime(timezone=True), nullable=False)

    budget = db.Column(db.Numeric(10, 2))
    max_attendees = db.Column(db.Integer, nullable=False)

    # Denormalized so capacity can be checked under a row lock without
    # counting memberships (FR-PE-004, NFR-REL-005).
    attendee_count = db.Column(db.Integer, default=0, nullable=False)

    min_age = db.Column(db.Integer)
    max_age = db.Column(db.Integer)
    min_year = db.Column(db.String(40))
    max_year = db.Column(db.String(40))

    # FR-PE-009. Null or empty means no restriction.
    allowed_genders = db.Column(db.String(200))

    # FR-ACE-001: null means the standard activity placeholder photo.
    image_key = db.Column(db.String(255))

    # FR-ACE-002: a new activity defaults to OPEN.
    status = db.Column(db.String(30), default=STATUS_OPEN, nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow,
                           nullable=False)

    university = db.relationship("University")
    creator = db.relationship("User", foreign_keys=[creator_id],
                              backref=db.backref("created_activities", passive_deletes=True))
    activity_type = db.relationship("ActivityType")

    @property
    def image(self):
        return self.image_key or DEFAULT_ACTIVITY_IMAGE

    @property
    def creator_name(self):
        """FR-AD-004 shows the creator; FR-AUTH-017 may have removed them."""
        return self.creator.name if self.creator else DELETED_USER_NAME

    @property
    def seats_left(self):
        return self.max_attendees - self.attendee_count

    def __repr__(self):
        return f"<Activity {self.id} {self.title!r} {self.status}>"


class ActivityDestination(db.Model):
    """FR-ACE-001; NFR-REL-001 free text only."""

    __tablename__ = "activity_destinations"
    __table_args__ = (
        UniqueConstraint("activity_id", "position", name="uq_destination_position"),
    )

    id = _uuid_pk()
    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            nullable=False)
    position = db.Column(db.Integer, nullable=False)
    text = db.Column(db.String(300), nullable=False)

    activity = db.relationship(
        "Activity",
        backref=db.backref("destinations", order_by="ActivityDestination.position",
                           cascade="all, delete-orphan"),
    )


class ActivityMembership(db.Model):
    """FR-ACE-003, FR-ACE-007, FR-PE-003; NFR-REL-005."""

    __tablename__ = "activity_memberships"
    __table_args__ = (
        CheckConstraint(
            "role IN ('" + "', '".join(ROLES) + "')",
            name="ck_membership_role",
        ),
        # FR-ACE-007: "There shall be exactly one Admin per activity." A
        # partial unique index makes at most one a database guarantee, so a
        # service bug cannot produce two.
        Index("uq_activity_single_admin", "activity_id",
              unique=True,
              sqlite_where=db.text("role = 'ADMIN'"),
              postgresql_where=db.text("role = 'ADMIN'")),
        # FR-ACE-007 succession promotes the earliest joined member.
        Index("ix_membership_user_joined", "user_id", "joined_at"),
    )

    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        primary_key=True)
    role = db.Column(db.String(20), default=ROLE_ATTENDEE, nullable=False)
    joined_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    activity = db.relationship(
        "Activity", backref=db.backref("memberships", cascade="all, delete-orphan"))
    user = db.relationship("User", backref=db.backref("memberships", passive_deletes=True))


class FinalizedField(db.Model):
    """FR-ACE-008. A row per finalized field, not a boolean column.

    Rows rather than flags because the design records who finalized a field
    and when, and because finalization is one-way: the row's presence is the
    fact, so there is no way to express "un-finalized" by accident.
    """

    __tablename__ = "finalized_fields"
    __table_args__ = (
        CheckConstraint(
            "field_name IN ('" + "', '".join(FINALIZABLE_FIELDS) + "')",
            name="ck_finalized_field_name",
        ),
    )

    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            primary_key=True)
    field_name = db.Column(db.String(40), primary_key=True)
    finalized_by = db.Column(db.String(36),
                             db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    finalized_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    activity = db.relationship(
        "Activity", backref=db.backref("finalized_fields", cascade="all, delete-orphan"))


# --- Participation (design Figure 18) ----------------------------------------

class JoinRequest(db.Model):
    """FR-PE-002."""

    __tablename__ = "join_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('" + "', '".join(REQUEST_STATES) + "')",
            name="ck_join_request_status",
        ),
        CheckConstraint(
            f"message IS NULL OR length(message) <= {MAX_DESCRIPTION_LENGTH}",
            name="ck_join_request_message_length",
        ),
        # One outstanding request per user per activity; decided rows remain
        # as the record the decision notification is sent from.
        Index("uq_join_request_pending", "activity_id", "user_id",
              unique=True,
              sqlite_where=db.text("status = 'PENDING'"),
              postgresql_where=db.text("status = 'PENDING'")),
    )

    id = _uuid_pk()
    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            nullable=False)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    message = db.Column(db.String(MAX_DESCRIPTION_LENGTH))
    status = db.Column(db.String(20), default=REQUEST_PENDING, nullable=False)
    reviewed_by = db.Column(db.String(36),
                            db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    decided_at = db.Column(db.DateTime(timezone=True))

    activity = db.relationship("Activity", backref="join_requests")
    user = db.relationship("User", foreign_keys=[user_id],
                           backref=db.backref("join_requests", passive_deletes=True))
    reviewer = db.relationship("User", foreign_keys=[reviewed_by])


class WaitlistEntry(db.Model):
    """FR-PE-010. Position is computed from created_at, never stored, so
    removals never renumber rows."""

    __tablename__ = "waitlist_entries"
    __table_args__ = (Index("ix_waitlist_activity_created", "activity_id", "created_at"),)

    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    activity = db.relationship(
        "Activity", backref=db.backref("waitlist", cascade="all, delete-orphan"))
    user = db.relationship("User")


class Invitation(db.Model):
    """FR-PE-011. Single-use, stored only as a hash (NFR-SECU-007)."""

    __tablename__ = "invitations"

    id = _uuid_pk()
    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            nullable=False)
    # Nullable for the same reason as Activity.creator_id above.
    invited_by = db.Column(db.String(36),
                           db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    email = db.Column(db.String(255))
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))

    activity = db.relationship("Activity", backref="invitations")
    inviter = db.relationship("User", foreign_keys=[invited_by])


class SavedActivity(db.Model):
    """FR-AD-005."""

    __tablename__ = "saved_activities"

    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        primary_key=True)
    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            primary_key=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("saved_activities", passive_deletes=True))
    activity = db.relationship("Activity")


# --- Messaging (design Figure 19) --------------------------------------------

class ForumTopic(db.Model):
    """FR-MESS-003, FR-MESS-005. "Main" is created with the activity."""

    __tablename__ = "forum_topics"
    __table_args__ = (
        UniqueConstraint("activity_id", "name", name="uq_topic_activity_name"),
    )

    id = _uuid_pk()
    activity_id = db.Column(db.String(36), db.ForeignKey("activities.id", ondelete="CASCADE"),
                            nullable=False)
    name = db.Column(db.String(80), nullable=False)
    created_by = db.Column(db.String(36),
                           db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    activity = db.relationship(
        "Activity", backref=db.backref("topics", cascade="all, delete-orphan"))
    creator = db.relationship("User", foreign_keys=[created_by])


class Message(db.Model):
    """FR-MESS-002, 006, 007. Sequential id, per the design's table notes."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            f"length(body) >= 1 AND length(body) <= {MAX_MESSAGE_LENGTH}",
            name="ck_message_body_length",
        ),
        Index("ix_message_topic_id", "topic_id", "id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    topic_id = db.Column(db.String(36), db.ForeignKey("forum_topics.id", ondelete="CASCADE"),
                         nullable=False)

    # FR-AUTH-017: null after the author deletes their account, which is how
    # messages survive under the "Deleted user" placeholder.
    author_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"),
                          nullable=True)

    body = db.Column(db.String(MAX_MESSAGE_LENGTH), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    edited_at = db.Column(db.DateTime(timezone=True))

    # FR-MESS-007: soft delete, so the client can show the placeholder.
    deleted_at = db.Column(db.DateTime(timezone=True))

    topic = db.relationship(
        "ForumTopic", backref=db.backref("messages", cascade="all, delete-orphan"))
    author = db.relationship("User", backref=db.backref("messages", passive_deletes=True))

    @property
    def author_name(self):
        """FR-MESS-006, FR-AUTH-017."""
        return self.author.name if self.author else DELETED_USER_NAME

    @property
    def is_deleted(self):
        return self.deleted_at is not None


class MessageMention(db.Model):
    """FR-MESS-008."""

    __tablename__ = "message_mentions"

    message_id = db.Column(db.Integer, db.ForeignKey("messages.id", ondelete="CASCADE"),
                           primary_key=True)
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        primary_key=True)

    message = db.relationship(
        "Message", backref=db.backref("mentions", cascade="all, delete-orphan"))
    user = db.relationship("User")


# --- Infrastructure tables ---------------------------------------------------

class Notification(db.Model):
    """FR-PE-002 and the rest of the design's notification event table."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notification_user_created", "user_id", "created_at"),)

    id = _uuid_pk()
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    type = db.Column(db.String(60), nullable=False)
    payload = db.Column(db.JSON)
    read_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("notifications", passive_deletes=True))


class OutboxEvent(db.Model):
    """NFR-REL-004. Written in the same transaction as the data change, then
    dispatched after commit, so a failing subscriber never blocks the publisher."""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("ix_outbox_unprocessed", "created_at",
              sqlite_where=db.text("processed_at IS NULL"),
              postgresql_where=db.text("processed_at IS NULL")),
    )

    id = _uuid_pk()
    type = db.Column(db.String(60), nullable=False)
    payload = db.Column(db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)
    processed_at = db.Column(db.DateTime(timezone=True))
    attempts = db.Column(db.Integer, default=0, nullable=False)


class VerificationToken(db.Model):
    """FR-AUTH-014, NFR-SECU-007: single-use, hash-only, 15-minute expiry.

    Unused while the external provider owns email verification and password
    change. It stays in the schema because the design specifies it and a
    second sign-in method may need it.
    """

    __tablename__ = "verification_tokens"

    id = _uuid_pk()
    user_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="CASCADE"),
                        nullable=False)
    purpose = db.Column(db.String(40), nullable=False)
    token_hash = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False)
    used_at = db.Column(db.DateTime(timezone=True))

    user = db.relationship("User", backref=db.backref("verification_tokens", passive_deletes=True))


class AuditLog(db.Model):
    """NFR-SECU-012. Append-only: grant the application role INSERT and SELECT
    only, so rows cannot be changed or removed."""

    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)

    id = _uuid_pk()
    actor_id = db.Column(db.String(36), db.ForeignKey("users.id", ondelete="SET NULL"),
                         nullable=True)
    action = db.Column(db.String(80), nullable=False)
    target_type = db.Column(db.String(60))
    target_id = db.Column(db.String(36))
    metadata_json = db.Column("metadata", db.JSON)
    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    actor = db.relationship("User", foreign_keys=[actor_id])
