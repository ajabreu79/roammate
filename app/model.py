from datetime import date, datetime, timezone

import flask_login

from . import db

DEFAULT_PROFILE_PIC = "Defaultpfp.png"
DELETED_USER_NAME = "Deleted user"


def utcnow():
    return datetime.now(timezone.utc)


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


class User(db.Model, flask_login.UserMixin):
    """Our profile record for a Clerk identity.

    FR-AUTH-002: there is deliberately no password column. Credentials live
    with Clerk and never reach this database.
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)

    # The Clerk user id (`sub` in the session token) is the real identity.
    clerk_user_id = db.Column(db.String(255), unique=True, nullable=False, index=True)
    auth_provider = db.Column(db.String(40), default="clerk", nullable=False)

    # FR-AUTH-004: one account per address. Clerk enforces this on its side
    # too, but the constraint belongs in our schema as well.
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)

    first_name = db.Column(db.String(80), default="", nullable=False)
    last_name = db.Column(db.String(80), default="", nullable=False)

    # FR-AUTH-014, mirrored from Clerk on every request.
    email_verified = db.Column(db.Boolean, default=False, nullable=False)

    # Step 2 of registration (FR-AUTH-001). Nullable because the row is
    # created the moment Clerk authenticates, before step 2 is filled in.
    gender = db.Column(db.String(40))
    school_year = db.Column(db.String(40))
    university = db.Column(db.String(200))
    birthday = db.Column(db.Date)
    desc = db.Column(db.String(500), default="", nullable=False)

    # FR-AUTH-012
    profile_pic = db.Column(db.String(255), default=DEFAULT_PROFILE_PIC, nullable=False)

    created_at = db.Column(db.DateTime(timezone=True), default=utcnow, nullable=False)

    # FR-AUTH-017: deletion anonymizes rather than drops the row, so messages
    # survive under a "Deleted user" placeholder.
    is_deleted = db.Column(db.Boolean, default=False, nullable=False)

    @property
    def name(self):
        if self.is_deleted:
            return DELETED_USER_NAME
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def age(self):
        """FR-PROF-003: derived from birthday, never stored as a column."""
        return age_from_birthday(self.birthday)

    @property
    def profile_complete(self):
        """Whether step 2 of registration has been answered."""
        return all([self.gender, self.school_year, self.university, self.birthday])

    def __repr__(self):
        return f"<User {self.id} {self.email}>"
