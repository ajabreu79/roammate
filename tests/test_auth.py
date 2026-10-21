"""Each test names the requirement it covers."""

import json

from app import db, model, providers
from app.services import AuthService
from app.validators import MAX_DESC_LENGTH, RegistrationValidator

from .conftest import complete_profile_form, years_ago


# --- FR-AUTH-002 ------------------------------------------------------------

def test_user_table_has_no_password_column(app):
    assert "password" not in model.User.__table__.columns


# --- FR-AUTH-010 ------------------------------------------------------------

def test_protected_route_redirects_anonymous_to_signup(signed_out, client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/auth/signup")


def test_signup_and_login_are_reachable_anonymously(signed_out, client):
    assert client.get("/auth/signup").status_code == 200
    assert client.get("/auth/login").status_code == 200


def test_unlisted_route_is_protected_by_default(signed_out, client):
    """A new endpoint is protected without having to opt in."""
    response = client.get("/profile/1")
    assert response.headers["Location"].endswith("/auth/signup")


# --- FR-AUTH-014 ------------------------------------------------------------

def test_unverified_user_is_held_at_verification_page(client, identity):
    identity["email_verified"] = False
    response = client.get("/")
    assert response.headers["Location"].endswith("/auth/verify-email/pending")


def test_unverified_user_may_still_log_out_and_export(client, identity):
    identity["email_verified"] = False
    assert client.get("/auth/export").status_code == 200
    assert client.post("/auth/logout").status_code == 302


# --- FR-AUTH-001 ------------------------------------------------------------

def test_verified_user_without_profile_is_sent_to_step_two(client):
    response = client.get("/")
    assert response.headers["Location"].endswith("/auth/signup2")


def test_step_two_collects_the_required_fields(client):
    response = client.post("/auth/signup2", data=complete_profile_form())
    assert response.status_code == 302

    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.gender == "Woman"
    assert user.school_year == "Junior"
    assert user.university == "Case Western Reserve University"
    assert user.birthday is not None
    assert user.first_name == "Ada" and user.last_name == "Lovelace"


def test_home_is_reachable_once_the_profile_is_complete(client):
    client.post("/auth/signup2", data=complete_profile_form())
    assert client.get("/").status_code == 200


# --- FR-AUTH-005 ------------------------------------------------------------

def test_future_birthday_is_rejected(client):
    client.post("/auth/signup2",
                data=complete_profile_form(birthday=years_ago(-1)))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.birthday is None


def test_under_eighteen_is_rejected(client):
    client.post("/auth/signup2",
                data=complete_profile_form(birthday=years_ago(18, days=1)))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.birthday is None


def test_exactly_eighteen_is_accepted(client):
    client.post("/auth/signup2",
                data=complete_profile_form(birthday=years_ago(18)))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.birthday is not None


def test_malformed_birthday_is_a_validation_error_not_a_crash(client):
    response = client.post("/auth/signup2",
                           data=complete_profile_form(birthday="31/12/1999"))
    assert response.status_code == 302
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.birthday is None


# --- FR-AUTH-006 ------------------------------------------------------------

def test_description_at_the_limit_is_accepted(client):
    client.post("/auth/signup2",
                data=complete_profile_form(description="x" * MAX_DESC_LENGTH))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert len(user.desc) == MAX_DESC_LENGTH


def test_description_over_the_limit_is_rejected(client):
    client.post("/auth/signup2",
                data=complete_profile_form(description="x" * (MAX_DESC_LENGTH + 1)))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert not user.profile_complete


# --- FR-AUTH-012 ------------------------------------------------------------

def test_profile_picture_defaults_when_none_is_uploaded(client):
    client.post("/auth/signup2", data=complete_profile_form())
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.profile_pic == model.DEFAULT_PROFILE_PIC


def test_profile_picture_can_be_changed_later(client):
    import io
    client.post("/auth/signup2", data=complete_profile_form())
    response = client.post("/auth/edit-profile", data={
        "profile_pic": (io.BytesIO(b"not-really-a-png"), "me.png"),
    }, content_type="multipart/form-data")
    assert response.status_code == 302

    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.profile_pic != model.DEFAULT_PROFILE_PIC
    # Renamed, so two users uploading "me.png" cannot overwrite each other.
    assert user.profile_pic.endswith(".png") and "me" not in user.profile_pic


def test_non_image_upload_is_rejected(client):
    import io
    client.post("/auth/signup2", data=complete_profile_form())
    client.post("/auth/edit-profile", data={
        "profile_pic": (io.BytesIO(b"#!/bin/sh"), "payload.sh"),
    }, content_type="multipart/form-data")

    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.profile_pic == model.DEFAULT_PROFILE_PIC


# --- FR-AUTH-013 ------------------------------------------------------------

def test_university_options_filter_by_query(signed_out, client):
    rows = client.get("/auth/universities?q=case").get_json()
    assert [row["name"] for row in rows] == ["Case Western Reserve University"]


def test_university_options_filter_by_country(signed_out, client):
    rows = client.get("/auth/universities?country=Japan").get_json()
    assert {row["country"] for row in rows} == {"Japan"}
    assert len(rows) >= 2


def test_university_outside_the_list_is_rejected(client):
    client.post("/auth/signup2",
                data=complete_profile_form(university="Totally Real University"))
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.university is None


# --- FR-AUTH-009 ------------------------------------------------------------

def test_logout_revokes_the_clerk_session(client, clerk_calls):
    client.get("/auth/signup2")          # establish a session + sid
    response = client.post("/auth/logout")
    assert response.headers["Location"].endswith("/auth/login")
    assert clerk_calls["revoked"] == ["sess_1"]


def test_logout_is_not_reachable_by_get(client):
    assert client.get("/auth/logout").status_code == 405


# --- FR-AUTH-017 ------------------------------------------------------------

def test_export_returns_the_users_own_profile_as_json(client):
    client.post("/auth/signup2", data=complete_profile_form())
    response = client.get("/auth/export")

    assert response.mimetype == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    payload = json.loads(response.data)
    assert payload["profile"]["email"] == "student@case.edu"
    assert payload["profile"]["university"] == "Case Western Reserve University"


def test_deletion_requires_explicit_confirmation(client, clerk_calls):
    client.post("/auth/signup2", data=complete_profile_form())
    client.post("/auth/account", data={"confirm": "yes"})

    user = db.session.execute(db.select(model.User)).scalar_one()
    assert not user.is_deleted
    assert clerk_calls["deleted"] == []


def test_deletion_removes_the_clerk_identity_and_anonymizes_our_row(client, clerk_calls):
    client.post("/auth/signup2", data=complete_profile_form())
    client.post("/auth/account", data={"confirm": "DELETE"})

    assert clerk_calls["deleted"] == ["user_clerk_1"]
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.is_deleted
    assert user.name == model.DELETED_USER_NAME
    assert user.university is None and user.birthday is None
    assert "student@case.edu" not in user.email


def test_deletion_is_refused_when_clerk_rejects_it(client, monkeypatch):
    client.post("/auth/signup2", data=complete_profile_form())

    def explode(self, uid):
        raise RuntimeError("clerk is down")

    monkeypatch.setattr(providers.ClerkAdapter, "delete_identity", explode)
    client.post("/auth/account", data={"confirm": "DELETE"})

    user = db.session.execute(db.select(model.User)).scalar_one()
    # Anonymizing locally while the Clerk identity survives would leave a
    # signed-in user attached to a hollow account.
    assert not user.is_deleted


# --- FR-AUTH-004 ------------------------------------------------------------

def test_email_column_is_unique(app):
    assert model.User.__table__.columns["email"].unique


def test_second_clerk_identity_with_the_same_email_reuses_the_row(client, identity):
    client.get("/")
    first = db.session.execute(db.select(model.User)).scalar_one()

    identity["provider_user_id"] = "user_clerk_2"
    AuthService.sync_from_provider("user_clerk_2")

    rows = db.session.execute(db.select(model.User)).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == first.id
    assert rows[0].clerk_user_id == "user_clerk_2"


# --- FR-AUTH-018 ------------------------------------------------------------

def test_valid_submission_succeeds_under_the_limit(client):
    client.post("/auth/signup2", data=complete_profile_form())
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert user.profile_complete


def test_flooding_step_two_is_rate_limited(client):
    """Clerk rate-limits sign-in and sign-up; this covers our own endpoint."""
    for _ in range(25):                                  # limit is 20 / 5 min
        client.post("/auth/signup2", data=complete_profile_form(gender=""))

    # The limiter, not the validator, is now what turns requests away: a
    # submission that would otherwise succeed is refused.
    client.post("/auth/signup2", data=complete_profile_form())
    user = db.session.execute(db.select(model.User)).scalar_one()
    assert not user.profile_complete


# --- unit coverage for the validator ----------------------------------------

def test_email_normalization():
    assert RegistrationValidator.normalize_email("  Alice@CASE.edu ") == "alice@case.edu"


def test_parse_birthday_messages(app):
    assert RegistrationValidator.parse_birthday("")[1] == ["Please provide your birthday."]
    assert "YYYY-MM-DD" in RegistrationValidator.parse_birthday("nope")[1][0]
    assert RegistrationValidator.parse_birthday(years_ago(-1))[1] == ["Provided birthday is invalid."]


# --- design-doc route aliases -----------------------------------------------

def test_design_doc_register_paths_redirect_to_the_canonical_routes(signed_out, client):
    assert client.get("/auth/register").headers["Location"].endswith("/auth/signup")
    assert client.get("/auth/register/step2").headers["Location"].endswith("/auth/signup2")


# --- templates render --------------------------------------------------------

def test_anonymous_pages_render(signed_out, client):
    for path in ("/auth/signup", "/auth/login", "/auth/verify-email"):
        assert client.get(path).status_code == 200, path


def test_authenticated_pages_render(client):
    assert client.get("/auth/signup2").status_code == 200
    assert client.get("/auth/password-change").status_code == 200

    client.post("/auth/signup2", data=complete_profile_form())
    for path in ("/", "/auth/edit-profile", "/profile/1"):
        assert client.get(path).status_code == 200, path


def test_pages_render_without_clerk_configured(signed_out, app, client):
    """A missing publishable key must not 500 the sign-in page."""
    app.config["CLERK_PUBLISHABLE_KEY"] = None
    response = client.get("/auth/login")
    assert response.status_code == 200
    assert b"Sign-in is not configured" in response.data
