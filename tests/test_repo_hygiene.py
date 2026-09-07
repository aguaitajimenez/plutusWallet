"""The repository must stay safe to publish.

These tests are about the repository rather than the program: they fail if a
credential, a database, or a personal path is ever committed. The same scanner
runs as a pre-commit hook, so this is the backstop for a commit made with
--no-verify or on a machine where the hook was never enabled.
"""
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent


def tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def test_no_secrets_in_any_tracked_file():
    result = subprocess.run(
        [sys.executable, "scripts/check_secrets.py", "--all"],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_data_or_credential_files_are_tracked():
    bad = [f for f in tracked()
           if f.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3"))
           or pathlib.PurePosixPath(f).name in {"credentials", "keyfile",
                                                "session_key", ".env"}]
    assert bad == [], "these must never be committed: {}".format(bad)


def test_gitignore_covers_the_dangerous_paths():
    patterns = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for needed in ("*.db", ".env", "__pycache__"):
        assert needed in patterns, "missing from .gitignore: {}".format(needed)


def test_no_developer_paths_or_addresses_leak():
    """Absolute home paths and e-mail addresses identify the author's machine."""
    import re
    email = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
    for name in tracked():
        path = ROOT / name
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        assert "C:\\Users\\" not in text, "Windows home path in {}".format(name)
        assert not re.search(r"/home/[a-z]", text), "POSIX home path in {}".format(name)
        found = [m for m in email.findall(text)
                 if not m.endswith(("example.com", "example.org", "plaid.com",
                                    "noreply@anthropic.com"))]
        assert not found, "e-mail address in {}: {}".format(name, found)


def test_the_precommit_hook_is_available_to_clone_users():
    hook = ROOT / "scripts" / "githooks" / "pre-commit"
    assert hook.is_file(), "the hook must be committed, not just installed locally"
    assert "check_secrets.py" in hook.read_text(encoding="utf-8")
