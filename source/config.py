"""Filesystem layout, permissions and logging for PlutusTracker.

Everything user-specific lives in ~/.plutus so the checkout stays disposable:
clone the repo, run it, delete it, and no financial data or credential is lost.
Set PLUTUS_HOME to relocate it (the test suite does this).

    ~/.plutus/
        credentials       Plaid client_id and secrets
        plaid_data.db     accounts, transactions, holdings, settings
        backups/          rotating copies taken before each sync
        plutus.log        rotating application log
"""
import logging
import os
import shutil
import subprocess
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

APP_NAME = "PlutusTracker"
PROJECT_DIR = Path(__file__).resolve().parent.parent


def home():
    """Data directory. Read at call time so PLUTUS_HOME can be set late."""
    return Path(os.environ.get("PLUTUS_HOME") or (Path.home() / ".plutus"))


def credentials_path():
    return home() / "credentials"


def db_path():
    return home() / "plaid_data.db"


def backup_dir():
    return home() / "backups"


def log_path():
    return home() / "plutus.log"


def session_key_path():
    """Signs the session cookie that carries the CSRF token."""
    return home() / "session_key"


# Pre-1.0 locations, migrated on first run.
LEGACY_CREDENTIALS = Path.home() / ".plutusTracker"
LEGACY_DB = PROJECT_DIR / "plaid_data.db"

log = logging.getLogger("plutus")


def restrict(path):
    """Make a file or directory readable only by its owner.

    POSIX modes cover Linux and macOS; on Windows they are close to meaningless,
    so hand the job to icacls instead. Best effort either way - hardened
    permissions are a bonus, never a precondition for running.
    """
    try:
        if os.name == "nt":
            user = os.environ.get("USERNAME")
            if not user:
                return False
            grant = "{}:(OI)(CI)F".format(user) if path.is_dir() else "{}:F".format(user)
            subprocess.run(["icacls", str(path), "/inheritance:r", "/grant:r", grant],
                           capture_output=True, check=True, timeout=15)
        else:
            path.chmod(0o700 if path.is_dir() else 0o600)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def ensure_home():
    """Create ~/.plutus, lock it down, and migrate from the old layout.

    Migration copies rather than moves, so a failure leaves the original intact.
    Returns human-readable notes about anything it did.
    """
    notes = []
    root = home()
    fresh = not root.exists()
    root.mkdir(parents=True, exist_ok=True)
    backup_dir().mkdir(parents=True, exist_ok=True)
    if fresh:
        restrict(root)
        notes.append("created {}".format(root))

    # An explicitly relocated home is a deliberate, separate install - a test
    # run, a second profile - and must never inherit the default one's data.
    if os.environ.get("PLUTUS_HOME"):
        return notes

    if LEGACY_CREDENTIALS.is_file() and not credentials_path().exists():
        shutil.copy2(LEGACY_CREDENTIALS, credentials_path())
        restrict(credentials_path())
        notes.append("imported credentials from {}".format(LEGACY_CREDENTIALS))

    # The database carries access tokens, so it belongs beside the credentials
    # rather than inside a checkout someone might `git clean`.
    if LEGACY_DB.is_file() and not db_path().exists():
        shutil.copy2(LEGACY_DB, db_path())
        restrict(db_path())
        notes.append("imported database from {} (original left in place)".format(LEGACY_DB))

    return notes


def backup_db(keep=7):
    """Copy the database aside before a sync writes to it. Returns the path."""
    src = db_path()
    if not src.is_file():
        return None
    backup_dir().mkdir(parents=True, exist_ok=True)
    import datetime
    dest = backup_dir() / "plaid_data-{}.db".format(
        datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    try:
        shutil.copy2(src, dest)
        restrict(dest)
    except OSError as exc:
        log.warning("backup skipped: %s", exc)
        return None
    old = sorted(backup_dir().glob("plaid_data-*.db"))[:-keep]
    for path in old:
        try:
            path.unlink()
        except OSError:
            pass
    return dest


def serve(app, port, label):
    """Run a Flask app on a production WSGI server, bound to loopback only.

    Flask's built-in server is single-purpose development scaffolding and says
    so on startup. Waitress is pure Python, works the same on Windows, and does
    not pretend the app is reachable from anywhere but this machine - the
    explicit 127.0.0.1 bind is a security boundary, not a default.
    """
    from waitress import serve as _serve
    log.info("%s -> http://localhost:%d", label, port)
    _serve(app, host="127.0.0.1", port=port, threads=8,
           ident="PlutusTracker", clear_untrusted_proxy_headers=True)


class _Redactor(logging.Filter):
    """Keep secrets out of the log even when a traceback carries one."""

    PREFIXES = ("access-sandbox-", "access-production-", "public-sandbox-",
                "public-production-", "link-sandbox-", "link-production-")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        out = msg
        from . import plaid_api  # late: config must not import it at load
        for value in (plaid_api.SECRET, plaid_api.CLIENT_ID):
            if value and len(value) > 6 and value in out:
                out = out.replace(value, "<redacted>")
        for prefix in self.PREFIXES:
            idx = out.find(prefix)
            while idx != -1:
                end = idx + len(prefix)
                while end < len(out) and (out[end].isalnum() or out[end] == "-"):
                    end += 1
                out = out[:idx] + prefix + "<redacted>" + out[end:]
                idx = out.find(prefix, idx + len(prefix) + 10)
        if out != msg:
            record.msg, record.args = out, ()
        return True


def setup_logging(verbose=False):
    """Log to ~/.plutus/plutus.log and to the console. Idempotent."""
    if getattr(setup_logging, "_done", False):
        return log
    ensure_home()

    log.setLevel(logging.DEBUG)
    log.addFilter(_Redactor())

    try:
        handler = RotatingFileHandler(
            log_path(), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(message)s"))
        handler.setLevel(logging.DEBUG)
        log.addHandler(handler)
        restrict(log_path())
    except OSError as exc:  # a read-only home must not stop the app
        print("warning: file logging disabled ({})".format(exc), file=sys.stderr)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(console)

    setup_logging._done = True
    return log
