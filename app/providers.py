"""Sign-in behind one interface.

Per the design doc, AuthProviderAdapter is the seam over the external
identity provider and ClerkAdapter is its implementation. A new sign-in
method -- university SSO, say -- is a new adapter method here, not a change
to AuthService, the controller, or any model.

Clerk owns the credential half of the requirements:

  FR-AUTH-002  passwords never reach this application
  FR-AUTH-003  password / confirmation match
  FR-AUTH-015  12+ chars, lower, upper, digit, special
  FR-AUTH-014  email verification before the session is usable
  FR-AUTH-016  Google OAuth
  FR-AUTH-018  rate limiting on sign-in and sign-up

Several of those are dashboard settings rather than code. They are stated in
CLERK_REQUIRED_SETTINGS below and printed as a checklist by
scripts/check_clerk.py, which cannot assert them -- the Backend API does not
expose instance settings for reading.
"""

import base64
import binascii

from flask import current_app

# The Clerk instance configuration this application depends on, as
# (config path, operator, expected value, requirement). Paths match the JSON
# from `clerk config pull`; scripts/check_clerk_config.py asserts them.
#
# These are instance settings, not code, and the Backend API does not expose
# them for reading -- only the authenticated CLI does. So they are checked by
# piping a config pull into that script rather than by check_clerk.py.
CLERK_REQUIRED_SETTINGS = [
    # FR-AUTH-001 step 1 collects first and last name. Clerk only returns them
    # if its sign-up form requires them, and sync_from_provider has nothing to
    # fall back on.
    ("user_model.first_name.required", "==", True, "FR-AUTH-001"),
    ("user_model.last_name.required", "==", True, "FR-AUTH-001"),

    # FR-AUTH-015 asks for 12 or longer. The instance is deliberately set to
    # 15, which is stricter and still satisfies the requirement, so this
    # asserts the floor rather than an exact value.
    ("auth_password.min_length", ">=", 12, "FR-AUTH-015"),
    ("auth_password.require_lowercase", "==", True, "FR-AUTH-015"),
    ("auth_password.require_uppercase", "==", True, "FR-AUTH-015"),
    ("auth_password.require_numbers", "==", True, "FR-AUTH-015"),
    ("auth_password.require_special_char", "==", True, "FR-AUTH-015"),

    ("auth_email.verify_at_sign_up", "==", True, "FR-AUTH-014"),
    ("connection_oauth_google.enabled", "==", True, "FR-AUTH-016"),

    # FR-AUTH-008: 30 days, in seconds.
    ("session_settings.maximum_lifetime.enabled", "==", True, "FR-AUTH-008"),
    ("session_settings.maximum_lifetime.duration_seconds", "==", 2592000, "FR-AUTH-008"),

    # FR-AUTH-018: captcha covers sign-up flooding, lockout covers sign-in.
    ("auth_attack_protection.bot_protection.captcha_enabled", "==", True, "FR-AUTH-018"),
    ("auth_attack_protection.user_lockout.enabled", "==", True, "FR-AUTH-018"),
]


def lookup(config, path):
    """Follow a dotted path into the config. Returns (found, value)."""
    node = config
    for segment in path.split("."):
        if not isinstance(node, dict) or segment not in node:
            return False, None
        node = node[segment]
    return True, node


def check_settings(config):
    """Compare a `clerk config pull` payload against CLERK_REQUIRED_SETTINGS.

    Returns a list of (path, requirement, actual, expected, ok) rows.
    """
    operators = {
        "==": lambda actual, expected: actual == expected,
        ">=": lambda actual, expected: isinstance(actual, (int, float)) and actual >= expected,
    }

    results = []
    for path, operator, expected, requirement in CLERK_REQUIRED_SETTINGS:
        found, actual = lookup(config, path)
        ok = found and operators[operator](actual, expected)
        results.append((path, requirement, actual if found else "<missing>",
                        f"{operator} {expected}", ok))
    return results


class ProviderUnavailable(RuntimeError):
    """Clerk is not configured, so no request can be authenticated."""


class AuthProviderAdapter:
    """Interface over the external identity provider."""

    name = None

    def authenticate(self, flask_request):
        """Return the verified session payload for this request, or None."""
        raise NotImplementedError

    def fetch_identity(self, provider_user_id):
        """Return the provider's record for one user as a plain dict."""
        raise NotImplementedError

    def revoke_session(self, session_id):
        """Invalidate one session (FR-AUTH-009)."""
        raise NotImplementedError

    def delete_identity(self, provider_user_id):
        """Remove the user from the provider (FR-AUTH-017)."""
        raise NotImplementedError


