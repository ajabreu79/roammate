"""Orchestration for registration, login, logout, deletion and export.

The controller in auth.py stays thin: it reads the request, calls one of
these services, and turns the result into a response.
"""

import secrets

from flask import current_app

from . import db
from . import model
from . import providers
from .sessions import SessionManager
from .validators import RegistrationValidator


class AuthService:
    """Registration, sign-in and sign-out on top of the Clerk adapter."""

    @staticmethod
    def user_from_request(flask_request):
        """Resolve the request's Clerk session to our own User row.

        Called once per request by the Flask-Login request loader, so this is
        the single place identity enters the application. Returns None when
        the request carries no valid Clerk session.
        """
        payload = SessionManager.identify(flask_request)
        if not payload or not payload.get("sub"):
            return None

        user = AuthService.sync_from_provider(payload["sub"])
        if user is None or user.is_deleted:
            return None

        SessionManager.remember_context(user, payload)
        return user

    @staticmethod
    def sync_from_provider(clerk_user_id):
        """Find or create the local row for a Clerk identity.

        Registration step 1 (email, names, password, confirmation) happens in
        Clerk's own sign-up component, so the first time we see a verified
        session we create the profile row and let step 2 fill in the rest.
        """
        query = db.select(model.User).where(model.User.clerk_user_id == clerk_user_id)
        user = db.session.execute(query).scalar_one_or_none()

        try:
            identity = providers.CLERK.fetch_identity(clerk_user_id)
        except providers.ProviderUnavailable:
            return user
        except Exception as error:
            # A Clerk outage should not log out everyone who already has a row.
            current_app.logger.warning("Could not fetch Clerk user %s: %s", clerk_user_id, error)
            return user

        if identity is None:
            return user

        if user is None:
            # FR-AUTH-004: another row may already hold this address if the
            # same person signed up twice through different Clerk methods.
            existing = db.session.execute(
                db.select(model.User).where(model.User.email == identity["email"])
            ).scalar_one_or_none()
            if existing is not None and not existing.is_deleted:
                existing.clerk_user_id = clerk_user_id
                user = existing
            else:
                user = model.User(
                    clerk_user_id=clerk_user_id,
                    email=identity["email"],
                    first_name=identity["first_name"],
                    last_name=identity["last_name"],
                    email_verified=identity["email_verified"],
                    profile_pic=model.DEFAULT_PROFILE_PIC,
                )
                db.session.add(user)

        if not user.is_deleted:
            # Clerk is the authority on these three; mirror them every time.
            user.email = identity["email"]
            user.email_verified = identity["email_verified"]
            if identity["first_name"]:
                user.first_name = identity["first_name"]
            if identity["last_name"]:
                user.last_name = identity["last_name"]

        db.session.commit()
        return user

    @staticmethod
    def complete_profile(user, gender, school_year, university,
                         birthday_raw, description, profile_pic_filename=None):
        """Registration step 2 (FR-AUTH-001). Returns (errors, user)."""
        errors, birthday = RegistrationValidator.validate_profile(
            gender, school_year, university, birthday_raw, description
        )
        if errors:
            return errors, None

        user.gender = gender
        user.school_year = school_year
        user.university = university
        user.birthday = birthday
        user.desc = description or ""
        if profile_pic_filename:
            user.profile_pic = profile_pic_filename

        db.session.commit()
        return [], user

    @staticmethod
    def logout():
        """FR-AUTH-009."""
        SessionManager.end()


class AccountDeletionService:
    """FR-AUTH-017 deletion half.

    Our row is anonymized rather than dropped so messages the user sent
    survive under a "Deleted user" placeholder, as the design doc requires.
    The Clerk identity itself is deleted outright -- that is where the
    personal data and credentials live.
    """

    @staticmethod
    def delete(user):
        """Returns (errors, deleted)."""
        AccountDeletionService._leave_activities(user)

        clerk_user_id = user.clerk_user_id
        try:
            providers.CLERK.delete_identity(clerk_user_id)
        except providers.ProviderUnavailable:
            return ["Account deletion is unavailable right now. Please try again later."], False
        except Exception as error:
            # Anonymizing our row while the Clerk identity survives would let
            # the user sign back in to a hollow account, so refuse instead.
            current_app.logger.error("Clerk deletion failed for %s: %s", clerk_user_id, error)
            return ["Could not delete your account. Please try again later."], False

        user.clerk_user_id = f"deleted-{user.id}-{secrets.token_hex(8)}"
        user.email = f"deleted-{user.id}-{secrets.token_hex(8)}@deleted.invalid"
        user.first_name = model.DELETED_USER_NAME
        user.last_name = ""
        user.gender = None
        user.school_year = None
        user.university = None
        user.birthday = None
        user.desc = ""
        user.profile_pic = model.DEFAULT_PROFILE_PIC
        user.email_verified = False
        user.is_deleted = True

        db.session.commit()
        SessionManager.end()
        return [], True

    @staticmethod
    def _leave_activities(user):
        """Hand off to ParticipationService so admin succession runs.

        That module is not built yet; deletion must not be blocked on it, so
        its absence is logged rather than raised.
        """
        try:
            from .participation import ParticipationService
        except ImportError:
            current_app.logger.warning(
                "ParticipationService unavailable; user %s deleted without leaving activities",
                user.id,
            )
            return
        ParticipationService.leave_all(user)


class DataExportService:
    """FR-AUTH-017 export half: the user's own data as structured JSON."""

    @staticmethod
    def export(user):
        data = {
            "exported_at": model.utcnow().isoformat(),
            "profile": {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "gender": user.gender,
                "school_year": user.school_year,
                "university": user.university,
                "birthday": user.birthday.isoformat() if user.birthday else None,
                "description": user.desc,
                "profile_picture": user.profile_pic,
                "email_verified": user.email_verified,
                "auth_provider": user.auth_provider,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            },
        }

        # The design doc also calls for follows, memberships and messages.
        # Those models do not exist yet, so each appears once it does rather
        # than shipping an empty list that reads like real data.
        for attribute in ("follows", "memberships", "messages"):
            related = getattr(user, attribute, None)
            if related is None:
                continue
            data[attribute] = [DataExportService._row(item) for item in related]

        return data

    @staticmethod
    def _row(item):
        columns = getattr(getattr(item, "__table__", None), "columns", None)
        if columns is None:
            return str(item)
        row = {}
        for column in columns:
            value = getattr(item, column.name, None)
            row[column.name] = value.isoformat() if hasattr(value, "isoformat") else value
        return row
