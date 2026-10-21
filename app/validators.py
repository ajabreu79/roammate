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

        age = today.year - birthday.year - ((today.month, today.day) < (birthday.month, birthday.day))
        if age < MIN_AGE:
            return None, [f"You must be at least {MIN_AGE} years old to use this site."]

        return birthday, []

    @staticmethod
    def check_description(description):
        """FR-AUTH-006."""
        if description and len(description) > MAX_DESC_LENGTH:
            return [f"Maximum description length is {MAX_DESC_LENGTH} characters."]
        return []

    @staticmethod
    def check_university(university):
        """FR-AUTH-013: guard against a hand-crafted POST naming anything else."""
        if not (university or "").strip():
            return ["Please select your university."]
        if not universities.is_known(university):
            return ["Please choose a university from the list."]
        return []

    @classmethod
    def validate_profile(cls, gender, school_year, university, birthday_raw, description):
        """Step 2 of registration (FR-AUTH-001). Returns (errors, birthday)."""
        errors = []
        if not (gender or "").strip():
            errors.append("Please select your gender.")
        if not (school_year or "").strip():
            errors.append("Please select your school year.")
        errors.extend(cls.check_university(university))

        birthday, birthday_errors = cls.parse_birthday(birthday_raw)
        errors.extend(birthday_errors)
        errors.extend(cls.check_description(description))

        return errors, birthday
