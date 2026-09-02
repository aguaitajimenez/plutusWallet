"""Thin wrapper over Plaid's REST API.

Plain `requests` rather than plaid-python: the JSON API is stable, while the
typed SDK's model imports change shape between releases and buy us nothing at
this size.

Credentials live in ~/.plutus/credentials, written by the Configure option in
app.py. Environment variables of the same names override the file, which is
what CI and the test suite use.
"""
import logging
import os
import time

import requests

from . import config

log = logging.getLogger("plutus")

# Transient failures worth retrying rather than surfacing to the user.
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRIES = 3
BACKOFF_S = 1.5
TIMEOUT_S = 45

HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}
KEYS = ("PLAID_CLIENT_ID", "PLAID_SECRET_SANDBOX", "PLAID_SECRET_PRODUCTION",
        "PLAID_ENV")

# Resolved by reload() at import, and again whenever credentials are saved.
ENV = "sandbox"
CLIENT_ID = None
SECRET = None


class PlaidError(Exception):
    def __init__(self, payload):
        self.code = payload.get("error_code", "UNKNOWN")
        self.message = payload.get("error_message", "")
        super().__init__("{}: {}".format(self.code, self.message))


def _read_config():
    """Parse the KEY=VALUE credential file; a missing file is not an error."""
    data = {}
    try:
        text = config.credentials_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return data
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip()
    return data


def _write_config(cfg):
    config.ensure_home()
    lines = ["# PlutusTracker credentials - treat this file as a password.", ""]
    lines += ["{}={}".format(k, cfg[k]) for k in KEYS if cfg.get(k)]
    path = config.credentials_path()
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    config.restrict(path)


def reload():
    """Re-resolve ENV/CLIENT_ID/SECRET. Call after saving new credentials."""
    global ENV, CLIENT_ID, SECRET
    cfg = _read_config()

    def get(key):
        return (cfg.get(key) or os.getenv(key) or "").strip()

    ENV = (get("PLAID_ENV") or "sandbox").lower()
    CLIENT_ID = get("PLAID_CLIENT_ID") or None
    SECRET = get("PLAID_SECRET_{}".format(ENV.upper())) or None
    return ENV


def have_credentials():
    return bool(CLIENT_ID and SECRET)


def detect_env(client_id, secret):
    """Which environment a secret belongs to, found by minting a throwaway
    link token. Returns (env, detail); env is None when neither accepts it.
    Creates no Item and costs nothing.
    """
    detail = None
    for env, host in HOSTS.items():
        try:
            resp = requests.post(
                host + "/link/token/create",
                json={"client_id": client_id, "secret": secret,
                      "user": {"client_user_id": "local-user"},
                      "client_name": "PlutusTracker",
                      "products": ["transactions"],
                      "country_codes": ["US"], "language": "en"},
                timeout=30,
            )
        except requests.RequestException as exc:
            return None, "network error: {}".format(exc)
        if resp.status_code == 200:
            return env, None
        try:
            detail = resp.json().get("error_code", "HTTP {}".format(resp.status_code))
        except ValueError:
            detail = "HTTP {}".format(resp.status_code)
    return None, detail


def save_credentials(client_id, secret, env):
    """Store the pair in ~/.plutus/credentials and make `env` active."""
    cfg = _read_config()
    cfg["PLAID_CLIENT_ID"] = client_id
    cfg["PLAID_SECRET_{}".format(env.upper())] = secret
    cfg["PLAID_ENV"] = env
    _write_config(cfg)
    return reload()


def clear_credentials():
    """Forget stored credentials. Linked Items in the database are untouched."""
    try:
        config.credentials_path().unlink()
    except OSError:
        pass
    return reload()


def call(path, **body):
    """POST to Plaid, retrying transient failures.

    Rate limits and 5xx are worth a second try; a bad request or an expired
    login is not, so PlaidError propagates immediately for the caller to
    classify.
    """
    if ENV not in HOSTS:
        raise RuntimeError("PLAID_ENV must be 'sandbox' or 'production', got {!r}".format(ENV))
    if not CLIENT_ID or not SECRET:
        raise RuntimeError(
            "No Plaid credentials for {} - run 'python app.py' and choose "
            "[c] Configure.".format(ENV)
        )

    payload = {"client_id": CLIENT_ID, "secret": SECRET, **body}
    last_exc = None
    for attempt in range(1, RETRIES + 1):
        try:
            resp = requests.post(HOSTS[ENV] + path, json=payload, timeout=TIMEOUT_S)
        except requests.RequestException as exc:
            last_exc = exc
            log.warning("%s: network error (attempt %d/%d): %s",
                        path, attempt, RETRIES, exc)
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in RETRY_STATUS:
                try:
                    raise PlaidError(resp.json())
                except ValueError:
                    resp.raise_for_status()
            last_exc = None
            log.warning("%s: HTTP %s (attempt %d/%d)",
                        path, resp.status_code, attempt, RETRIES)
        if attempt < RETRIES:
            time.sleep(BACKOFF_S * attempt)

    if last_exc is not None:
        raise last_exc
    raise PlaidError({"error_code": "PLAID_UNAVAILABLE",
                      "error_message": "Plaid did not respond successfully after "
                                       "{} attempts".format(RETRIES)})


def institution_name(institution_id):
    if not institution_id:
        return "unknown"
    try:
        res = call(
            "/institutions/get_by_id",
            institution_id=institution_id,
            country_codes=["US"],
        )
        return res["institution"]["name"]
    except PlaidError:
        return institution_id


reload()
