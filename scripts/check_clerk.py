#!/usr/bin/env python
"""Check the Clerk wiring before trusting the auth requirements.

    .venv/bin/python scripts/check_clerk.py

Verifies that the configured keys work and prints the dashboard settings the
requirements depend on. Those settings are not readable through the Backend
API, so they are a manual checklist rather than an assertion.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app                      # noqa: E402
from app.providers import CLERK_REQUIRED_SETTINGS, clerk_js_url  # noqa: E402


def main():
    app = create_app()

    problems = []
    with app.app_context():
        secret = app.config.get("CLERK_SECRET_KEY")
        publishable = app.config.get("CLERK_PUBLISHABLE_KEY")

        if not secret:
            problems.append("CLERK_SECRET_KEY is not set")
        if not publishable:
            problems.append("CLERK_PUBLISHABLE_KEY is not set")

        if publishable and not clerk_js_url(publishable):
            problems.append("CLERK_PUBLISHABLE_KEY is not a well-formed Clerk key")
        elif publishable:
            print(f"clerk-js URL: {clerk_js_url(publishable)}")

        if secret:
            try:
                from clerk_backend_api import Clerk
                users = Clerk(bearer_auth=secret).users.list(request={"limit": 1})
                print(f"Backend API reachable; {len(users or [])} user(s) returned by a limit-1 list")
            except Exception as error:
                problems.append(f"Backend API call failed: {error}")

    print("\nDashboard settings this application assumes:")
    for setting, expected in CLERK_REQUIRED_SETTINGS.items():
        print(f"  [ ] {setting} = {expected}")

    if problems:
        print("\nProblems:")
        for problem in problems:
            print(f"  - {problem}")
        return 1

    print("\nKeys look good.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