class ClerkAdapter(AuthProviderAdapter):
    name = "clerk"

    def _client(self):
        client = current_app.extensions.get("clerk")
        if client is None:
            raise ProviderUnavailable(
                "CLERK_SECRET_KEY is not set; see README for configuration."
            )
        return client

    def authenticate(self, flask_request):
        """Verify Clerk's session cookie/header against its JWKS.

        Returns the token payload -- `sub` is the Clerk user id, `sid` the
        session id -- or None when the request carries no valid session.
        """
        from clerk_backend_api.security import AuthenticateRequestOptions

        # The SDK only needs an object exposing `.headers`, and reads the
        # __session cookie or an Authorization bearer off it. Flask's request
        # satisfies that directly.
        state = self._client().authenticate_request(
            flask_request,
            AuthenticateRequestOptions(
                # authenticate_request does not reuse the client's bearer_auth;
                # the secret key has to be passed per call.
                secret_key=current_app.config["CLERK_SECRET_KEY"],
                authorized_parties=current_app.config.get("CLERK_AUTHORIZED_PARTIES") or None,
            ),
        )
        if not state.is_signed_in:
            return None
        return dict(state.payload or {})

    def fetch_identity(self, provider_user_id):
        user = self._client().users.get(user_id=provider_user_id)
        if user is None:
            return None

        primary = None
        for address in (user.email_addresses or []):
            if address.id == user.primary_email_address_id:
                primary = address
                break
        if primary is None and user.email_addresses:
            primary = user.email_addresses[0]
        if primary is None or not primary.email_address:
            return None

        verification = getattr(primary, "verification", None)
        return {
            "provider_user_id": user.id,
            "email": primary.email_address.strip().lower(),
            "first_name": (user.first_name or "").strip(),
            "last_name": (user.last_name or "").strip(),
            # FR-AUTH-014: Clerk is the authority on whether the address is
            # confirmed, including for Google sign-ins.
            "email_verified": getattr(verification, "status", None) == "verified",
            "image_url": user.image_url,
        }

    def revoke_session(self, session_id):
        if not session_id:
            return False
        self._client().sessions.revoke(session_id=session_id)
        return True

    def delete_identity(self, provider_user_id):
        self._client().users.delete(user_id=provider_user_id)
        return True


CLERK = ClerkAdapter()

_ADAPTERS = {CLERK.name: CLERK}


def adapter_for(user_or_name=None):
    """The adapter that authenticates a given user. Clerk is the only one."""
    name = getattr(user_or_name, "auth_provider", user_or_name)
    return _ADAPTERS.get(name, CLERK)


def register_provider(app):
    """Build the Clerk client once per app."""
    secret = app.config.get("CLERK_SECRET_KEY")
    if not secret:
        app.logger.warning(
            "CLERK_SECRET_KEY is not set: every authenticated route will refuse "
            "requests until it is configured."
        )
        return None

    from clerk_backend_api import Clerk

    client = Clerk(bearer_auth=secret)
    app.extensions["clerk"] = client
    return client


def is_configured():
    return current_app.extensions.get("clerk") is not None


CLERK_JS_VERSION = "5"


def clerk_status(publishable_key, secret_key):
    """Why Clerk is or is not usable. Returns (enabled, message).

    Distinguishes a missing key from a malformed one: an environment variable
    exported earlier in the shell wins over .env (load_dotenv does not
    override), so "set it" is the wrong advice when the real problem is a
    leftover placeholder.
    """
    missing = [name for name, value in
               (("CLERK_PUBLISHABLE_KEY", publishable_key), ("CLERK_SECRET_KEY", secret_key))
               if not value]
    if missing:
        return False, f"{' and '.join(missing)} is not set. See README."

    if not clerk_js_url(publishable_key):
        return False, (
            f'CLERK_PUBLISHABLE_KEY is set but malformed: "{publishable_key[:12]}...". '
            "A stale export in this shell overrides .env -- try "
            "`unset CLERK_PUBLISHABLE_KEY CLERK_SECRET_KEY`."
        )

    return True, ""


def clerk_js_url(publishable_key):
    """The clerk-js bundle URL for a publishable key.

    Clerk serves the browser bundle from the instance's own Frontend API
    domain, which is base64-encoded inside the publishable key itself
    (`pk_test_<base64 of "host$">`), so the URL is derived rather than
    configured separately.
    """
    if not publishable_key:
        return None
    try:
        encoded = publishable_key.split("_", 2)[2]
        # The key omits base64 padding; add the maximum and let b64decode trim.
        host = base64.b64decode(encoded + "==").decode("utf-8").rstrip("$")
    except (IndexError, UnicodeDecodeError, binascii.Error):
        current_app.logger.warning("CLERK_PUBLISHABLE_KEY is not a well-formed Clerk key")
        return None
    if not host or "/" in host:
        return None
    return f"https://{host}/npm/@clerk/clerk-js@{CLERK_JS_VERSION}/dist/clerk.browser.js"
