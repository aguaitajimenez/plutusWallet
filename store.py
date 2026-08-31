"""SQLite persistence: access tokens, accounts, transactions, holdings."""
import pathlib
import sqlite3

DB_PATH = pathlib.Path(__file__).parent / "plaid_data.db"

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
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def save_item(conn, item_id, access_token, institution_id, institution_name, env):
    conn.execute(
        """INSERT INTO items (item_id, access_token, institution_id, institution_name, env)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(item_id) DO UPDATE SET access_token = excluded.access_token""",
        (item_id, access_token, institution_id, institution_name, env),
    )
    conn.commit()


def items(conn, env):
    return conn.execute("SELECT * FROM items WHERE env = ? ORDER BY linked_at", (env,)).fetchall()


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
