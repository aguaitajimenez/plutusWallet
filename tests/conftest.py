"""Test fixtures.

Two things must be true before any `source` module is imported, so they are
done at module scope rather than in a fixture: the data directory has to point
somewhere disposable, and the OS keyring must be left alone. Otherwise a test
run would read - and encrypt - the developer's real financial database.
"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="plutus-tests-")
os.environ["PLUTUS_HOME"] = _TMP
os.environ["PLUTUS_NO_KEYRING"] = "1"          # key goes to a file under _TMP
os.environ.pop("PLAID_CLIENT_ID", None)
os.environ.pop("PLAID_SECRET_SANDBOX", None)
os.environ.pop("PLAID_SECRET_PRODUCTION", None)
os.environ["PLAID_ENV"] = "sandbox"

import pytest  # noqa: E402

from source import config, plaid_api, store  # noqa: E402


TABLES = ("items", "accounts", "transactions", "investment_transactions",
          "holdings", "settings")


@pytest.fixture(autouse=True)
def clean_db(monkeypatch):
    """Empty tables per test, and no possibility of a real Plaid call.

    Rows are deleted rather than the file: Windows keeps a lock on an open
    SQLite database, and the WAL sidecars would survive a plain unlink anyway.
    """
    c = store.connect()
    for table in TABLES:
        c.execute("DELETE FROM {}".format(table))
    c.commit()
    c.close()

    def _no_network(*args, **kwargs):
        raise AssertionError("a test tried to reach Plaid: {}".format(args[:1]))

    monkeypatch.setattr(plaid_api, "call", _no_network)
    yield


@pytest.fixture
def conn():
    c = store.connect()
    yield c
    c.close()


@pytest.fixture
def linked(conn):
    """One bank Item and one brokerage Item, as a real install would have."""
    store.save_item(conn, "item_bank", "access-sandbox-bank",
                    "ins_bank", "Example Bank", "sandbox")
    store.save_item(conn, "item_brok", "access-sandbox-brok",
                    "ins_brok", "Example Brokerage", "sandbox")
    conn.executemany(
        "INSERT INTO accounts VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)",
        [("acc_check", "item_bank", "Checking", None, "depository", "checking",
          "0001", 5000.0, 5000.0, "USD"),
         ("acc_card", "item_bank", "Card", None, "credit", "credit card",
          "0002", 250.0, None, "USD"),
         ("acc_brok", "item_brok", "Brokerage", None, "investment", "brokerage",
          "0003", 10000.0, None, "USD")])
    conn.commit()
    return conn


def add_txns(conn, rows):
    """rows: (id, account_id, date, name, amount, category)"""
    conn.executemany(
        "INSERT INTO transactions VALUES (?,?,?,?,?,?,?,?,?)",
        [(r[0], r[1], r[2], r[3], None, r[4], "USD", r[5], 0) for r in rows])
    conn.commit()
