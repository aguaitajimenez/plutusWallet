"""The money math: what counts as spending, and what the SAA card recommends.

These are the calculations a user would act on, so they are the ones worth
pinning down. Most of the bugs this suite guards against were real: brokerage
deposits counted as expenses, rent cheques dropped entirely, and an allocation
gap that disagreed with itself.
"""
from source import dashboard, store

from conftest import add_txns


# --- what is spending and what is just moving your own money ----------------

def test_internal_transfers_are_not_spending():
    assert dashboard._is_transfer("Transfer > Internal Account Transfer")
    assert dashboard._is_transfer("Payment > Credit Card")


def test_payroll_is_income_despite_the_transfer_prefix():
    # Plaid files direct deposits under Transfer; excluding them would erase income.
    assert not dashboard._is_transfer("Transfer > Payroll")
    assert not dashboard._is_transfer("Transfer > Deposit")


def test_cheques_and_cash_leave_the_household_so_they_count():
    assert not dashboard._is_transfer("Transfer > Withdrawal > Check")
    assert not dashboard._is_transfer("Transfer > Withdrawal > ATM")
    assert not dashboard._is_transfer("Transfer > Debit")


def test_categories_are_split_into_readable_buckets():
    assert dashboard._cat_top("Transfer > Withdrawal > Check") == "Checks"
    assert dashboard._cat_top("Transfer > Withdrawal > ATM") == "Cash withdrawals"
    assert dashboard._cat_top("Transfer > Debit") == "Zelle & bank payments"
    assert dashboard._cat_top("Shops > Supermarkets and Groceries") == "Groceries"
    assert dashboard._cat_top("Shops > Department Stores") == "Shops"
    assert dashboard._cat_top(None) == "Uncategorized"


def test_money_moved_to_a_linked_institution_is_never_an_expense(linked):
    add_txns(linked, [
        ("t1", "acc_check", "2026-08-01", "PAYROLL", -4000.0, "Transfer > Payroll"),
        ("t2", "acc_check", "2026-08-05", "Example Brokerage", 2000.0,
         "Service > Financial > Financial Planning and Investments"),
        ("t3", "acc_check", "2026-08-06", "WHOLE FOODS", 120.0,
         "Shops > Supermarkets and Groceries"),
    ])
    accounts, _ = dashboard._accounts(linked, "sandbox")
    _, _, this, cats = dashboard._flows(linked, "sandbox", accounts)

    assert this["income"] == 4000.0
    assert this["expenses"] == 120.0      # the 2000 to the brokerage is not spending
    assert cats == {"Groceries": 120.0}


def test_every_transaction_is_either_counted_or_explicitly_internal(linked):
    add_txns(linked, [
        ("t1", "acc_check", "2026-08-01", "PAYROLL", -4000.0, "Transfer > Payroll"),
        ("t2", "acc_check", "2026-08-02", "Example Brokerage", 2000.0, "Service > Financial"),
        ("t3", "acc_check", "2026-08-03", "RENT", 2395.0, "Transfer > Withdrawal > Check"),
        ("t4", "acc_card", "2026-08-04", "PAYMENT TO CREDIT CARD", 500.0,
         "Payment > Credit Card"),
    ])
    accounts, _ = dashboard._accounts(linked, "sandbox")
    txns, _, _, _ = dashboard._flows(linked, "sandbox", accounts)

    counted = [t for t in txns if not t["transfer"]]
    internal = [t for t in txns if t["transfer"]]
    assert len(counted) + len(internal) == len(txns) == 4
    assert {t["transaction_id"] for t in counted} == {"t1", "t3"}


# --- strategic asset allocation ---------------------------------------------

def _saa(client):
    """Scrape the Now/Target/Gap figures the Overview renders."""
    import re
    body = client.get("/").get_data(as_text=True)
    section = body[body.index("<table class=saatable"):]
    section = section[:section.index("</table>")]
    nums = re.findall(r"[+-]?\$[\d,]+\.\d\d", re.sub(r"<[^>]+>", " ", section))
    return [float(n.replace("$", "").replace(",", "").replace("+", "")) for n in nums]


