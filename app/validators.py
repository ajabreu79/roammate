"""RegistrationValidator.

The design doc assigns this class password match, password complexity,
unique email, birthday rules and description length. With Clerk as the
provider, the first three are enforced before any request reaches us:

  FR-AUTH-003  password match      -> Clerk sign-up form
  FR-AUTH-015  password complexity -> Clerk password policy
  FR-AUTH-004  unique email        -> Clerk, plus the unique constraint on
                                      User.email in model.py

See providers.CLERK_REQUIRED_SETTINGS for the exact dashboard values those
depend on. What is left here is the profile data Clerk knows nothing about.
"""

from datetime import date, datetime

from . import db
from . import model
from . import universities

MAX_DESC_LENGTH = 500
MIN_AGE = 18


class RegistrationValidator:
    """Every method returns a list of messages; empty means acceptable."""

    @staticmethod
    def normalize_email(email):
        """Emails are case-insensitive identities, so store one canonical form."""
        return (email or "").strip().lower()

    @staticmethod
    def email_is_registered(email):
        query = db.select(model.User).where(model.User.email == email)
        return db.session.execute(query).scalar_one_or_none() is not None

    @staticmethod
    def parse_birthday(birthday_raw):
        """Return (birthday, errors) -- FR-AUTH-005.

        An unparseable date is a validation error, not a 500.
        """
        if not birthday_raw:
            return None, ["Please provide your birthday."]
        try:
            birthday = datetime.strptime(birthday_raw, "%Y-%m-%d").date()
        except ValueError:
            return None, ["Please provide a birthday in YYYY-MM-DD format."]

        today = date.today()
        if birthday > today:
            return None, ["Provided birthday is invalid."]

        # One age calculation for the whole system; see model.age_from_birthday.
        if model.age_from_birthday(birthday, today) < MIN_AGE:
            return None, [f"You must be at least {MIN_AGE} years old to use this site."]

        return birthday, []

    @staticmethod
    def check_description(description):
        """FR-AUTH-006."""
        if description and len(description) > MAX_DESC_LENGTH:
            return [f"Maximum description length is {MAX_DESC_LENGTH} characters."]
        return []

    @staticmethod
    def resolve_university(name):
        """Return (university, errors) for a submitted university name.

        FR-AUTH-013 / FR-PROF-009: the name has to resolve to a real
        University row, because its id is the isolation boundary every
        activity query is scoped by.
        """
        if not (name or "").strip():
            return None, ["Please select your university."]
        university = universities.get_by_name(name)
        if university is None:
            return None, ["Please choose a university from the list."]
        return university, []

    @classmethod
    def validate_profile(cls, gender, school_year, university, birthday_raw, description):
        """Step 2 of registration (FR-AUTH-001).

        Returns (errors, birthday, university) -- the resolved University row,
        since the caller needs its id.
        """
        errors = []
        if not (gender or "").strip():
            errors.append("Please select your gender.")
        if not (school_year or "").strip():
            errors.append("Please select your school year.")

        resolved_university, university_errors = cls.resolve_university(university)
        errors.extend(university_errors)

        birthday, birthday_errors = cls.parse_birthday(birthday_raw)
        errors.extend(birthday_errors)
        errors.extend(cls.check_description(description))

        return errors, birthday, resolved_university
