#!/usr/bin/env python3
"""Refuse to let a credential reach the repository.

Run over the staged changes by the pre-commit hook, and over every tracked
file by the test suite:

    python scripts/check_secrets.py            # staged changes only
    python scripts/check_secrets.py --all      # every tracked file

Exits non-zero, and names the file and line, when it finds anything that looks
like a real secret. The patterns are deliberately shaped to match real values
and not the placeholders the code and tests legitimately contain: a Plaid
access token is only flagged when the prefix is followed by a UUID, so
`access-sandbox-bank` in a fixture stays quiet while a live token does not.
"""
import argparse
import re
import subprocess
import sys

# Files that must never be committed, whatever they contain.
FORBIDDEN_NAMES = re.compile(
    r"(^|/)(credentials|keyfile|\.env)$|\.(db|db-wal|db-shm|sqlite3?)$")

PATTERNS = [
    ("Plaid access token",
     re.compile(r"access-(?:sandbox|production|development)-"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")),
    ("Plaid public/link token",
     re.compile(r"(?:public|link)-(?:sandbox|production|development)-"
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}")),
    # No leading \b: underscore is a word character, so \bsecret\b would miss
    # the PLAID_SECRET_PRODUCTION= form the credentials file actually uses.
    ("Plaid secret assignment",
     re.compile(r"(?i)secret[a-z_]*\s*[=:]\s*[\"']?[0-9a-f]{28,34}\b")),
    ("Plaid client_id assignment",
     re.compile(r"(?i)client_?id[a-z_]*\s*[=:]\s*[\"']?[0-9a-f]{20,26}\b")),
    # A Plaid secret is exactly 30 hex characters; a bare one on its own line
    # is the shape you get from pasting a key into a file.
    ("bare 30-hex value (Plaid secret shape)",
     re.compile(r"^[\s\"']*[0-9a-f]{30}[\s\"',]*$")),
    ("Fernet/encryption key",
     re.compile(r"\bgAAAAA[A-Za-z0-9_\-=]{20,}")),
    ("Private key block",
     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----")),
]

# This file necessarily contains the patterns it searches for.
SELF = "scripts/check_secrets.py"


def tracked_files():
    out = subprocess.run(["git", "ls-files"], capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def staged_files():
    out = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                         capture_output=True, text=True)
    return [f for f in out.stdout.splitlines() if f]


def scan(paths):
    findings = []
    for path in paths:
        if path == SELF:
            continue
        if FORBIDDEN_NAMES.search(path):
            findings.append((path, 0, "file must never be committed", path))
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if len(line) > 4000:        # minified or binary-ish
                        line = line[:4000]
                    for label, pattern in PATTERNS:
                        match = pattern.search(line)
                        if match:
                            findings.append((path, lineno, label, match.group(0)[:60]))
        except (OSError, UnicodeDecodeError):
            continue
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--all", action="store_true",
                    help="scan every tracked file instead of staged changes")
    args = ap.parse_args()

    paths = tracked_files() if args.all else staged_files()
    findings = scan(paths)

    if not findings:
        print("check_secrets: {} file(s) scanned, nothing found".format(len(paths)))
        return 0

    print("check_secrets: REFUSING - possible credentials found\n", file=sys.stderr)
    for path, lineno, label, sample in findings:
        where = "{}:{}".format(path, lineno) if lineno else path
        print("  {:<40} {}".format(where, label), file=sys.stderr)
        if lineno:
            print("  {:<40} {!r}".format("", sample), file=sys.stderr)
    print("\nSecrets belong in ~/.plutus, never in the repository.",
          file=sys.stderr)
    print("If this is a false positive, adjust PATTERNS in {}.".format(SELF),
          file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
