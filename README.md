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

## Clerk instance settings

Several requirements are satisfied by Clerk configuration rather than by code
here. They are declared in `app/providers.py` (`CLERK_REQUIRED_SETTINGS`) as
(config path, operator, expected value, requirement) and are **asserted**, not
just documented:

```bash
npx -y clerk@latest auth login          # once; dashboard sign-in is separate
npx -y clerk@latest link --app <app_id> # once per checkout
npx -y clerk@latest config pull | .venv/bin/python scripts/check_clerk_config.py
```

That exits non-zero if anything drifts, so it works as a pre-submission gate.
The Backend API does not expose instance settings for reading, which is why
this goes through the CLI rather than `scripts/check_clerk.py`.

To change a setting, patch it rather than clicking through the dashboard:

```bash
npx -y clerk@latest config patch --file patch.json --dry-run
```

Current state of the settings that matter:

| Setting | Value | Requirement |
|---|---|---|
| `user_model.first_name.required` / `last_name.required` | `true` | FR-AUTH-001 |
| `auth_password.min_length` | `15` | FR-AUTH-015 |
| `auth_password.require_lowercase` / `uppercase` / `numbers` / `special_char` | `true` | FR-AUTH-015 |
| `auth_email.verify_at_sign_up` | `true` | FR-AUTH-014 |
| `connection_oauth_google.enabled` | `true` | FR-AUTH-016 |
| `session_settings.maximum_lifetime.duration_seconds` | `2592000` (30 days) | FR-AUTH-008 |
| `auth_attack_protection.bot_protection.captcha_enabled` | `true` | FR-AUTH-018 |
| `auth_attack_protection.user_lockout.enabled` | `true` (10 attempts / 60 min) | FR-AUTH-018 |

Three things worth knowing about those values:

- **`min_length` is 15, not the 12 of FR-AUTH-015.** Deliberate: stricter, and
  every accepted password still satisfies "12 or longer". The check asserts
  `>= 12` rather than `== 12` for that reason.
- **Email verification uses `email_code`, not a link.** Clerk emails a 6-digit
  code that its component collects inline, which is what
  `templates/auth/verify_email.html` tells the user to expect.
- **Captcha can block sign-ups in an automated browser.** For scripted
  testing, create a user with
  `npx -y clerk@latest users create --email <you>+clerk_test@example.com ...`
  and use verification code `424242`.

## Requirement traceability in the code

Every module tags the SRS requirements it implements inline, so a requirement
can be traced to code with grep rather than by reading:

```bash
grep -rn "FR-AUTH-005" app/          # where one requirement is implemented
grep -rn "GAP" app/                  # every known shortfall, with its requirement
```

`GAP (<requirement>)` marks a requirement that is only partly met, recorded
next to the code responsible. `app/main.py` currently carries 54 of them plus
a module docstring listing the design-doc deviations that apply to the whole
file; its routes are drafted against models that `model.py` does not define
yet, so they raise `AttributeError` at request time. Two tests in
`tests/test_auth.py` are marked `xfail(strict=True)` for that reason and will
start failing — on purpose — the moment the Activity model lands.

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
| FR-AUTH-008 | 30-day cookie | `session_settings.maximum_lifetime`; `SessionManager` |
| FR-AUTH-009 | Manual logout | `auth.logout_post` → `SessionManager.end` (revokes the Clerk session) |
| FR-AUTH-010 | Protected routes by default | `auth.require_authentication`, registered via `app.before_request` |
| FR-AUTH-011 | One click between login and signup | Links in `signup.html` / `login.html`, plus Clerk's own |
| FR-AUTH-012 | Default and changeable photo | `model.DEFAULT_PROFILE_PIC`; `auth.edit_profile_post` |
| FR-PROF-002 | Editable profile, minus email/university/birthday | `auth.edit_profile_post` |
| FR-PROF-003 | Age derived from birthday | `model.age_from_birthday` |
| FR-AUTH-013 | Filterable university dropdown | `app/universities.py`, `auth.university_options`, `static/js/signup2.js` |
| FR-AUTH-014 | Email verification before access | Clerk; gated by `VERIFICATION_EXEMPT` in `auth.require_authentication` |
| FR-AUTH-015 | Password complexity | `auth_password.*`, asserted by `scripts/check_clerk_config.py` |
| FR-AUTH-016 | Google OAuth | Clerk social connection; `auth.oauth_google_callback` |
| FR-AUTH-017 | Deletion and GDPR export | `AccountDeletionService`, `DataExportService` |
| FR-AUTH-018 | Rate limiting | Clerk for sign-in/sign-up; `app/ratelimit.py` for our own endpoints |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

The suite stubs the Clerk adapter, so it runs without Clerk credentials.
`tests/test_clerk_adapter.py` additionally asserts that the SDK symbols the
adapter calls still exist, and `tests/test_clerk_settings.py` covers the
instance-settings checker.

## Known gaps

- The rate limiter in `app/ratelimit.py` is in-process, so each worker keeps
  its own counter; a multi-worker deployment needs Redis behind it.
- `AccountDeletionService._leave_activities` expects a `ParticipationService`
  that does not exist yet; deletion logs a warning and proceeds.
- `DataExportService` exports the profile only. Follows, memberships and
  messages appear automatically once those relationships exist on `User`.
- No CSS yet: `roammate.css`, `roammate.js`, `css/signup.css`, `css/login.css`
  and `resources/logo.png` / `logo-full.png` are referenced by the templates
  but not in the repo, so pages render unstyled.
