"""SQLite persistence: access tokens, accounts, transactions, holdings.

The database lives in ~/.plutus alongside the credentials, not in the
checkout: it holds Plaid access tokens, so it is a credential itself and must
survive `git clean`.
"""
import sqlite3

from . import config, crypto

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id          TEXT PRIMARY KEY,
    access_token     TEXT NOT NULL,
    institution_id   TEXT,
    institution_name TEXT,
    env              TEXT NOT NULL,
    cursor           TEXT,
    linked_at        TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS accounts (
    account_id    TEXT PRIMARY KEY,
    item_id       TEXT,
    name          TEXT,
    official_name TEXT,
    type          TEXT,
    subtype       TEXT,
    mask          TEXT,
    current       REAL,
    available     REAL,
    currency      TEXT,
    updated_at    TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS transactions (
    transaction_id TEXT PRIMARY KEY,
    account_id     TEXT,
    date           TEXT,
    name           TEXT,
    merchant_name  TEXT,
    amount         REAL,
    currency       TEXT,
    category       TEXT,
    pending        INTEGER
);
CREATE TABLE IF NOT EXISTS investment_transactions (
    investment_transaction_id TEXT PRIMARY KEY,
    account_id    TEXT,
    security_id   TEXT,
    ticker        TEXT,
    name          TEXT,
    type          TEXT,
    subtype       TEXT,
    date          TEXT,
    quantity      REAL,
    price         REAL,
    amount        REAL,
    fees          REAL,
    currency      TEXT
);
CREATE INDEX IF NOT EXISTS idx_invtxn_date ON investment_transactions (date);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS holdings (
    account_id    TEXT,
    security_id   TEXT,
    ticker        TEXT,
    security_name TEXT,
    type          TEXT,
    quantity      REAL,
    price         REAL,
    value         REAL,
    cost_basis    REAL,
    currency      TEXT,
    as_of         TEXT DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (account_id, security_id)
);
"""


def connect():
    config.ensure_home()
    created = not config.db_path().exists()
    conn = sqlite3.connect(config.db_path())
    if created:
        config.restrict(config.db_path())
    conn.row_factory = sqlite3.Row
    # The dashboard reads on request threads while a background sync writes;
    # WAL lets those overlap, and the timeout absorbs the brief write locks.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    return conn


def save_item(conn, item_id, access_token, institution_id, institution_name, env):
    conn.execute(
        """INSERT INTO items (item_id, access_token, institution_id, institution_name, env)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET access_token = excluded.access_token""",
        (item_id, crypto.encrypt(access_token), institution_id, institution_name, env),
    )
    conn.commit()


def items(conn, env):
    """Linked Items as dicts, with access tokens decrypted for use.

    Rows written before encryption existed are upgraded in place the first
    time they are read, so no explicit migration step is needed.
    """
    rows = conn.execute(
        "SELECT * FROM items WHERE env = ? ORDER BY linked_at", (env,)).fetchall()
    out, upgrade = [], []
    for row in rows:
        item = dict(row)
        stored = item["access_token"]
        if not crypto.is_encrypted(stored):
            upgrade.append((crypto.encrypt(stored), item["item_id"]))
        else:
            item["access_token"] = crypto.decrypt(stored)
        out.append(item)
    if upgrade:
        conn.executemany("UPDATE items SET access_token = ? WHERE item_id = ?", upgrade)
        conn.commit()
    return out


def set_cursor(conn, item_id, cursor):
    conn.execute("UPDATE items SET cursor = ? WHERE item_id = ?", (cursor, item_id))
    conn.commit()


def remove_item(conn, item_id):
    """Purge an Item and everything hanging off it."""
    ids = [r["account_id"] for r in
           conn.execute("SELECT account_id FROM accounts WHERE item_id = ?", (item_id,))]
    if ids:
        qs = ",".join("?" * len(ids))
        conn.execute("DELETE FROM transactions WHERE account_id IN ({})".format(qs), ids)
        conn.execute("DELETE FROM holdings WHERE account_id IN ({})".format(qs), ids)
        conn.execute("DELETE FROM accounts WHERE account_id IN ({})".format(qs), ids)
    conn.execute("DELETE FROM items WHERE item_id = ?", (item_id,))
    conn.commit()


def get_setting(conn, key, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute(
        """INSERT INTO settings (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (key, str(value)),
    )
    conn.commit()
