"""Tests for the instance-settings checker.

The settings themselves live in Clerk, not in this repo, so what is testable
is that check_settings() reads a `clerk config pull` payload correctly and
fails loudly when a requirement is not met.
"""

import copy

import pytest

from app.providers import CLERK_REQUIRED_SETTINGS, check_settings, lookup


# A payload shaped like `clerk config pull`, holding only the paths we assert.
COMPLIANT_CONFIG = {
    "user_model": {
        "first_name": {"enabled": True, "required": True},
        "last_name": {"enabled": True, "required": True},
    },
    "auth_password": {
        "min_length": 15,
        "require_lowercase": True,
        "require_uppercase": True,
        "require_numbers": True,
        "require_special_char": True,
    },
    "auth_email": {"verify_at_sign_up": True},
    "connection_oauth_google": {"enabled": True},
    "session_settings": {
        "maximum_lifetime": {"enabled": True, "duration_seconds": 2592000},
    },
    "auth_attack_protection": {
        "bot_protection": {"captcha_enabled": True},
        "user_lockout": {"enabled": True},
    },
}


def failures(config):
    return [path for path, _req, _actual, _expected, ok in check_settings(config) if not ok]


def test_the_live_configuration_shape_passes():
    assert failures(COMPLIANT_CONFIG) == []


def test_every_required_setting_is_covered_by_the_fixture():
    """If a requirement is added, the fixture has to grow with it."""
    for path, _operator, _expected, _requirement in CLERK_REQUIRED_SETTINGS:
        found, _value = lookup(COMPLIANT_CONFIG, path)
        assert found, f"{path} is required but missing from COMPLIANT_CONFIG"


def test_a_missing_path_is_a_failure_not_a_crash():
    assert "auth_password.min_length" in failures({})


@pytest.mark.parametrize("path, value", [
    ("user_model.first_name.required", False),
    ("user_model.last_name.required", False),
    ("auth_password.require_lowercase", False),
    ("auth_password.require_uppercase", False),
    ("auth_password.require_numbers", False),
    ("auth_password.require_special_char", False),
    ("auth_email.verify_at_sign_up", False),
    ("connection_oauth_google.enabled", False),
    ("auth_attack_protection.bot_protection.captcha_enabled", False),
    ("auth_attack_protection.user_lockout.enabled", False),
])
def test_each_setting_being_off_is_caught(path, value):
    config = copy.deepcopy(COMPLIANT_CONFIG)
    node = config
    *parents, leaf = path.split(".")
    for segment in parents:
        node = node[segment]
    node[leaf] = value

    assert path in failures(config)


def test_a_shorter_password_minimum_is_caught():
    config = copy.deepcopy(COMPLIANT_CONFIG)
    config["auth_password"]["min_length"] = 11
    assert "auth_password.min_length" in failures(config)


def test_a_longer_password_minimum_is_allowed():
    """FR-AUTH-015 is a floor, so 15 and 20 both satisfy it."""
    config = copy.deepcopy(COMPLIANT_CONFIG)
    config["auth_password"]["min_length"] = 20
    assert "auth_password.min_length" not in failures(config)


def test_the_wrong_session_lifetime_is_caught():
    config = copy.deepcopy(COMPLIANT_CONFIG)
    config["session_settings"]["maximum_lifetime"]["duration_seconds"] = 604800  # 7 days
    assert "session_settings.maximum_lifetime.duration_seconds" in failures(config)


# --- key misconfiguration messages -------------------------------------------

def test_missing_keys_are_reported_as_unset():
    from app.providers import clerk_status

    enabled, message = clerk_status(None, None)
    assert not enabled
    assert "CLERK_PUBLISHABLE_KEY and CLERK_SECRET_KEY is not set" in message


def test_a_malformed_key_is_not_reported_as_unset(app):
    """A stale shell export beats .env, so "set it" would be wrong advice."""
    from app.providers import clerk_status

    enabled, message = clerk_status("pk_test_...", "sk_test_...")
    assert not enabled
    assert "malformed" in message
    assert "unset CLERK_PUBLISHABLE_KEY" in message


def test_well_formed_keys_are_enabled(app):
    import base64
    from app.providers import clerk_status

    key = "pk_test_" + base64.b64encode(b"happy-hippo-1.clerk.accounts.dev$").decode().rstrip("=")
    enabled, message = clerk_status(key, "sk_test_real")
    assert enabled and message == ""
