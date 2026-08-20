import logging
import os
import secrets
import time
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

logger = logging.getLogger(__name__)

security = HTTPBasic()

# Methods that can't mutate state -- a forged cross-site GET has nothing to
# gain, so require_same_origin below only inspects the rest.
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")

# In-process brute-force throttle. HTTP Basic offers unlimited online guessing
# otherwise, and this app is the only thing standing between a LAN device and
# the whole dashboard. Per-client sliding window: more than _MAX_FAILURES failed
# attempts within _WINDOW_SECONDS locks that client out until the failures age
# out. In-memory (no dependency, no shared store) is proportionate for a
# single-worker LAN deployment -- a throttle, not a hard security boundary, so
# the unlocked dict access under the threadpool is acceptable. Cleared on a
# successful login; stale single-failure entries age out of the window on read.
_MAX_FAILURES = 10
_WINDOW_SECONDS = 300
_failures: dict[str, list[float]] = {}


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def require_admin(
    request: Request,
    credentials: HTTPBasicCredentials = Depends(security),
) -> str:
    expected_user = os.environ.get("APP_ADMIN_USER", "admin")
    expected_password = os.environ.get("APP_ADMIN_PASSWORD", "")

    client_ip = _client_ip(request)
    now = time.monotonic()
    recent = [t for t in _failures.get(client_ip, []) if now - t < _WINDOW_SECONDS]

    # Opportunistic global sweep, piggybacked on every request: an IP that
    # fails once and never returns would otherwise sit in this dict forever
    # -- only a *successful* login (below) or hitting the throttle branch
    # prunes anything, and neither touches an IP whose failures have
    # already all aged out. Cheap: this dict never holds more entries than
    # distinct client IPs that have ever failed a login, which on this
    # app's LAN-only deployment is naturally small.
    for ip in [k for k, ts in _failures.items() if all(now - t >= _WINDOW_SECONDS for t in ts)]:
        _failures.pop(ip, None)

    if len(recent) >= _MAX_FAILURES:
        _failures[client_ip] = recent  # keep the pruned window; don't grow it
        logger.warning(
            "auth: %d failed attempts from %s within %ds -- throttling",
            len(recent), client_ip, _WINDOW_SECONDS,
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed authentication attempts. Try again later.",
            headers={"Retry-After": str(_WINDOW_SECONDS)},
        )

    # secrets.compare_digest("", "") is True -- an unset/blank
    # APP_ADMIN_PASSWORD must never be treated as "matches anything empty".
    # app/main.py's startup check already refuses to boot in this case; this
    # is defence in depth for any entry point that skips that hook.
    #
    # Compare on UTF-8 bytes, not str: secrets.compare_digest raises TypeError
    # on a non-ASCII str, which for an attacker-supplied username would surface
    # as an unauthenticated 500 rather than a clean 401.
    user_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"), expected_user.encode("utf-8")
    )
    password_ok = bool(expected_password) and secrets.compare_digest(
        credentials.password.encode("utf-8"), expected_password.encode("utf-8")
    )

    if not (user_ok and password_ok):
        recent.append(now)
        _failures[client_ip] = recent
        logger.warning(
            "auth: failed login for user %r from %s (%d failure(s) in window)",
            credentials.username, client_ip, len(recent),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )

    _failures.pop(client_ip, None)  # a good login clears that client's slate
    return credentials.username


def require_same_origin(request: Request) -> None:
    """CSRF guard for the form/JSON routers behind require_admin. HTTP Basic
    has no session cookie to hang a SameSite policy on, so a browser will
    happily replay saved Basic credentials on a cross-site POST -- a page
    open in another tab could otherwise submit /assets/{id}/delete or
    /discovery/run/nmap. Reject any unsafe-method request that a browser marks
    as cross-origin.

    Prefer Sec-Fetch-Site: the browser sets it and page JavaScript cannot
    forge or strip it, so it closes the gap where an attacker page suppresses
    the Origin header. Fall back to comparing Origin (or Referer) against Host
    for the older clients that don't send it.

    A request with none of these headers is let through: that's what a bare
    curl/API client sends (and this app has no other CSRF-relevant state for
    one to forge), not what a browser navigation or form submission omits."""
    if request.method in _SAFE_METHODS:
        return

    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site is not None:
        # same-origin: this app's own page. none: a direct navigation (e.g. the
        # browser address bar), not a cross-site trigger. Everything else
        # (cross-site, same-site) is a request another site initiated.
        if fetch_site in ("same-origin", "none"):
            return
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected",
        )

    header = request.headers.get("origin") or request.headers.get("referer")
    if header is None:
        return
    request_host = request.headers.get("host")
    header_host = urlparse(header).netloc
    if not header_host or header_host != request_host:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cross-origin request rejected",
        )