def test_allocation_gaps_are_equal_and_opposite(linked):
    """Rebalancing is zero-sum: whatever investing needs, cash gives up."""
    store.set_setting(linked, "saa_target", "70")
    store.set_setting(linked, "cash_floor", "1000")
    inv_now, inv_tgt, inv_gap, cash_now, cash_tgt, cash_gap = _saa(
        dashboard.app.test_client())

    assert round(inv_gap + cash_gap, 2) == 0
    assert round(inv_now + cash_now, 2) == round(inv_tgt + cash_tgt, 2)


def test_security_floor_and_debt_leave_the_base(linked):
    """base = invested + cash - floor - debt; here 10000 + 5000 - 1000 - 250."""
    store.set_setting(linked, "saa_target", "50")
    store.set_setting(linked, "cash_floor", "1000")
    inv_now, inv_tgt, _, cash_now, _, _ = _saa(dashboard.app.test_client())

    assert inv_now == 10000.0
    assert cash_now == 3750.0           # 5000 cash - 1000 floor - 250 card debt
    assert inv_tgt == 6875.0            # half of a 13750 base


def test_defaults_apply_when_nothing_is_configured(linked):
    body = dashboard.app.test_client().get("/").get_data(as_text=True)
    assert 'name=target min=0 max=100 step=1\n             value="50"' in body \
        or 'value="50"' in body
    assert 'value="3000"' in body


# --- subscription detection --------------------------------------------------

def _sub_txns(name, dates, amount, category="Service > Subscription"):
    return [{"transaction_id": "{}{}".format(name, i), "account_id": "acc_check",
             "date": d, "name": name, "merchant_name": name, "amount": amount,
             "category": category, "transfer": False, "institution": "Example Bank"}
            for i, d in enumerate(dates)]


def test_steady_monthly_charge_is_detected():
    subs = dashboard._detect_subscriptions(
        _sub_txns("Netflix", ["2026-06-10", "2026-07-10", "2026-08-10"], 15.49))
    assert [s["name"] for s in subs] == ["Netflix"]
    assert subs[0]["cadence"] == "monthly"
    assert subs[0]["monthly"] == 15.49


def test_two_visits_a_month_apart_are_not_a_subscription():
    """The bug this guards: any shop visited twice looked 'monthly'."""
    subs = dashboard._detect_subscriptions(
        _sub_txns("Marshalls", ["2026-06-05", "2026-07-09"], 72.72,
                  "Shops > Department Stores"))
    assert subs == []


def test_wobbly_amounts_are_groceries_not_subscriptions():
    txns = _sub_txns("Corner Shop", ["2026-06-01", "2026-07-01", "2026-08-01"], 20.0,
                     "Food and Drink > Restaurants")
    txns[1]["amount"] = 55.0
    txns[2]["amount"] = 9.0
    assert dashboard._detect_subscriptions(txns) == []


def test_tiny_recurring_amounts_are_ignored():
    subs = dashboard._detect_subscriptions(
        _sub_txns("INTEREST", ["2026-06-13", "2026-07-13", "2026-08-13"], 0.01,
                  "Interest > Interest Earned"))
    assert subs == []


def test_lapsed_subscriptions_are_flagged_but_kept():
    import datetime
    today = datetime.date.today()
    old = [(today - datetime.timedelta(days=d)).isoformat() for d in (120, 90, 60)]
    recent = [(today - datetime.timedelta(days=d)).isoformat() for d in (62, 31, 2)]
    subs = {s["name"]: s for s in dashboard._detect_subscriptions(
        _sub_txns("Gone", old, 9.99) + _sub_txns("Live", recent, 12.0))}

    assert subs["Gone"]["active"] is False
    assert subs["Live"]["active"] is True
