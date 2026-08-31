"""Pull everything from every linked Item into plaid_data.db.

    python sync.py

Safe to run repeatedly: transactions use a stored cursor, so each run fetches
only what changed. Holdings are a full snapshot and get overwritten.
"""
import plaid_api
import store

# Errors that just mean "this Item is not that kind of account" - not failures.
SKIPPABLE = {
    "PRODUCTS_NOT_SUPPORTED",
    "PRODUCT_NOT_READY",
    "NO_INVESTMENT_ACCOUNTS",
    "NO_ACCOUNTS",
    # Each Item is linked for one product family only, so the other pulls
    # are expected to lack consent.
    "ADDITIONAL_CONSENT_REQUIRED",
}


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


def sync_holdings(conn, item):
    res = plaid_api.call("/investments/holdings/get", access_token=item["access_token"])
    securities = {s["security_id"]: s for s in res["securities"]}
    for h in res["holdings"]:
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
    conn.commit()
    return "{} holdings".format(len(res["holdings"]))


def main():
    conn = store.connect()
    linked = store.items(conn, plaid_api.ENV)
    if not linked:
        print("No linked accounts in {}. Run: python link_server.py".format(plaid_api.ENV))
        return

    for item in linked:
        print("\n{}".format(item["institution_name"]))
        for label, fn in (("accounts", sync_accounts),
                          ("transactions", sync_transactions),
                          ("holdings", sync_holdings)):
            try:
                print("  {:<13} {}".format(label, fn(conn, item)))
            except plaid_api.PlaidError as exc:
                if exc.code in SKIPPABLE:
                    print("  {:<13} n/a".format(label))
                elif exc.code == "ITEM_LOGIN_REQUIRED":
                    print("  {:<13} REAUTH NEEDED - relink this institution".format(label))
                else:
                    print("  {:<13} {}".format(label, exc))

    print("\nWrote plaid_data.db")


if __name__ == "__main__":
    main()
