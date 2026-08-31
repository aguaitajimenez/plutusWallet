"""Thin wrapper over Plaid's REST API.

Plain `requests` rather than plaid-python: the JSON API is stable, while the
typed SDK's model imports change shape between releases and buy us nothing at
this size.

Credentials live in ~/.plutusTracker, written by the Configure option in
app.py. A project .env is still honored as a fallback.
"""
import os
import pathlib

import requests
from dotenv import load_dotenv

CONFIG_PATH = pathlib.Path.home() / ".plutusTracker"

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
        text = CONFIG_PATH.read_text(encoding="utf-8")
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
    lines = ["# PlutusTracker credentials - treat this file as a password.", ""]
    lines += ["{}={}".format(k, cfg[k]) for k in KEYS if cfg.get(k)]
    CONFIG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:  # best effort; Windows ACLs don't map onto POSIX modes
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


def reload():
    """Re-resolve ENV/CLIENT_ID/SECRET. Call after saving new credentials."""
    global ENV, CLIENT_ID, SECRET
    load_dotenv()  # project .env, for setups predating ~/.plutusTracker
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
    """Store the pair in ~/.plutusTracker and make `env` active."""
    cfg = _read_config()
    cfg["PLAID_CLIENT_ID"] = client_id
    cfg["PLAID_SECRET_{}".format(env.upper())] = secret
    cfg["PLAID_ENV"] = env
    _write_config(cfg)
    return reload()


def clear_credentials():
    """Forget stored credentials. Linked Items in the database are untouched."""
    try:
        CONFIG_PATH.unlink()
    except OSError:
        pass
    return reload()


def call(path, **body):
    if ENV not in HOSTS:
        raise RuntimeError("PLAID_ENV must be 'sandbox' or 'production', got {!r}".format(ENV))
    if not CLIENT_ID or not SECRET:
        raise RuntimeError(
            "No Plaid credentials for {} - run 'python app.py' and choose "
            "[c] Configure.".format(ENV)
        )
    resp = requests.post(
        HOSTS[ENV] + path,
        json={"client_id": CLIENT_ID, "secret": SECRET, **body},
        timeout=30,
    )
    if resp.status_code != 200:
        try:
            raise PlaidError(resp.json())
        except ValueError:
            resp.raise_for_status()
    return resp.json()


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
