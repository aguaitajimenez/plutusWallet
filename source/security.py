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
  * Response headers - no sniffing, no framing, no referrer leakage.

There is deliberately no login. The threat being defended against is a hostile
*web page*, not a hostile *user*: anyone with an account on this machine can
read the database directly, and a password in front of a localhost page would
not change that.
"""
import logging
import secrets
from urllib.parse import urlparse

from flask import abort, request, session

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
    if not origin:
        return True          # curl and same-origin form posts may omit it
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    return bool(parsed.netloc) and parsed.netloc == app_host


def harden(app, csp=CSP_STRICT):
    """Apply the guards above to a Flask app. Call once, at import time."""
    # Per-process key: sessions intentionally do not survive a restart, and
    # nothing of value is stored in them beyond the CSRF token.
    app.secret_key = secrets.token_urlsafe(32)

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
                abort(403)

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("Content-Security-Policy", csp)
        resp.headers.setdefault("Cache-Control", "no-store")
        return resp

    @app.context_processor
    def _inject_csrf():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"]}

    return app
