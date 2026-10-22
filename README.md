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
next to the code responsible. `app/main.py` carries 49 of them plus a module
docstring listing the design-doc deviations that apply to the whole file.

Its routes are still drafted against models `model.py` does not define
(`ActivityProposal`, `ActivityProposalParticipation`, `ActivityProposalStatus`,
`ActivityProposalJoinRequest`, `ActivityProposalMessage`, `Meetup`), so they
raise `AttributeError` at request time. Two tests in `tests/test_auth.py` are
marked `xfail(strict=True)` for that reason and will start failing — on
purpose — the moment the Activity model lands.

The rules those routes enforce live in `app/activity_rules.py` instead of
inline, which is what the design doc specifies (`ActivityValidator`,
`ActivityStatusMachine`, `RolePolicy`, `EligibilityChecker`) and what makes
them testable without a database per NFR-SOFT-002. `tests/test_activity_rules.py`
covers them with 49 tests and no app context.

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
| FR-ACE-004 | Activity field validation | `activity_rules.ActivityValidator` |
| FR-ACE-007 | Role permission matrix | `activity_rules.RolePolicy` |
| FR-ACE-009 | Status transitions | `activity_rules.ActivityStatusMachine` |
| FR-PE-003…009 | Join eligibility | `activity_rules.EligibilityChecker` |
| FR-AUTH-013 | Filterable university dropdown | `app/universities.py`, `auth.university_options`, `static/js/signup2.js` |
| FR-AUTH-014 | Email verification before access | Clerk; gated by `VERIFICATION_EXEMPT` in `auth.require_authentication` |
| FR-AUTH-015 | Password complexity | `auth_password.*`, asserted by `scripts/check_clerk_config.py` |
| FR-AUTH-016 | Google OAuth | Clerk social connection; `auth.oauth_google_callback` |
| FR-AUTH-017 | Deletion and GDPR export | `AccountDeletionService`, `DataExportService` |
| FR-AUTH-018 | Rate limiting | Clerk for sign-in/sign-up; `app/ratelimit.py` for our own endpoints |
| FR-PROF-009 | Cross-university isolation | `main._university_scoped`, plus RLS policies in migration `4de96d5e2e95` |

## Tests

```bash
.venv/bin/python -m pytest tests -q
```

The suite stubs the Clerk adapter, so it runs without Clerk credentials.
`tests/test_clerk_adapter.py` additionally asserts that the SDK symbols the
adapter calls still exist, and `tests/test_clerk_settings.py` covers the
instance-settings checker.

## Schema changes

Managed by Flask-Migrate. The schema is **not** created by `create_all` any
more, because that creates missing tables and never alters existing ones — a
model change then fails on a database that already has the old tables.

```bash
.venv/bin/flask --app app db upgrade                     # apply migrations
.venv/bin/flask --app app db migrate -m "what changed"   # after editing model.py
.venv/bin/flask --app app db upgrade                     # then apply it
.venv/bin/flask --app app seed-universities              # FR-AUTH-013 reference data
```

Always read the generated migration before applying it; autogenerate misses
CHECK constraint changes and renames (it sees a rename as a drop plus an add,
which loses data).

`tests/test_migrations.py` fails if `model.py` and the migrations disagree, so
a forgotten `db migrate` is caught by the suite rather than in deployment.
`render_as_batch` is on, which SQLite needs for anything beyond adding a
column.

The local database is **`instance/roammate.sqlite`** — gitignored, created by
`db upgrade`.

## Database

PostgreSQL on Neon (project **RoamMate**, `curly-tooth-86278910`). The design
doc specifies PostgreSQL and the schema depends on it: the partial unique
indexes behind FR-ACE-007's "exactly one Admin" and FR-PE-002's "one pending
request" have no MySQL equivalent, and neither does the row-level security
NFR-SECU-010 calls for.

`neon link` wrote a gitignored `.neon` holding the org, project and branch, so
branch-scoped CLI commands need no flags. `neon env pull` writes two URLs:

| Variable | Endpoint | Used by |
|---|---|---|
| `DATABASE_URL` | `...-pooler...` | the application; `create_app` reads this |
| `DATABASE_URL_UNPOOLED` | direct | migrations; `migrations/env.py` prefers it |

That split matters. The pooled endpoint is PgBouncer in transaction mode and
keeps no session state, while Alembic takes advisory locks, issues
multi-statement DDL and relies on `SET`. A migration run through the pooler
fails in ways that never mention pooling.

The driver is **psycopg 3** (`psycopg[binary]`), not psycopg2: SQLAlchemy 2.1
resolves a bare `postgresql://` URL to psycopg 3, so Neon's connection string
works without rewriting its scheme.

Leave `DATABASE_URL` unset to fall back to SQLite at
`instance/roammate.sqlite`. Tests always use in-memory SQLite and never reach
Neon.

Neon's free tier suspends an idle compute after about 5 minutes; the first
request afterwards pays a cold start of a few hundred milliseconds. Worth
warming before a live demo.

### Row-level security (NFR-SECU-010)

Migration `4de96d5e2e95` enables RLS on the 17 university-scoped tables.
`app_current_university_id()` reads the `app.auth_provider_id` setting, which
`AuthService._apply_university_scope` publishes per transaction from the
verified Clerk token, and resolves it to a university. With the setting absent
no rows are visible, so unauthenticated access fails closed.

**RLS never applies to a table's owner**, and Neon's generated owner role also
has `BYPASSRLS`. So the policies only bite when the application connects as a
separate role:

```bash
APP_DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') \
  .venv/bin/python scripts/setup_app_role.py --write-env
```

`APP_DB_PASSWORD` is a **new** secret you generate, not any existing
credential: the script creates `roammate_app` (no superuser, no BYPASSRLS)
with that password, grants it per-table access, and `--write-env` points
`DATABASE_URL` at it. Without the flag the URL is only printed.

Re-running rotates the password, so any URL issued earlier stops working.

**Re-run it after every branch switch.** `neon env pull` — which `neon link`
and `neon checkout` both perform — resets `DATABASE_URL` to the owner role,
which silently makes RLS inert because the owner bypasses it. The symptom is
no error at all, just isolation that is no longer enforced. Check with:

```bash
psql "$DATABASE_URL" -c 'select current_user, (select rolbypassrls from pg_roles where rolname=current_user)'
```

`DATABASE_URL_UNPOOLED` stays on the owner: migrations have to bypass RLS, and
running as the owner is what lets them.

Verified on Neon: the app role sees only its own university's rows, 0 rows with
the setting unset, and a cross-university `INSERT` is refused by the policy's
`WITH CHECK`.

`_university_scoped()` in `main.py` stays. It is the application half; RLS is
what holds when a query forgets to call it.

### Branches

The schema is applied to the `production` branch. For ongoing work prefer a
branch per developer or per PR, so a migration meets production-like data
before production does:

```bash
neon checkout dev --create    # create, pin in .neon, pull its env
.venv/bin/flask --app app db upgrade
```

`neon checkout production` switches back. See the `neon-postgres-branches`
skill in `.claude/skills/`.

## Known gaps

- The rate limiter in `app/ratelimit.py` is in-process, so each worker keeps
  its own counter; a multi-worker deployment needs Redis behind it.
- `AccountDeletionService._leave_activities` expects a `ParticipationService`
  that does not exist yet, so deleting an account that admins an activity
  leaves that activity with no Admin (FR-ACE-007). The partial unique index
  caps admins at one; it cannot require one.
- Waitlists, invitations, admin succession, real-time messaging, message
  edit/delete and @mentions have tables but no code.
- The three `/activity/<id>/meetups` routes reference `model.Meetup`, which
  does not exist in the design doc's schema and so is not in `model.py`. They
  raise at request time. Either add a requirement and a table, or remove the
  routes and the calendar UI that calls them.
- `DataExportService` now exports profile, follows, memberships and messages,
  as the design specifies.
- No CSS yet: `roammate.css`, `roammate.js`, `css/signup.css`, `css/login.css`
  and `resources/logo.png` / `logo-full.png` are referenced by the templates
  but not in the repo, so pages render unstyled.
