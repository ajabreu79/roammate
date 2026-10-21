import threading
import time
from collections import defaultdict, deque
from functools import wraps

from flask import flash, redirect, request, url_for

# FR-AUTH-018. This is an in-process sliding window: correct for a single
# worker, which is what the dev server and a single gunicorn worker give us.
# A multi-worker deployment needs a shared backend (Flask-Limiter on Redis)
# because each worker would otherwise keep its own independent counter.
_hits = defaultdict(deque)
_lock = threading.Lock()


def _client_key(scope):
    return (scope, request.headers.get("X-Forwarded-For", request.remote_addr or "unknown"))


def _too_many(scope, limit, window_seconds):
    key = _client_key(scope)
    cutoff = time.monotonic() - window_seconds
    with _lock:
        hits = _hits[key]
        while hits and hits[0] < cutoff:
            hits.popleft()
        if len(hits) >= limit:
            return True
        hits.append(time.monotonic())
        return False


def reset():
    """Clear all counters. Used by tests."""
    with _lock:
        _hits.clear()


def rate_limit(scope, limit, window_seconds, redirect_endpoint):
    """Reject more than `limit` requests per `window_seconds` from one client."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if _too_many(scope, limit, window_seconds):
                flash("Too many attempts. Please wait a moment and try again.")
                # Redirect rather than a bare 429 so the flash message is what
                # the user actually sees on the form they just submitted.
                return redirect(url_for(redirect_endpoint))
            return view(*args, **kwargs)

        return wrapper

    return decorator
