"""Pull everything from every linked Item into plaid_data.db.

    python sync.py

Safe to run repeatedly: transactions use a stored cursor, so each run fetches
only what changed. Holdings are a full snapshot and get overwritten.
"""
import datetime
import logging
import time

from . import config, plaid_api, store

LOOKBACK_DAYS = 730   # Plaid's maximum
PAGE = 500            # max page size for /investments/transactions/get
REFRESH_WAIT_S = 8    # refresh is asynchronous; give Plaid time to re-pull

log = logging.getLogger("plutus")

# Errors that just mean "this Item is not that kind of account" - not failures.
SKIPPABLE = {
    "PRODUCTS_NOT_SUPPORTED",
    "PRODUCT_NOT_READY",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_ACCOUNTS",
    # An Item may genuinely lack consent for a product family. That is only
    # benign when the Item holds no account the product would have covered -
    # see cash_accounts_without_transactions.
    "ADDITIONAL_CONSENT_REQUIRED",
}

# Account types whose activity arrives through the transactions product.
CASH_TYPES = ("depository", "credit")


def cash_accounts_without_transactions(conn, item):
    """Cash accounts under an Item that cannot sync for want of the product.

    An Item's products are fixed when it is created, so a brokerage linked for
    `investments` alone fetches nothing for its cash account. Plaid answers
    that pull with a consent error, and reporting it as "n/a" makes it look
    identical to an Item that simply has no cash accounts - which is how a
    salary paid into a brokerage's cash account stayed missing from cash flow
    without anything ever looking wrong.
    """
    qs = ",".join("?" * len(CASH_TYPES))
    return [r["name"] or r["account_id"] for r in conn.execute(
        "SELECT account_id, name FROM accounts WHERE item_id = ? "
        "AND type IN ({})".format(qs), (item["item_id"],) + CASH_TYPES)]


def refresh_all(items):
    """Ask Plaid to re-pull from every institution, then wait once.

    Without this, Plaid serves a cached snapshot that can lag real balances by
    a day or more - holdings especially. Refreshes are fired for every Item
    before the single wait, so the wait overlaps rather than stacking per Item.
    Each endpoint applies only to the matching product, so the other one
    erroring is expected, not a failure.
    """
    asked = []
    for item in items:
        for path, label in (("/investments/refresh", "holdings"),
                            ("/transactions/refresh", "transactions")):
            try:
                plaid_api.call(path, access_token=item["access_token"])
                asked.append("{} {}".format(item["institution_name"], label))
            except plaid_api.PlaidError:
                continue  # product not on this Item
    if asked:
        time.sleep(REFRESH_WAIT_S)
    return asked


def sync_accounts(conn, item):
    res = plaid_api.call("/accounts/get", access_token=item["access_token"])
    for acct in res["accounts"]:
        bal = acct.get("balances", {})
        conn.execute(
            """INSERT INTO accounts (account_id, item_id, name, official_name, type,
                                     subtype, mask, current, available, currency, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(account_id) DO UPDATE SET
                 current=excluded.current, available=excluded.available,
                 updated_at=CURRENT_TIMESTAMP""",
            (acct["account_id"], item["item_id"], acct.get("name"),
             acct.get("official_name"), acct.get("type"), acct.get("subtype"),
             acct.get("mask"), bal.get("current"), bal.get("available"),
             bal.get("iso_currency_code")),
        )
    conn.commit()
    return len(res["accounts"])


def sync_transactions(conn, item):
    cursor, added, removed = item["cursor"], 0, 0
    while True:
        body = {"access_token": item["access_token"]}
        if cursor:
            body["cursor"] = cursor
        res = plaid_api.call("/transactions/sync", **body)

        for txn in res["added"] + res["modified"]:
            cats = txn.get("category") or []
            conn.execute(
                """INSERT INTO transactions (transaction_id, account_id, date, name,
                       merchant_name, amount, currency, category, pending)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(transaction_id) DO UPDATE SET
                     amount=excluded.amount, pending=excluded.pending,
                     name=excluded.name""",
                (txn["transaction_id"], txn["account_id"], txn["date"], txn.get("name"),
                 txn.get("merchant_name"), txn.get("amount"),
                 txn.get("iso_currency_code"), " > ".join(cats),
                 int(bool(txn.get("pending")))),
            )
        added += len(res["added"])

        for gone in res["removed"]:
            conn.execute("DELETE FROM transactions WHERE transaction_id = ?",
                         (gone["transaction_id"],))
        removed += len(res["removed"])

        cursor = res["next_cursor"]
        conn.commit()
        if not res["has_more"]:
            break

    store.set_cursor(conn, item["item_id"], cursor)
    return "{} new, {} removed".format(added, removed)


