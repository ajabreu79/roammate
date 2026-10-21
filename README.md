# roammate

Flask app with authentication delegated to [Clerk](https://clerk.com).

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`ClerkAdapter` is written against `clerk-backend-api` 7.x: the request
verification helpers live in `clerk_backend_api.security`, and
`authenticate_request` takes the secret key per call rather than from the
client. Older 1.x/2.x releases put them in `clerk_backend_api.jwks_helpers`,
so the pin in `requirements.txt` matters. `tests/test_clerk_adapter.py`
asserts the symbols we call still exist.

Create a Clerk application, then export its keys:

```bash
export CLERK_PUBLISHABLE_KEY=pk_test_...
export CLERK_SECRET_KEY=sk_test_...
export SECRET_KEY=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
```

Verify the wiring and print the dashboard checklist:

```bash
.venv/bin/python scripts/check_clerk.py
```

Run it:

```bash
.venv/bin/flask --app app run --debug
```

Set `COOKIE_SECURE=1` in any deployment served over HTTPS.

## Clerk dashboard settings

Several requirements are satisfied by Clerk configuration rather than by code
in this repository. They are listed in `app/providers.py`
(`CLERK_REQUIRED_SETTINGS`) and must be set in the Clerk dashboard:

| Setting | Value | Requirement |
|---|---|---|
| First and last name | required | FR-AUTH-001 |
| Password minimum length | 12 | FR-AUTH-015 |
| Require lowercase, uppercase, number, special | on | FR-AUTH-015 |
| Email address verification | required | FR-AUTH-014 |
| Google social connection | enabled | FR-AUTH-016 |
| Session lifetime | 30 days | FR-AUTH-008 |
| Bot & abuse protection | enabled | FR-AUTH-018 |

Also set the Clerk **paths**: sign-in `/auth/login`, sign-up `/auth/signup`,
and add `/auth/oauth/google/callback` as an allowed redirect.

## Where each requirement lives

| | Requirement | Where |
|---|---|---|
| FR-AUTH-001 | Two-step registration | Step 1 Clerk sign-up component; step 2 `auth.signup2_post` → `AuthService.complete_profile` |
| FR-AUTH-002 | Passwords only as hashes | Clerk. `model.User` has no password column |
| FR-AUTH-003 | Password confirmation match | Clerk sign-up component |
| FR-AUTH-004 | One account per email | Clerk, plus `unique=True` on `User.email` and the merge in `AuthService.sync_from_provider` |
| FR-AUTH-005 | No future birthdays, 18+ | `RegistrationValidator.parse_birthday` |
| FR-AUTH-006 | 500-character description | `RegistrationValidator.check_description` |
| FR-AUTH-007 | Email + password login | Clerk sign-in component |
| FR-AUTH-008 | 30-day cookie | Clerk session lifetime; `SessionManager` |
| FR-AUTH-009 | Manual logout | `auth.logout_post` → `SessionManager.end` (revokes the Clerk session) |
| FR-AUTH-010 | Protected routes by default | `auth.require_authentication`, registered via `app.before_request` |
| FR-AUTH-011 | One click between login and signup | Links in `signup.html` / `login.html`, plus Clerk's own |
| FR-AUTH-012 | Default and changeable photo | `model.DEFAULT_PROFILE_PIC`; `auth.edit_profile_post` |
| FR-AUTH-013 | Filterable university dropdown | `app/universities.py`, `auth.university_options`, `static/js/signup2.js` |
| FR-AUTH-014 | Email verification before access | Clerk; gated by `VERIFICATION_EXEMPT` in `auth.require_authentication` |
| FR-AUTH-015 | Password complexity | Clerk password policy |
| FR-AUTH-016 | Google OAuth | Clerk social connection; `auth.oauth_google_callback` |
| FR-AUTH-017 | Deletion and GDPR export | `AccountDeletionService`, `DataExportService` |
| FR-AUTH-018 | Rate limiting | Clerk for sign-in/sign-up; `app/ratelimit.py` for our own endpoints |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

The suite stubs the Clerk adapter, so it runs without Clerk credentials.

## Known gaps

- The rate limiter in `app/ratelimit.py` is in-process, so each worker keeps
  its own counter; a multi-worker deployment needs Redis behind it.
- `AccountDeletionService._leave_activities` expects a `ParticipationService`
  that does not exist yet; deletion logs a warning and proceeds.
- `DataExportService` exports the profile only. Follows, memberships and
  messages appear automatically once those relationships exist on `User`.
