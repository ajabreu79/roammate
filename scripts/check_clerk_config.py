#!/usr/bin/env python
"""Assert the Clerk instance settings the requirements depend on.

The Backend API does not expose instance settings for reading, so the config
comes from the authenticated CLI:

    npx -y clerk@latest config pull | .venv/bin/python scripts/check_clerk_config.py

Exits non-zero if any setting in providers.CLERK_REQUIRED_SETTINGS is wrong,
which makes it usable as a pre-submission gate.
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.providers import check_settings      # noqa: E402


def main():
    raw = sys.stdin.read()
    if not raw.strip():
        print("No config on stdin. Pipe `npx -y clerk@latest config pull` into this script.")
        return 2

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        print(f"Could not parse the config as JSON: {error}")
        return 2

    results = check_settings(config)
    width = max(len(path) for path, *_ in results)

    failures = 0
    for path, requirement, actual, expected, ok in results:
        if not ok:
            failures += 1
        status = "PASS" if ok else "FAIL"
        detail = f"= {actual}" if ok else f"= {actual}, want {expected}"
        print(f"{status:5} {requirement:12} {path:{width}} {detail}")

    print()
    if failures:
        print(f"{failures} of {len(results)} settings are wrong. Fix them with:")
        print("  npx -y clerk@latest config patch --file <patch.json> --dry-run")
        return 1

    print(f"All {len(results)} required Clerk settings are correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
