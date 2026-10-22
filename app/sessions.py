"""Session handling.

Deviation from the design doc: the doc has SessionManager issuing our own
signed JWT (sub, uni, sid) and keeping session:{sid} in Redis. With Clerk as
the provider that would be a second, redundant session. Clerk already issues
a signed, HttpOnly, Secure, SameSite session cookie carrying sub and sid,
verified here against its JWKS on every request, so this module reads that
session rather than minting one. The 30-day lifetime of FR-AUTH-008 is a
Clerk session setting (see providers.CLERK_REQUIRED_SETTINGS), and logout
revokes the session through the adapter.

The one claim Clerk does not carry is `uni`; it is cached in the Flask
session so callers that want it do not re-query per request. It holds the
university *id* -- the FR-PROF-009 isolation key that `_university_scoped`
compares against -- and not the University row: the Flask session is a signed
JSON cookie, so only JSON-serialisable values may go in it.
"""

from flask import current_app, session

from . import providers


class SessionManager:
    @staticmethod
    def identify(flask_request):
        """Return the verified Clerk payload for this request, or None."""
        try:
            return providers.CLERK.authenticate(flask_request)
        except providers.ProviderUnavailable:
            return None

    @staticmethod
    def remember_context(user, payload):
        """Cache the design doc's non-Clerk claims for this request's session."""
        session["sid"] = (payload or {}).get("sid")
        # The id, not user.university: that attribute is the University row,
        # which the session cookie cannot serialise. None until step 2 of
        # registration sets it (FR-AUTH-001).
        session["uni"] = user.university_id

    @staticmethod
    def current_sid():
        return session.get("sid")

    @staticmethod
    def current_university_id():
        """The cached FR-PROF-009 scope, or None before registration step 2."""
        return session.get("uni")

    @staticmethod
    def end():
        """Revoke the Clerk session and drop our local state (FR-AUTH-009)."""
        sid = session.get("sid")
        try:
            providers.CLERK.revoke_session(sid)
        except Exception as error:
            # Logging out locally must succeed even if Clerk is unreachable.
            # The local session is cleared either way and the Clerk session
            # still expires on its own.
            current_app.logger.warning("Could not revoke Clerk session %s: %s", sid, error)
        session.clear()