def sync_investment_transactions(conn, item):
    """Buys, sells, dividends and fees. Paginated: an automated investing
    service generates hundreds of small transactions."""
    end = datetime.date.today()
    start = end - datetime.timedelta(days=LOOKBACK_DAYS)
    offset = seen = 0
    while True:
        res = plaid_api.call(
            "/investments/transactions/get",
            access_token=item["access_token"],
            start_date=start.isoformat(), end_date=end.isoformat(),
            options={"count": PAGE, "offset": offset},
        )
        securities = {s["security_id"]: s for s in res.get("securities", [])}
        batch = res.get("investment_transactions", [])
        for t in batch:
            sec = securities.get(t.get("security_id")) or {}
            conn.execute(
                """INSERT INTO investment_transactions (investment_transaction_id,
                       account_id, security_id, ticker, name, type, subtype, date,
                       quantity, price, amount, fees, currency)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(investment_transaction_id) DO UPDATE SET
                     quantity=excluded.quantity, price=excluded.price,
                     amount=excluded.amount, fees=excluded.fees""",
                (t["investment_transaction_id"], t["account_id"], t.get("security_id"),
                 sec.get("ticker_symbol"), t.get("name"), t.get("type"),
                 t.get("subtype"), t.get("date"), t.get("quantity"), t.get("price"),
                 t.get("amount"), t.get("fees"), t.get("iso_currency_code")),
            )
        seen += len(batch)
        offset += len(batch)
        conn.commit()
        if not batch or seen >= res.get("total_investment_transactions", seen):
            break
    return "{} activity rows".format(seen)


def sync_holdings(conn, item):
    res = plaid_api.call("/investments/holdings/get", access_token=item["access_token"])
    securities = {s["security_id"]: s for s in res["securities"]}
    live = set()
    for h in res["holdings"]:
        live.add((h["account_id"], h["security_id"]))
        sec = securities.get(h["security_id"], {})
        conn.execute(
            """INSERT INTO holdings (account_id, security_id, ticker, security_name, type,
                   quantity, price, value, cost_basis, currency, as_of)
               VALUES (?,?,?,?,?,?,?,?,?,?,CURRENT_TIMESTAMP)
               ON CONFLICT(account_id, security_id) DO UPDATE SET
                 quantity=excluded.quantity, price=excluded.price,
                 value=excluded.value, cost_basis=excluded.cost_basis,
                 as_of=CURRENT_TIMESTAMP""",
            (h["account_id"], h["security_id"], sec.get("ticker_symbol"),
             sec.get("name"), sec.get("type"), h.get("quantity"),
             h.get("institution_price"), h.get("institution_value"),
             h.get("cost_basis"), h.get("iso_currency_code")),
        )

    # Drop positions Plaid no longer reports for these accounts - a sold
    # holding must not linger at its last known value.
    dropped = 0
    acct_ids = [a["account_id"] for a in res.get("accounts", [])]
    if acct_ids:
        qs = ",".join("?" * len(acct_ids))
        stale = [(r["account_id"], r["security_id"]) for r in conn.execute(
            "SELECT account_id, security_id FROM holdings "
            "WHERE account_id IN ({})".format(qs), acct_ids)
            if (r["account_id"], r["security_id"]) not in live]
        for key in stale:
            conn.execute(
                "DELETE FROM holdings WHERE account_id = ? AND security_id = ?", key)
        dropped = len(stale)

    conn.commit()
    return "{} holdings{}".format(
        len(res["holdings"]), ", {} closed".format(dropped) if dropped else "")


def main():
    """Pull every linked Item. Returns the institutions needing re-auth."""
    config.setup_logging()
    conn = store.connect()
    linked = store.items(conn, plaid_api.ENV)
    if not linked:
        log.info("No linked accounts in %s. Use [l] Link in app.py.", plaid_api.ENV)
        return []

    config.backup_db()
    asked = refresh_all(linked)
    log.info("refreshed: %s", ", ".join(asked) or "nothing")

    needs_reauth, uncovered = [], []
    for item in linked:
        log.info("\n%s", item["institution_name"])
        for label, fn in (("accounts", sync_accounts),
                          ("transactions", sync_transactions),
                          ("holdings", sync_holdings),
                          ("investing", sync_investment_transactions)):
            try:
                log.info("  %-13s %s", label, fn(conn, item))
            except plaid_api.PlaidError as exc:
                if exc.code in SKIPPABLE:
                    blind = (cash_accounts_without_transactions(conn, item)
                             if label == "transactions" else [])
                    if blind:
                        log.warning(
                            "  %-13s NOT SYNCED - this Item has no transactions "
                            "product, so nothing from %s is pulled",
                            label, ", ".join(blind))
                        if item["institution_name"] not in uncovered:
                            uncovered.append(item["institution_name"])
                    else:
                        log.info("  %-13s n/a", label)
                elif exc.code == "ITEM_LOGIN_REQUIRED":
                    log.warning("  %-13s REAUTH NEEDED - relink this institution", label)
                    if item["institution_name"] not in needs_reauth:
                        needs_reauth.append(item["institution_name"])
                else:
                    log.error("  %-13s %s", label, exc)
            except Exception as exc:  # one bad Item must not abort the rest
                log.exception("  %-13s unexpected failure: %s", label, exc)

    if uncovered:
        log.warning(
            "%s: cash accounts are not being synced, so money paid into "
            "them is missing from cash flow. Fix without spending a Plaid "
            "Item: [l] Link -> Add missing products.", ", ".join(uncovered))

    log.info("\nSync complete.")
    return needs_reauth


if __name__ == "__main__":
    main()
