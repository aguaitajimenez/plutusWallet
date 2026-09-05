"""HTTP hardening for the two local servers.

Binding to loopback is not the same as being private. Any web page the user
happens to have open can issue requests to http://localhost:8001 - the browser
sends them happily, and for a plain form POST it does not even ask permission
first. A DNS rebinding attack goes further and makes a hostile origin look
same-origin. So a local server still needs the same defences as a public one:

  * Host allow-list  - the request must be addressed to a loopback name,
                       which is what breaks DNS rebinding.
  * CSRF token       - every state-changing request must carry a secret this
                       server issued, which a cross-origin page cannot read.
  * Origin check     - belt and braces for browsers that send the header.
  * Response headers - no sniffing, no framing, no referrer sent off-site.

There is deliberately no login. The threat being defended against is a hostile
*web page*, not a hostile *user*: anyone with an account on this machine can
read the database directly, and a password in front of a localhost page would
not change that.
"""
import logging
import secrets
from urllib.parse import urlparse

from flask import Response, abort, request, session

from . import config

log = logging.getLogger("plutus")

LOOPBACK_NAMES = {"localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# The dashboard serves only its own inline CSS/JS, so it can be locked down
# hard. The link server has to load Plaid Link from Plaid's CDN and let it
# open its own frames, so it gets a wider policy rather than a broken page.
CSP_STRICT = ("default-src 'none'; script-src 'self' 'unsafe-inline'; "
              "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
              "connect-src 'self'; form-action 'self'; base-uri 'none'; "
              "frame-ancestors 'none'")
CSP_LINK = ("default-src 'self'; script-src 'self' 'unsafe-inline' "
            "https://cdn.plaid.com; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; connect-src 'self' https://*.plaid.com; "
            "frame-src https://cdn.plaid.com https://*.plaid.com; "
            "form-action 'self'; base-uri 'none'; frame-ancestors 'none'")


def host_is_local(host_header):
    """True when the request was addressed to a loopback name."""
    if not host_header:
        return False
    name = host_header.rsplit(":", 1)[0] if ":" in host_header else host_header
    if name.startswith("[") and "]" in host_header:      # IPv6 literal
        name = host_header[:host_header.index("]") + 1]
    return name.lower() in LOOPBACK_NAMES


def safe_redirect_target(value, fallback="/"):
    """Only allow same-site paths. Rejects '//evil.com' and '/\\evil.com',
    which browsers treat as absolute URLs despite the leading slash."""
    if not value or not value.startswith("/"):
        return fallback
    if value.startswith("//") or value.startswith("/\\"):
        return fallback
    return value


def _origin_ok(app_host):
    origin = request.headers.get("Origin") or request.headers.get("Referer")
    if not origin or origin == "null":
        # Absent, or opaque. Browsers send "null" for a form post whose
        # referrer policy hid the origin - which this app's own pages used to
        # do, so every save it offered was refused as cross-site. Either way
        # there is nothing to compare, and the CSRF token below is the check
        # that actually stops a hostile page: it cannot read the token.
        return True
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == app_host


STALE_PAGE = """<!doctype html><meta charset=utf-8><title>Page went stale</title>
<style>body{font:15px/1.6 system-ui,sans-serif;max-width:34rem;
margin:4rem auto;padding:0 1rem;background:#fff;color:#222}
h1{font-size:1.15rem}a{color:#06c}
@media (prefers-color-scheme:dark){body{background:#141414;color:#e6e6e6}
a{color:#6cf}}</style>
<h1>This page went stale</h1>
<p>Your change was <b>not</b> saved. This page was loaded before the server
   last started, so the security token on its form is no longer the one the
   server expects.</p>
<p><a href="/">Reload the dashboard</a>, then make the change again.</p>
"""


def _session_key():
    """A signing key for the session cookie, stable across restarts.

    It has to persist, and both servers have to agree on it. Cookies ignore
    the port, so the dashboard on 8001 and the link server on 8000 share a
    single cookie on localhost; with a key each, every visit to one of them
    silently invalidated the other's CSRF token and the next save came back
    403. A restart did the same to any tab left open.

    The cookie carries nothing but the CSRF token, and the file is owner-only
    beside the database, so keeping it costs no secrecy that is not already
    lost if someone can read that directory.
    """
    path = config.session_key_path()
    try:
        if path.is_file():
            existing = path.read_text(encoding="ascii").strip()
            if existing:
                return existing
        config.ensure_home()
        key = secrets.token_urlsafe(32)
        path.write_text(key, encoding="ascii")
        config.restrict(path)
        return key
    except OSError as exc:
        # Not fatal: the server still runs, but a tab left open will need a
        # reload after a restart.
        log.warning("cannot persist the session key (%s); open pages will "
                    "need a reload after a restart", exc)
        return secrets.token_urlsafe(32)


def _stale_token_response():
    """Refuse the request, but tell a legitimate user how to recover.

    A bare 403 is right for an attack and useless for the far commoner case:
    a real user on a page older than the server. The request is still
    rejected and no token is handed out - only the wording changes.
    """
    return Response(STALE_PAGE, status=403,
                    content_type="text/html; charset=utf-8")


def harden(app, csp=CSP_STRICT):
    """Apply the guards above to a Flask app. Call once, at import time."""
    app.secret_key = _session_key()

    @app.before_request
    def _guard():
        if not host_is_local(request.host):
            log.warning("rejected request for non-loopback host %r", request.host)
            abort(403)
        if request.method in UNSAFE_METHODS:
            if not _origin_ok(request.host):
                log.warning("rejected cross-origin %s %s", request.method, request.path)
                abort(403)
            expected = session.get("csrf")
            supplied = (request.form.get("csrf")
                        or request.headers.get("X-CSRF-Token")
                        or (request.get_json(silent=True) or {}).get("csrf"))
            if not expected or not supplied or not secrets.compare_digest(
                    str(expected), str(supplied)):
                log.warning("rejected %s %s: bad CSRF token", request.method, request.path)
                return _stale_token_response()

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def _inject_csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"]}

    return app
