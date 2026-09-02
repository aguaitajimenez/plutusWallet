"""Encryption at rest for the Plaid access tokens held in the database.

An access token is a live credential: whoever holds it can read the linked
bank accounts until the Item is removed. The database file therefore travels
with the same risk as a password file, and it is easy to leak by accident -
a backup tool, a cloud-synced home directory, a support bundle.

The encryption key lives in the operating system's credential store (Windows
Credential Manager, macOS Keychain, Secret Service on Linux) rather than beside
the data, so a copied database is inert on any other machine or account.

Where no OS keystore exists - a headless server, a locked-down container - we
fall back to a key file next to the database and say so plainly. That still
defeats casual disclosure through backups, but an attacker who can read the
database directory can also read the key. It is defence in depth, not a
guarantee, and `key_source()` reports which of the two is in play.
"""
import logging
import os

from cryptography.fernet import Fernet, InvalidToken

from . import config

log = logging.getLogger("plutus")

SERVICE = "PlutusTracker"
KEY_NAME = "db-encryption-key"
PREFIX = "enc1:"          # version tag, so the format can change later

_cached_key = None
_source = "uninitialised"


def key_source():
    """'keyring', 'file', or 'uninitialised' - which protection is active."""
    return _source


def _keyfile():
    return config.home() / "keyfile"


def _load_from_keyring():
    try:
        import keyring
        existing = keyring.get_password(SERVICE, KEY_NAME)
        if existing:
            return existing.encode("utf-8")
        key = Fernet.generate_key()
        keyring.set_password(SERVICE, KEY_NAME, key.decode("utf-8"))
        return key
    except Exception as exc:  # no backend, locked keychain, dbus missing...
        log.debug("OS keyring unavailable: %s", exc)
        return None


def _load_from_file():
    path = _keyfile()
    try:
        if path.is_file():
            return path.read_bytes().strip()
        config.ensure_home()
        key = Fernet.generate_key()
        path.write_bytes(key)
        config.restrict(path)
        log.warning(
            "No OS keyring available; the database key is stored at %s. "
            "Anyone who can read that directory can read your tokens.", path)
        return key
    except OSError as exc:
        log.error("Cannot create an encryption key (%s); tokens stay in plaintext.", exc)
        return None


def _key():
    global _cached_key, _source
    if _cached_key is not None:
        return _cached_key
    if os.environ.get("PLUTUS_NO_KEYRING"):   # tests, and locked-down installs
        key = _load_from_file()
        _source = "file" if key else "none"
    else:
        key = _load_from_keyring()
        _source = "keyring"
        if key is None:
            key = _load_from_file()
            _source = "file" if key else "none"
    _cached_key = key
    return key


def _reset_for_tests():
    global _cached_key, _source
    _cached_key, _source = None, "uninitialised"


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    """Encrypt a token. Returns it unchanged if no key could be obtained."""
    if value is None or is_encrypted(value):
        return value
    key = _key()
    if not key:
        return value
    return PREFIX + Fernet(key).encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value):
    """Decrypt a token; plaintext (pre-encryption rows) passes straight through."""
    if not is_encrypted(value):
        return value
    key = _key()
    if not key:
        raise RuntimeError(
            "This database holds encrypted tokens but no key is available. "
            "If the OS keyring was reset, re-link your institutions.")
    try:
        return Fernet(key).decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise RuntimeError(
            "Stored tokens cannot be decrypted with this machine's key - the "
            "database was probably copied from another machine or account. "
            "Re-link your institutions to continue.")
