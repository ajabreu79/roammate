"""Orchestration for registration, login, logout, deletion and export.

The controller in auth.py stays thin: it reads the request, calls one of
these services, and turns the result into a response.
"""


from flask import current_app

from . import db
from . import model
from . import providers
from .sessions import SessionManager
from .validators import RegistrationValidator


class AuthService:
    """Registration, sign-in and sign-out on top of the Clerk adapter."""

    @staticmethod
    def _apply_university_scope(provider_user_id):
        """Publish the verified identity to PostgreSQL for row-level security.

        NFR-SECU-010: the policies added in migration 4de96d5e2e95 resolve the
        requester's university from this setting, so no query can return
        another university's rows even if it forgets _university_scoped().

        `local=true` scopes it to the current transaction, which is what makes
        it safe over Neon's pooled endpoint: a session-level SET would leak to
        whichever client PgBouncer hands the connection to next.

        The value is the Clerk `sub` from an already-verified token, and it is
        bound as a parameter -- `SET` cannot take one, which is why this uses
        set_config() rather than string-building a SET statement.
        """
        if db.engine.dialect.name != "postgresql":
            return                       # SQLite has no row-level security
        db.session.execute(
            db.text("SELECT set_config('app.auth_provider_id', :sub, true)"),
            {"sub": provider_user_id},
        )

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

        # Before the first query, so the lookup itself is scoped.
        AuthService._apply_university_scope(payload["sub"])

        user = AuthService.sync_from_provider(payload["sub"])
        if user is None:
            return None

        SessionManager.remember_context(user, payload)
        return user

    @staticmethod
    def sync_from_provider(provider_user_id):
        """Find or create the local row for a Clerk identity.

        Registration step 1 (email, names, password, confirmation) happens in
        Clerk's own sign-up component, so the first time we see a verified
        session we create the profile row and let step 2 fill in the rest.
        """
        query = db.select(model.User).where(
            model.User.auth_provider_id == provider_user_id)
        user = db.session.execute(query).scalar_one_or_none()

        try:
            identity = providers.CLERK.fetch_identity(provider_user_id)
        except providers.ProviderUnavailable:
            return user
        except Exception as error:
            # A Clerk outage should not log out everyone who already has a row.
            current_app.logger.warning(
                "Could not fetch Clerk user %s: %s", provider_user_id, error)
            return user

        if identity is None:
            return user

        if user is None:
            # FR-AUTH-004: another row may already hold this address if the
            # same person signed up twice through different Clerk methods.
            existing = db.session.execute(
                db.select(model.User).where(model.User.email == identity["email"])
            ).scalar_one_or_none()
            if existing is not None:
                existing.auth_provider_id = provider_user_id
                user = existing
            else:
                user = model.User(
                    auth_provider_id=provider_user_id,
                    email=identity["email"],
                    first_name=identity["first_name"],
                    last_name=identity["last_name"],
                    email_verified=identity["email_verified"],
                    # FR-AUTH-012: null means the system-wide default photo.
                    profile_photo_key=None,
                )
                db.session.add(user)

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
        errors, birthday, resolved_university = RegistrationValidator.validate_profile(
            gender, school_year, university, birthday_raw, description
        )
        if errors:
            return errors, None

        user.gender = gender
        user.school_year = school_year
        user.university_id = resolved_university.id
        user.birthday = birthday
        user.description = description or ""
        if profile_pic_filename:
            user.profile_photo_key = profile_pic_filename

        db.session.commit()
        return [], user

    @staticmethod
    def logout():
        """FR-AUTH-009."""
        SessionManager.end()


class AccountDeletionService:
    """FR-AUTH-017 deletion half.

    The design doc's retention rules: deletion removes the user's profile,
    follows, preferences, links, saved activities and notifications, and sets
    author_id to null on their messages -- which is how messages survive under
    the "Deleted user" placeholder (model.Message.author_name). Those cascades
    are declared on the schema, so one row delete carries them out.

    The Clerk identity is deleted first, because that is where the credentials
    and personal data actually live.
    """

    @staticmethod
    def delete(user):
        """Returns (errors, deleted)."""
        errors = AccountDeletionService._leave_activities(user)
        if errors:
            return errors, False

        provider_user_id = user.auth_provider_id
        try:
            providers.CLERK.delete_identity(provider_user_id)
        except providers.ProviderUnavailable:
            return ["Account deletion is unavailable right now. Please try again later."], False
        except Exception as error:
            # Removing our row while the Clerk identity survives would let the
            # user sign back in to a hollow account, so refuse instead.
            current_app.logger.error("Clerk deletion failed for %s: %s", provider_user_id, error)
            return ["Could not delete your account. Please try again later."], False

        db.session.delete(user)
        db.session.commit()
        SessionManager.end()
        return [], True

    @staticmethod
    def _leave_activities(user):
        """Leave every activity so FR-ACE-007 admin succession runs.

        Returns a list of errors. ParticipationService is not built yet; its
        absence must not block deletion, but the memberships cannot simply be
        dropped either -- an activity whose Admin vanishes needs a successor
        promoted, and only that service knows how. So the memberships are
        removed by the schema cascade and the gap is logged loudly.

        GAP (FR-ACE-007): until ParticipationService exists, deleting an
          account that admins an activity leaves that activity with no Admin,
          which the partial unique index permits (it caps admins at one, it
          cannot require one).
        """
        try:
            from .participation import ParticipationService
        except ImportError:
            admin_count = db.session.execute(
                db.select(db.func.count())
                .select_from(model.ActivityMembership)
                .where(model.ActivityMembership.user_id == user.id,
                       model.ActivityMembership.role == model.ROLE_ADMIN)
            ).scalar_one()
            current_app.logger.warning(
                "ParticipationService unavailable; deleting user %s leaves %d "
                "activity/activities without an Admin (FR-ACE-007)",
                user.id, admin_count,
            )
            return []
        ParticipationService.leave_all(user)
        return []


class DataExportService:
    """FR-AUTH-017 export half: the user's own data as structured JSON."""

    @staticmethod
    def export(user):
        # NFR-SECU-009 does not apply here: this is the owner's own data,
        # which is the whole point of FR-AUTH-017.
        data = {
            "exported_at": model.utcnow().isoformat(),
            "profile": {
                "email": user.email,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "gender": user.gender,
                "school_year": user.school_year,
                "university": user.university_name,
                "birthday": user.birthday.isoformat() if user.birthday else None,
                "age": user.age,
                "description": user.description,
                "profile_photo": user.profile_photo,
                "account_public": user.account_public,
                "email_verified": user.email_verified,
                "created_at": user.created_at.isoformat() if user.created_at else None,
            },
            # The design's DataExportService: profile, follows, memberships
            # and messages.
            "follows": {
                "following": [
                    {"user": follow.followee.name, "status": follow.status,
                     "since": follow.created_at.isoformat()}
                    for follow in user.following
                ],
                "followers": [
                    {"user": follow.follower.name, "status": follow.status,
                     "since": follow.created_at.isoformat()}
                    for follow in user.followers
                ],
            },
            "memberships": [
                {"activity": membership.activity.title, "role": membership.role,
                 "joined_at": membership.joined_at.isoformat()}
                for membership in user.memberships
            ],
            "messages": [
                {"activity": message.topic.activity.title, "topic": message.topic.name,
                 "body": message.body, "created_at": message.created_at.isoformat(),
                 "deleted": message.is_deleted}
                for message in user.messages
            ],
        }
        return data
