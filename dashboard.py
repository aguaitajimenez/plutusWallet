"""Local money dashboard over plaid_data.db, in three tabs:

    Overview          - what you have right now, per institution
    Income & Expenses - monthly cash flow aggregated across Chase + Wealthfront
    Investments       - holdings, allocation, unrealized gains

    python dashboard.py   ->   http://localhost:8001

Reads only what sync.py last wrote. Nothing here calls Plaid except the
Refresh button, which runs the same sync in-process.
"""
import datetime
import math
import re
import statistics

from flask import Flask, redirect, render_template, request, url_for
from jinja2 import DictLoader

import plaid_api
import store
import sync

app = Flask(__name__)

DEBT_TYPES = {"credit", "loan"}
INVEST_TYPES = {"investment", "brokerage"}

# Categories excluded from income/expense math so moving your own money
# around (card payments, account transfers) doesn't count twice. Payroll and
# direct deposits also arrive under "Transfer" in Plaid's taxonomy, so those
# are explicitly kept as income.
TRANSFER_PREFIXES = ("Transfer", "Payment > Credit Card")
# Kept in the cash-flow math despite the Transfer prefix - these are money
# crossing the household boundary, not internal moves: payroll/deposits in,
# checks, Zelle/ACH debits, and cash withdrawals out. Own-institution moves
# (Wealthfront, card payments) are still caught by name and category above.
KEEP_AS_CASHFLOW = ("Transfer > Payroll", "Transfer > Deposit",
                    "Transfer > Withdrawal", "Transfer > Debit",
                    "Transfer > Credit")


def _cat_top(category):
    """Top-level display category, with readable buckets for the Transfer
    subtypes that count as spending - Plaid cannot see a check's payee, so
    'Transfer' would be misleading."""
    c = category or ""
    if c.startswith("Transfer > Withdrawal > Check"):
        return "Checks"
    if c.startswith("Transfer > Withdrawal"):
        return "Cash withdrawals"
    if c.startswith(("Transfer > Debit", "Transfer > Credit")):
        return "Zelle & bank payments"
    if c.startswith(("Shops > Supermarkets and Groceries", "Food and Drink > Groceries")):
        return "Groceries"
    return (c or "Uncategorized").split(" > ")[0]


def money(value):
    if value is None:
        return "--"
    return "{}${:,.2f}".format("-" if value < 0 else "", abs(value))


app.jinja_env.filters["money"] = money

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _k(value):
    """Short axis-tick money: $850, $12.7K."""
    if value >= 1000:
        return "${:.1f}K".format(value / 1000)
    return "${:,.0f}".format(value)


app.jinja_env.filters["kmoney"] = _k


def _flow_chart(periods):
    """Geometry for a diverging column chart: income up, expenses down.

    periods ascending: [{label, income, expenses, net}]. Coordinates are
    computed here so the template just emits SVG.
    """
    W, H = 720, 252
    # T and B leave room for the latest column's direct labels, which sit
    # outside the bars and must not collide with the tick row.
    L, R, T, B = 46, 10, 22, 38
    plot_w, plot_h = W - L - R, H - T - B
    max_i = max((p["income"] for p in periods), default=0.0)
    max_e = max((p["expenses"] for p in periods), default=0.0)
    ppd = plot_h / ((max_i + max_e) or 1.0)
    y0 = T + max_i * ppd
    n = max(len(periods), 1)
    sw = plot_w / n
    bw = max(6.0, min(26.0, sw * 0.55))

    def rounded(x, y_base, y_end, up):
        # Rounded at the data end only, square at the baseline.
        h = abs(y_base - y_end)
        if h < 0.5:
            return None
        r = min(4.0, h, bw / 2)
        if up:
            return ("M{:.1f},{:.1f} L{:.1f},{:.1f} Q{:.1f},{:.1f} {:.1f},{:.1f} "
                    "L{:.1f},{:.1f} Q{:.1f},{:.1f} {:.1f},{:.1f} L{:.1f},{:.1f} Z").format(
                x, y_base, x, y_end + r, x, y_end, x + r, y_end,
                x + bw - r, y_end, x + bw, y_end, x + bw, y_end + r, x + bw, y_base)
        return ("M{:.1f},{:.1f} L{:.1f},{:.1f} Q{:.1f},{:.1f} {:.1f},{:.1f} "
                "L{:.1f},{:.1f} Q{:.1f},{:.1f} {:.1f},{:.1f} L{:.1f},{:.1f} Z").format(
            x, y_base, x, y_end - r, x, y_end, x + r, y_end,
            x + bw - r, y_end, x + bw, y_end, x + bw, y_end - r, x + bw, y_base)

    bars, hits, xlabels, direct, net_pts = [], [], [], [], []
    for i, p in enumerate(periods):
        cx = L + sw * i + sw / 2
        x = cx - bw / 2
        top = y0 - 1 - p["income"] * ppd
        bot = y0 + 1 + p["expenses"] * ppd
        net_pts.append({"x": cx, "y": y0 - p["net"] * ppd})
        d = rounded(x, y0 - 1, top, up=True)
        if d:
            bars.append({"d": d, "cls": "in"})
        d = rounded(x, y0 + 1, bot, up=False)
        if d:
            bars.append({"d": d, "cls": "out"})
        hits.append({"x": L + sw * i, "w": sw, "label": p["label"],
                     "income": money(p["income"]), "expenses": money(p["expenses"]),
                     "net": money(p["net"])})
        xlabels.append({"x": cx, "text": p["label"]})
        if i == n - 1:  # direct-label only the latest period
            if p["income"]:
                direct.append({"x": cx, "y": top - 5, "text": _k(p["income"])})
            if p["expenses"]:
                direct.append({"x": cx, "y": bot + 12, "text": _k(p["expenses"])})

    grid = []
    if max_i:
        grid.append({"y": T, "text": _k(max_i)})
    if max_e:
        grid.append({"y": H - B, "text": _k(max_e)})
    net_path = ""
    if len(net_pts) >= 2:
        net_path = "M" + " L".join("{:.1f},{:.1f}".format(p["x"], p["y"]) for p in net_pts)
    return {"W": W, "H": H, "L": L, "R": R, "T": T, "plot_h": plot_h, "y0": y0,
            "bars": bars, "hits": hits, "xlabels": xlabels, "direct": direct,
            "grid": grid, "net_pts": net_pts, "net_path": net_path,
            "empty": not periods}


def _donut(slices, cx=90.0, cy=90.0, r_out=82.0, r_in=56.0, of="assets"):
    """Segment paths for a donut. slices: [{label, value, cls}], positive values."""
    total = sum(s["value"] for s in slices) or 1.0
    paths = []
    a0 = -math.pi / 2

    def pt(radius, angle):
        return cx + radius * math.cos(angle), cy + radius * math.sin(angle)

    for s in slices:
        frac = s["value"] / total
        sweep = frac * 2 * math.pi
        # A full-circle arc collapses in SVG; nudge it just under 360 degrees.
        sweep = min(sweep, 2 * math.pi - 0.001)
        a1 = a0 + sweep
        large = 1 if sweep > math.pi else 0
        x0o, y0o = pt(r_out, a0)
        x1o, y1o = pt(r_out, a1)
        x1i, y1i = pt(r_in, a1)
        x0i, y0i = pt(r_in, a0)
        d = ("M{:.2f},{:.2f} A{r},{r} 0 {} 1 {:.2f},{:.2f} "
             "L{:.2f},{:.2f} A{ri},{ri} 0 {} 0 {:.2f},{:.2f} Z").format(
            x0o, y0o, large, x1o, y1o, x1i, y1i, large, x0i, y0i,
            r=r_out, ri=r_in)
        paths.append({"d": d, "cls": s["cls"],
                      "tip": "<b>{}</b><br>{} &middot; {:.0f}% of {}".format(
                          s["label"], money(s["value"]), frac * 100, of)})
        a0 = a1
    return paths


def _conn_env():
    conn = store.connect()
    return conn, plaid_api.ENV


def _accounts(conn, env):
    items = store.items(conn, env)
    by_item = {i["item_id"]: i["institution_name"] for i in items}
    accounts = [
        dict(a) for a in conn.execute(
            "SELECT * FROM accounts WHERE item_id IN (SELECT item_id FROM items WHERE env = ?)",
            (env,),
        )
    ]
    for a in accounts:
        a["institution"] = by_item.get(a["item_id"], "unknown")
        a["is_debt"] = a["type"] in DEBT_TYPES
        a["signed"] = -(a["current"] or 0) if a["is_debt"] else (a["current"] or 0)
    return accounts, len(items)


def _is_transfer(category):
    c = category or ""
    if c.startswith(KEEP_AS_CASHFLOW):
        return False
    return c.startswith(TRANSFER_PREFIXES)


def common(conn, env, accounts, linked):
    freshest = max((a["updated_at"] for a in accounts), default=None)
    return {
        "env": env,
        "linked": linked,
        "updated": freshest[:16] if freshest else None,
        "empty": not accounts,
    }


def _flows(conn, env, accounts):
    """Transactions (institution + transfer flags), monthly aggregates, and the
    current month's summary and expense-by-category split. Shared by the
    Overview and Income & Expenses tabs."""
    inst = {a["account_id"]: a["institution"] for a in accounts}
    txns = [
        dict(t) for t in conn.execute(
            """SELECT t.* FROM transactions t
               JOIN accounts a ON a.account_id = t.account_id
               JOIN items i ON i.item_id = a.item_id
               WHERE i.env = ? ORDER BY t.date DESC""",
            (env,),
        )
    ]
    # A transaction naming one of the user's own institutions is money moving
    # between their own accounts, whatever Plaid categorized it as (Wealthfront
    # deposits arrive as "Service > Financial", not "Transfer").
    names = {(a["institution"] or "").lower() for a in accounts} - {"", "unknown"}
    inst_re = re.compile(r"\b(" + "|".join(sorted(re.escape(n) for n in names)) + r")\b") \
        if names else None
    for t in txns:
        t["institution"] = inst.get(t["account_id"], "unknown")
        label = "{} {}".format(t["merchant_name"] or "", t["name"] or "").lower()
        t["transfer"] = (_is_transfer(t["category"])
                         or bool(inst_re and inst_re.search(label)))

    # Plaid convention: positive amount = money out, negative = money in.
    months = {}
    for t in txns:
        if t["transfer"] or not t["date"] or t["amount"] is None:
            continue
        m = months.setdefault(t["date"][:7], {"income": 0.0, "expenses": 0.0})
        if t["amount"] < 0:
            m["income"] += -t["amount"]
        else:
            m["expenses"] += t["amount"]

    cur = max(months) if months else None
    this = {"income": 0.0, "expenses": 0.0, "net": 0.0, "month": cur}
    if cur:
        v = months[cur]
        this = {"income": v["income"], "expenses": v["expenses"],
                "net": v["income"] - v["expenses"], "month": cur}

    cats = {}
    for t in txns:
        if t["transfer"] or not t["amount"] or t["amount"] <= 0:
            continue
        if cur and (t["date"] or "").startswith(cur):
            top = _cat_top(t["category"])
            cats[top] = cats.get(top, 0.0) + t["amount"]
    return txns, months, this, cats


def _pdate(s):
    try:
        return datetime.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


# (base days, lo, hi, label, charges per month)
CADENCES = [(7, 5, 9, "weekly", 30 / 7), (14, 11, 17, "every 2 weeks", 30 / 14),
            (30, 26, 35, "monthly", 1.0), (91, 80, 100, "quarterly", 1 / 3),
            (365, 340, 390, "yearly", 1 / 12)]


def _detect_subscriptions(txns):
    """Heuristic recurring-charge detector.

    Groups outflows by normalized merchant, then flags groups whose charges
    land on a steady interval with a steady amount, plus anything Plaid
    already categorizes as a subscription.
    """
    groups = {}
    for t in txns:
        # < $2 filters out interest/rounding noise that recurs "on schedule".
        if t["transfer"] or not t["amount"] or t["amount"] < 2 or not _pdate(t["date"]):
            continue
        raw = (t["merchant_name"] or t["name"] or "").lower()
        key = re.sub(r"[\d#*]+", "", raw).strip()[:40]
        if key:
            groups.setdefault(key, []).append(t)

    subs = []
    for key, ts in groups.items():
        ts.sort(key=lambda t: t["date"])
        amounts = [t["amount"] for t in ts]
        typical = statistics.median(amounts)
        is_sub_cat = any("Subscription" in (t["category"] or "") for t in ts)

        # One interval proves nothing: two visits a month apart looks
        # "monthly". Interval-based detection needs at least three charges;
        # Plaid's own Subscription category vouches for newer ones below.
        cadence = factor = base = None
        if len(ts) >= 3:
            days = [(_pdate(ts[i + 1]["date"]) - _pdate(ts[i]["date"])).days
                    for i in range(len(ts) - 1)]
            med = statistics.median(days)
            for b, lo, hi, label, f in CADENCES:
                if lo <= med <= hi:
                    cadence, factor, base = label, f, b
                    break
            # A steady interval with a wobbly amount is groceries, not a
            # subscription - unless Plaid itself calls it one.
            steady = all(abs(a - typical) <= max(1.0, 0.15 * typical) for a in amounts)
            if cadence and not steady and not is_sub_cat:
                cadence = None
        if cadence is None and is_sub_cat:
            cadence, factor, base = "monthly (assumed)", 1.0, 30
        if cadence is None:
            continue

        # Lapsed when noticeably more than one billing period has passed
        # since the last charge (25% grace so a bill due today isn't flagged).
        days_since = (datetime.date.today() - _pdate(ts[-1]["date"])).days
        subs.append({"name": (ts[-1]["merchant_name"] or ts[-1]["name"] or key).strip(),
                     "cadence": cadence, "typical": typical,
                     "monthly": typical * factor, "count": len(ts),
                     "last": ts[-1]["date"], "days_since": days_since,
                     "active": days_since <= base * 1.25,
                     "txns": list(reversed(ts))})
    subs.sort(key=lambda s: -s["monthly"])
    return subs


@app.route("/subscriptions")
def subscriptions():
    conn, env = _conn_env()
    accounts, linked = _accounts(conn, env)
    txns, _, _, _ = _flows(conn, env, accounts)
    subs = _detect_subscriptions(txns)
    active = [s for s in subs if s["active"]]
    monthly_total = sum(s["monthly"] for s in active)
    return render_template(
        "subs.html",
        c=common(conn, env, accounts, linked),
        subs=subs,
        active_count=len(active),
        monthly_total=monthly_total,
        yearly_total=monthly_total * 12,
    )


@app.route("/")
def overview():
    conn, env = _conn_env()
    accounts, linked = _accounts(conn, env)
    groups = {}
    for a in sorted(accounts, key=lambda a: (a["type"] != "depository", a["name"] or "")):
        groups.setdefault(a["institution"], []).append(a)
    total = sum(a["signed"] for a in accounts)
    cash = sum(a["signed"] for a in accounts if a["type"] == "depository")
    invested = sum(a["signed"] for a in accounts if a["type"] in INVEST_TYPES)
    debt = sum(-a["signed"] for a in accounts if a["is_debt"])

    # Strategic asset allocation. The security floor is cash that must always
    # stay held; it and debt sit outside the SAA base entirely, so:
    #   invested share = invested / (invested + cash - floor - debt)
    # which, since net worth = cash + invested - debt, is simply:
    #   invested share = invested / (net worth - security floor)
    raw_t = store.get_setting(conn, "saa_target", None)
    target = float(raw_t) if raw_t not in (None, "") else 50.0
    raw_f = store.get_setting(conn, "cash_floor", None)
    floor = float(raw_f) if raw_f not in (None, "") else 3000.0
    investable = cash - floor
    base = invested + investable - debt
    pct_saa = (invested / base * 100) if base > 0 else None

    # Target values in dollars, and how far each side is from them. Cash is
    # shown net of the floor AND debt (debt is a claim against cash), so the
    # two rows sum to the base and the gaps are equal and opposite.
    saa_tgt = None
    if base > 0:
        tgt_inv = target / 100 * base
        cash_now = investable - debt
        saa_tgt = {"inv_now": invested, "inv_tgt": tgt_inv,
                   "inv_gap": tgt_inv - invested,
                   "cash_now": cash_now, "cash_tgt": base - tgt_inv,
                   "cash_gap": (base - tgt_inv) - cash_now}

    saa_msgs = []
    if investable < 0:
        saa_msgs.append(("warn", "Cash is {} below your security floor - hold off "
                                 "on investing until it is rebuilt.".format(money(-investable))))
    if base > 0 and investable >= 0:
        move = target / 100 * base - invested
        if move >= 1:
            if move > investable:
                saa_msgs.append(("", "Reaching {:.0f}% needs {}, more than your "
                                     "investable cash. Investing all {} would get "
                                     "you to {:.1f}%.".format(
                                         target, money(move), money(investable),
                                         (invested + investable) / base * 100)))
            else:
                saa_msgs.append(("pos", "Invest {} to reach {:.0f}% invested.".format(
                    money(move), target)))
        elif move <= -1:
            need_cash = (invested / (target / 100) - base) if target > 0 else 0
            saa_msgs.append(("", "You are {} above target. Wait: letting cash rebuild "
                                 "by {} with no new investing brings you back to "
                                 "{:.0f}%.".format(money(-move), money(need_cash), target)))
        else:
            saa_msgs.append(("pos", "On target."))

    slices = []
    if invested > 0:
        slices.append({"label": "Invested", "value": invested, "cls": "sl-inv"})
    if investable > 0:
        slices.append({"label": "Cash", "value": investable, "cls": "sl-cash"})
    if floor > 0 and cash > 0:
        slices.append({"label": "Security floor", "value": min(floor, cash), "cls": "sl-floor"})
    donut_total = sum(s["value"] for s in slices)
    legend = [{"label": s["label"], "value": s["value"], "cls": s["cls"],
               "pct": s["value"] / donut_total * 100 if donut_total else 0}
              for s in slices]

    # This month's cash flow + expenses-by-category donut.
    _, _, this, cats = _flows(conn, env, accounts)
    ranked = sorted(cats.items(), key=lambda kv: -kv[1])
    cat_slices = [{"label": k, "value": v, "cls": "cat-{}".format(i + 1)}
                  for i, (k, v) in enumerate(ranked[:5])]
    other = sum(v for _, v in ranked[5:])
    if other > 0:
        cat_slices.append({"label": "Other", "value": other, "cls": "cat-other"})
    spend_total = sum(s["value"] for s in cat_slices)
    cat_legend = [dict(s, pct=s["value"] / spend_total * 100) for s in cat_slices]

    return render_template(
        "overview.html",
        c=common(conn, env, accounts, linked),
        groups=groups, total=total, cash=cash, invested=invested, debt=debt,
        donut=_donut(slices) if slices else [],
        legend=legend, pct_saa=pct_saa, target=target, floor=floor,
        investable=investable, saa_msgs=saa_msgs, saa_tgt=saa_tgt,
        this=this, spend_total=spend_total,
        exp_donut=_donut(cat_slices, of="spending") if cat_slices else [],
        cat_legend=cat_legend,
    )


@app.route("/cashflow")
def cashflow():
    conn, env = _conn_env()
    accounts, linked = _accounts(conn, env)
    txns, months, this, cats = _flows(conn, env, accounts)

    # One dataset per selectable time axis; all render the same chart form.
    monthly_asc = []
    for i, (key, v) in enumerate(sorted(months.items())[-12:]):
        year, mm = key.split("-")
        label = MONTHS[int(mm) - 1]
        if i == 0 or mm == "01":
            label = "{} {}".format(label, year[2:])
        monthly_asc.append({"label": label, "tlabel": key, "income": v["income"],
                            "expenses": v["expenses"],
                            "net": v["income"] - v["expenses"]})

    yearly = {}
    for key, v in months.items():
        y = yearly.setdefault(key[:4], {"income": 0.0, "expenses": 0.0})
        y["income"] += v["income"]
        y["expenses"] += v["expenses"]
    yearly_asc = [{"label": k, "tlabel": k, "income": v["income"],
                   "expenses": v["expenses"], "net": v["income"] - v["expenses"]}
                  for k, v in sorted(yearly.items())[-8:]]

    # Fixed 14-day buckets, last 12 (~6 months).
    buckets = {}
    for t in txns:
        if t["transfer"] or not t["date"] or t["amount"] is None:
            continue
        try:
            day = datetime.date.fromisoformat(t["date"][:10])
        except ValueError:
            continue
        b = buckets.setdefault(day.toordinal() // 14, {"income": 0.0, "expenses": 0.0})
        if t["amount"] < 0:
            b["income"] += -t["amount"]
        else:
            b["expenses"] += t["amount"]
    biweekly_asc = []
    for idx in sorted(buckets)[-12:]:
        start = datetime.date.fromordinal(idx * 14)
        v = buckets[idx]
        label = "{} {}".format(MONTHS[start.month - 1], start.day)
        biweekly_asc.append({"label": label, "tlabel": label,
                             "income": v["income"], "expenses": v["expenses"],
                             "net": v["income"] - v["expenses"]})

    periods = {
        "biweekly": {"name": "Biweekly", "chart": _flow_chart(biweekly_asc),
                     "rows": list(reversed(biweekly_asc))},
        "monthly": {"name": "Monthly", "chart": _flow_chart(monthly_asc),
                    "rows": list(reversed(monthly_asc))},
        "yearly": {"name": "Yearly", "chart": _flow_chart(yearly_asc),
                   "rows": list(reversed(yearly_asc))},
    }

    # Expense breakdown for a selectable month (?y=YYYY&m=MM). Falls back to
    # the latest month of the chosen year, then the latest month overall.
    years = sorted({k[:4] for k in months})
    y = request.args.get("y", "")
    if y not in years:
        y = (this["month"] or "")[:4]
        if y not in years and years:
            y = years[-1]
    ymonths = [k for k in sorted(months) if k.startswith(y)]
    sel = "{}-{}".format(y, request.args.get("m", ""))
    if sel not in months:
        sel = ymonths[-1] if ymonths else None

    cat_rows, cat_donut, spend_total, month_opts = [], [], 0.0, []
    if sel:
        agg = {}
        for t in txns:
            if t["transfer"] or not t["amount"] or t["amount"] <= 0:
                continue
            if (t["date"] or "").startswith(sel):
                top = _cat_top(t["category"])
                e = agg.setdefault(top, {"total": 0.0, "txns": []})
                e["total"] += t["amount"]
                e["txns"].append(t)
        ranked = sorted(agg.items(), key=lambda kv: -kv[1]["total"])
        spend_total = sum(e["total"] for _, e in ranked)
        slices = [{"label": k, "value": e["total"], "cls": "cat-{}".format(i + 1)}
                  for i, (k, e) in enumerate(ranked[:5])]
        other = sum(e["total"] for _, e in ranked[5:])
        if other > 0:
            slices.append({"label": "Other", "value": other, "cls": "cat-other"})
        cat_donut = _donut(slices, of="spending") if slices else []
        for i, (k, e) in enumerate(ranked):
            cat_rows.append({"label": k, "value": e["total"],
                             "cls": "cat-{}".format(i + 1) if i < 5 else "cat-other",
                             "pct": e["total"] / spend_total * 100 if spend_total else 0,
                             "txns": e["txns"]})
        month_opts = [{"value": k[5:], "label": MONTHS[int(k[5:]) - 1], "sel": k == sel}
                      for k in ymonths]

    # Income received in the selected month (inflows, transfers excluded).
    sel_income = [t for t in txns
                  if not t["transfer"] and t["amount"] is not None and t["amount"] < 0
                  and sel and (t["date"] or "").startswith(sel)]
    income_total = sum(-t["amount"] for t in sel_income)

    return render_template(
        "cashflow.html",
        c=common(conn, env, accounts, linked),
        this=this, periods=periods,
        years=years, y=y, sel=sel, month_opts=month_opts,
        cat_rows=cat_rows, cat_donut=cat_donut, spend_total=spend_total,
        sel_income=sel_income, income_total=income_total,
        recent=[t for t in txns if not t["transfer"]][:1000],
        transfers_hidden=sum(1 for t in txns if t["transfer"]),
    )


@app.route("/investments")
def investments():
    conn, env = _conn_env()
    accounts, linked = _accounts(conn, env)
    inst = {a["account_id"]: a["institution"] for a in accounts}
    ids = set(inst)

    holdings = [
        dict(h) for h in conn.execute("SELECT * FROM holdings ORDER BY value DESC")
        if h["account_id"] in ids
    ]
    total = sum(h["value"] or 0 for h in holdings)
    basis_total = 0.0
    for h in holdings:
        h["institution"] = inst.get(h["account_id"], "unknown")
        h["pct"] = (h["value"] / total * 100) if (total and h["value"]) else None
        basis = h["cost_basis"]
        h["gain"] = (h["value"] - basis) if (h["value"] is not None and basis) else None
        h["gain_pct"] = (h["gain"] / basis * 100) if (h["gain"] is not None and basis) else None
        basis_total += basis or 0

    gain_total = (total - basis_total) if basis_total else None
    return render_template(
        "invest.html",
        c=common(conn, env, accounts, linked),
        holdings=holdings, total=total, basis_total=basis_total,
        gain_total=gain_total,
        gain_total_pct=(gain_total / basis_total * 100) if gain_total is not None and basis_total else None,
        invest_accounts=[a for a in accounts if a["type"] in INVEST_TYPES],
    )


@app.route("/settings", methods=["POST"])
def save_settings():
    conn = store.connect()
    # Blank fields fall back to the defaults (50% / $3,000) rather than zero.
    for field, key, cap in (("target", "saa_target", 100.0), ("floor", "cash_floor", None)):
        raw = request.form.get(field, "").strip()
        if raw == "":
            store.set_setting(conn, key, "")
            continue
        try:
            val = max(0.0, float(raw))
            store.set_setting(conn, key, min(cap, val) if cap else val)
        except ValueError:
            pass
    return redirect(url_for("overview"))


@app.route("/refresh", methods=["POST"])
def refresh():
    try:
        sync.main()
    except Exception as exc:  # a dead connection shouldn't take the page down
        print("refresh failed: {}".format(exc))
    return redirect(url_for("overview"))


LAYOUT = """
<!doctype html><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>PlutusTracker</title>
<style>
 :root{--bg:#fbfbfa;--fg:#16150f;--dim:#6b6a63;--line:#e2e0d8;--card:#fff;
       --pos:#1e6b34;--neg:#a1201a;--accent:#16150f;
       --flow-in:#2a78d6;--flow-out:#e34948;
       --don-inv:#2a78d6;--don-cash:#eb6834;
       --c1:#2a78d6;--c2:#eb6834;--c3:#1baf7a;--c4:#eda100;--c5:#e87ba4;--cother:#898781}
 @media (prefers-color-scheme:dark){
   :root{--bg:#16150f;--fg:#f3f1e9;--dim:#96948b;--line:#2e2c25;--card:#1e1d16;
         --pos:#6fcf8f;--neg:#f08a80;--accent:#f3f1e9;
         --flow-in:#3987e5;--flow-out:#e66767;
         --don-inv:#3987e5;--don-cash:#d95926;
         --c1:#3987e5;--c2:#d95926;--c3:#199e70;--c4:#c98500;--c5:#d55181;--cother:#898781}}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);
      font:15px/1.55 ui-sans-serif,system-ui,sans-serif}
 main{max-width:56rem;margin:0 auto;padding:1.5rem 1.25rem 4rem}
 nav{display:flex;gap:.25rem;align-items:center;border-bottom:1px solid var(--line);
     margin-bottom:2rem;flex-wrap:wrap}
 nav a{padding:.7rem .9rem;color:var(--dim);text-decoration:none;font-weight:500;
       border-bottom:2px solid transparent;margin-bottom:-1px}
 nav a.on{color:var(--fg);border-bottom-color:var(--accent)}
 nav .right{margin-left:auto;display:flex;gap:.75rem;align-items:center}
 .tag{font-size:.68rem;letter-spacing:.08em;text-transform:uppercase;padding:.2rem .5rem;
      border-radius:20px;border:1px solid var(--line);color:var(--dim)}
 form{margin:0}
 button{font:inherit;font-size:.85rem;padding:.4rem .8rem;border-radius:7px;
        border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer}
 button:hover{border-color:var(--dim)}
 h1{font-size:2.6rem;font-weight:600;letter-spacing:-.02em;margin:.1rem 0 0;
    font-variant-numeric:tabular-nums}
 .sub{color:var(--dim);font-size:.85rem;margin:.3rem 0 0}
 .tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(9.5rem,1fr));
        gap:.75rem;margin:1.6rem 0 2.2rem}
 .tile{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.9rem 1rem}
 .tile b{display:block;font-size:1.25rem;font-weight:600;font-variant-numeric:tabular-nums}
 .tile span{color:var(--dim);font-size:.72rem;letter-spacing:.06em;text-transform:uppercase}
 h2{font-size:.95rem;margin:2rem 0 .6rem;font-weight:600}
 .wrap{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
 table{border-collapse:collapse;width:100%;font-size:.9rem}
 th{text-align:left;font-weight:500;color:var(--dim);font-size:.72rem;
    letter-spacing:.07em;text-transform:uppercase;padding:.7rem .9rem;white-space:nowrap}
 td{padding:.6rem .9rem;border-top:1px solid var(--line);font-variant-numeric:tabular-nums}
 td.n,th.n{text-align:right} td.nw{white-space:nowrap}
 .pos{color:var(--pos)} .neg{color:var(--neg)} .muted{color:var(--dim);font-size:.85em}
 .empty{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:2.5rem;text-align:center;color:var(--dim)}
 code{background:var(--bg);border:1px solid var(--line);padding:.15rem .4rem;
      border-radius:4px;font-size:.85em}
 .note{color:var(--dim);font-size:.78rem;margin:.5rem 0 0}
 .chartcard{background:var(--card);border:1px solid var(--line);border-radius:10px;
            padding:1rem 1rem .6rem;margin-bottom:.75rem}
 .legend{display:flex;gap:1.1rem;font-size:.78rem;color:var(--dim);margin-bottom:.5rem}
 .sw{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:.4rem;
     vertical-align:-1px}
 .sw.in{background:var(--flow-in)} .sw.out{background:var(--flow-out)}
 .sw.sl-inv{background:var(--don-inv)} .sw.sl-cash{background:var(--don-cash)}
 .sw.sl-floor{background:var(--cother)}
 .donut .sl-floor{fill:var(--cother)}
 .saaform{display:flex;flex-direction:column;gap:.7rem;min-width:13rem;
          font-size:.85rem;color:var(--dim)}
 .saaform input{font:inherit;padding:.45rem .6rem;border-radius:7px;
                border:1px solid var(--line);background:var(--bg);color:var(--fg);
                width:100%;margin-top:.25rem;box-sizing:border-box}
 .saaform button{align-self:flex-start}
 .saatable{width:100%;font-size:.85rem;margin:.7rem 0 0;border-collapse:collapse}
 .saatable th{font-size:.68rem;padding:.3rem .4rem}
 .saatable td{padding:.35rem .4rem;border-top:1px solid var(--line)}
 .reco{margin:.6rem 0 0;font-size:.92rem}
 .reco.warn{color:var(--neg)} .reco.pos{color:var(--pos)} .reco.muted{color:var(--dim)}
 .chartwrap{position:relative}
 svg.flow{width:100%;height:auto;display:block}
 .flow .grid{stroke:var(--line);stroke-width:1}
 .flow .baseline{stroke:var(--dim);stroke-width:1}
 .flow .tick{fill:var(--dim);font-size:10px}
 .flow .direct{fill:var(--fg);font-size:10px;font-weight:600}
 .flow path.in{fill:var(--flow-in)} .flow path.out{fill:var(--flow-out)}
 .flow .hit{fill:transparent} .flow .hit:hover{fill:var(--fg);fill-opacity:.05}
 .tip{position:absolute;pointer-events:none;background:var(--card);
      border:1px solid var(--line);border-radius:8px;padding:.45rem .65rem;
      font-size:.78rem;box-shadow:0 4px 14px rgba(0,0,0,.18);white-space:nowrap;z-index:2}
 .alloc{display:flex;gap:2rem;align-items:center;flex-wrap:wrap;
        background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:1.25rem;margin-bottom:.75rem}
 .alloc .chartwrap{flex:0 0 180px}
 svg.donut{width:180px;height:180px;display:block}
 .donut .hit{stroke:var(--card);stroke-width:2}
 .donut .hit:hover{opacity:.85}
 .donut .sl-inv{fill:var(--don-inv)} .donut .sl-cash{fill:var(--don-cash)}
 .don-big{fill:var(--fg);font-size:30px;font-weight:600}
 .don-small{fill:var(--dim);font-size:11px}
 .alloclegend{flex:1;min-width:14rem}
 .alloclegend .row{display:flex;align-items:center;gap:.6rem;padding:.35rem 0;
                   font-size:.9rem;font-variant-numeric:tabular-nums}
 .alloclegend .row b{margin-left:auto}
 .alloclegend .pct{color:var(--dim);min-width:3.2rem;text-align:right}
 .refwrap{position:relative;display:flex}
 .refwrap form button{border-radius:7px 0 0 7px}
 .caret{border-radius:0 7px 7px 0;border-left:0;padding:.4rem .5rem}
 .menu{position:absolute;right:0;top:calc(100% + 6px);background:var(--card);
       border:1px solid var(--line);border-radius:8px;padding:.6rem .8rem;
       font-size:.85rem;white-space:nowrap;z-index:3;box-shadow:0 4px 14px rgba(0,0,0,.18)}
 .menu label{display:flex;gap:.5rem;align-items:center;cursor:pointer}
 .seg{display:inline-flex;border:1px solid var(--line);border-radius:8px;
      overflow:hidden;margin:0 0 .9rem}
 .seg button{border:0;border-radius:0;background:transparent;padding:.45rem 1rem;
             color:var(--dim)}
 .seg button.on{background:var(--fg);color:var(--bg)}
 .seg button+button{border-left:1px solid var(--line)}
 .flow .netline{stroke:var(--fg);stroke-width:2;fill:none;opacity:.85}
 .flow .netdot{fill:var(--fg);stroke:var(--card);stroke-width:2}
 .sw.netsw{background:var(--fg);border-radius:50%}
 .donut .cat-1{fill:var(--c1)} .donut .cat-2{fill:var(--c2)} .donut .cat-3{fill:var(--c3)}
 .donut .cat-4{fill:var(--c4)} .donut .cat-5{fill:var(--c5)} .donut .cat-other{fill:var(--cother)}
 .sw.cat-1{background:var(--c1)} .sw.cat-2{background:var(--c2)} .sw.cat-3{background:var(--c3)}
 .sw.cat-4{background:var(--c4)} .sw.cat-5{background:var(--c5)} .sw.cat-other{background:var(--cother)}
 .selrow{display:flex;gap:.5rem;margin:0 0 .9rem;align-items:center;flex-wrap:wrap}
 .selrow #sortseg{margin-left:auto}
 .catcard{background:var(--card);border:1px solid var(--line);border-radius:10px;
          padding:.25rem 1rem;margin-bottom:.5rem}
 .persuffix{font-size:1rem;font-weight:500;color:var(--dim);margin-left:.3rem}
 .subrow.lapsed summary{opacity:.65}
 .lapsetag{font-size:.66rem;letter-spacing:.07em;text-transform:uppercase;
           color:var(--neg);border:1px solid var(--neg);border-radius:20px;
           padding:.1rem .45rem;opacity:.9}
 select{font:inherit;font-size:.85rem;padding:.4rem .6rem;border-radius:7px;
        border:1px solid var(--line);background:var(--card);color:var(--fg)}
 .catlist{flex:1;min-width:16rem}
 .catlist details{border-bottom:1px solid var(--line)}
 .catlist details:last-child{border-bottom:0}
 .catlist summary{display:flex;align-items:center;gap:.6rem;padding:.5rem .2rem;
                  cursor:pointer;font-size:.9rem;font-variant-numeric:tabular-nums}
 .catlist summary::before{content:'\\25B8';color:var(--dim);font-size:.7rem;
                          transition:transform .15s}
 .catlist details[open] summary::before{transform:rotate(90deg)}
 .catlist summary b{margin-left:auto}
 .catlist table{width:100%;margin:.1rem 0 .7rem;font-size:.82rem}
 .catlist td{padding:.35rem .5rem}
</style>
<main>
<nav>
  <a href="{{ url_for('overview') }}"    class="{{ 'on' if tab == 'overview' }}">Overview</a>
  <a href="{{ url_for('cashflow') }}"    class="{{ 'on' if tab == 'cashflow' }}">Income &amp; Expenses</a>
  <a href="{{ url_for('investments') }}" class="{{ 'on' if tab == 'invest' }}">Investments</a>
  <a href="{{ url_for('subscriptions') }}" class="{{ 'on' if tab == 'subs' }}">Subscriptions</a>
  <div class=right>
    <span class=tag>{{ c.env }}</span>
    <div class=refwrap>
      <form id=refreshform method=post action="{{ url_for('refresh') }}"><button>Refresh</button></form>
      <button type=button class=caret id=caret aria-label="Refresh options">&#9662;</button>
      <div class=menu id=refmenu hidden>
        <label><input type=checkbox id=autoref> Auto-refresh every 10 min</label>
      </div>
    </div>
  </div>
</nav>
{% if c.empty %}
  <div class=empty>
    <p>No accounts linked yet.</p>
    <p>Run <code>python link_server.py</code> to connect Chase and Wealthfront,
       then <code>python sync.py</code>.</p>
  </div>
{% else %}
  {% block body %}{% endblock %}
  <p class=note>Last synced {{ c.updated or 'never' }} &middot;
     {{ c.linked }} institution{{ '' if c.linked == 1 else 's' }} linked</p>
{% endif %}
</main>
<script>
// Refresh options: caret opens a menu with the auto-refresh toggle.
// When on, the page re-syncs and reloads itself every 10 minutes by
// submitting the same form the Refresh button uses.
(function () {
  var caret = document.getElementById('caret'), menu = document.getElementById('refmenu'),
      box = document.getElementById('autoref'), form = document.getElementById('refreshform'),
      timer = null;
  caret.addEventListener('click', function (e) {
    menu.hidden = !menu.hidden; e.stopPropagation();
  });
  document.addEventListener('click', function (e) {
    if (!menu.hidden && !menu.contains(e.target) && e.target !== caret) menu.hidden = true;
  });
  function arm() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { form.submit(); }, 600000);
  }
  var on = false;
  try { on = localStorage.getItem('autorefresh') === '1'; } catch (err) {}
  box.checked = on;
  if (on) arm();
  box.addEventListener('change', function () {
    try { localStorage.setItem('autorefresh', box.checked ? '1' : '0'); } catch (err) {}
    if (box.checked) arm(); else if (timer) clearTimeout(timer);
  });
})();

// Sort the per-category transaction tables by date or amount. Lives here at
// the page bottom so it binds after the tables exist in the DOM.
document.querySelectorAll('#sortseg button').forEach(function (b) {
  b.addEventListener('click', function () {
    document.querySelectorAll('#sortseg button').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
    document.querySelectorAll('.catlist table').forEach(function (tb) {
      var body = tb.tBodies[0] || tb;
      var rows = [].slice.call(body.querySelectorAll('tr'));
      rows.sort(b.dataset.s === 'amount'
        ? function (a, c) { return parseFloat(c.dataset.amount) - parseFloat(a.dataset.amount); }
        : function (a, c) { return c.dataset.date < a.dataset.date ? -1 : 1; });
      rows.forEach(function (r) { body.appendChild(r); });
    });
  });
});

// Reveal more of the recent-transactions table, 25 rows at a time.
var morebtn = document.getElementById('morebtn');
if (morebtn) morebtn.addEventListener('click', function () {
  var hidden = document.querySelectorAll('#recenttable tr[hidden]');
  for (var i = 0; i < 25 && i < hidden.length; i++) hidden[i].hidden = false;
  var left = hidden.length - Math.min(25, hidden.length);
  if (left <= 0) morebtn.hidden = true;
  else morebtn.textContent = 'Show more (' + left + ' remaining)';
});

// Shared hover tooltip for charts.
document.addEventListener('mousemove', function (e) {
  var t = e.target, wrap = t.closest ? t.closest('.chartwrap') : null;
  document.querySelectorAll('.tip').forEach(function (el) {
    if (!wrap || el.parentElement !== wrap) el.hidden = true;
  });
  if (!wrap || !t.classList.contains('hit')) return;
  var tip = wrap.querySelector('.tip');
  tip.innerHTML = t.dataset.tip ||
                  '<b>' + t.dataset.label + '</b><br>Income ' + t.dataset.income +
                  '<br>Expenses ' + t.dataset.expenses + '<br>Net ' + t.dataset.net;
  tip.hidden = false;
  var r = wrap.getBoundingClientRect();
  var x = e.clientX - r.left + 14, y = e.clientY - r.top - 10;
  if (x + tip.offsetWidth > r.width) x -= tip.offsetWidth + 28;
  tip.style.left = x + 'px'; tip.style.top = y + 'px';
});
</script>
"""

OVERVIEW = """
{% extends 'layout.html' %}{% set tab = 'overview' %}
{% block body %}
<p class=sub>Net worth</p>
<h1 class="{{ 'neg' if total < 0 }}">{{ total|money }}</h1>
<div class=tiles>
  <div class=tile><span>Cash</span><b>{{ cash|money }}</b></div>
  <div class=tile><span>Invested</span><b>{{ invested|money }}</b></div>
  {% if debt %}<div class=tile><span>Debt</span><b class=neg>{{ (-debt)|money }}</b></div>{% endif %}
</div>

<h2>Allocation &middot; SAA</h2>
<div class=alloc>
  {% if donut %}
  <div class=chartwrap>
    <svg viewBox="0 0 180 180" class=donut role=img>
      {% for p in donut %}<path d="{{ p.d }}" class="hit {{ p.cls }}" data-tip="{{ p.tip }}" />{% endfor %}
      <text x=90 y=88 text-anchor=middle class=don-big>
        {{- '{:.0f}%'.format(pct_saa) if pct_saa is not none else '--' -}}
      </text>
      <text x=90 y=108 text-anchor=middle class=don-small>invested</text>
    </svg>
    <div class=tip hidden></div>
  </div>
  {% endif %}
  <div class=alloclegend>
    {% for l in legend %}
    <div class=row><i class="sw {{ l.cls }}"></i>
      {{ l.label }} <span class=pct>{{ '%.0f%%'|format(l.pct) }}</span>
      <b>{{ l.value|money }}</b></div>
    {% endfor %}
    {% if debt %}
    <div class=row><i class=sw style="background:transparent"></i><span class=muted>Debt
      (subtracted from the base)</span><b class=neg>{{ (-debt)|money }}</b></div>
    {% endif %}
    {% if target is not none and pct_saa is not none %}
    <div class=row><i class=sw style="background:transparent"></i>
      <span class=muted>Target</span>
      <b>{{ '%.0f%%'|format(target) }} <span class=muted>vs {{ '%.1f%%'|format(pct_saa) }} now</span></b></div>
    {% endif %}
    {% if saa_tgt %}
    <table class=saatable>
      <tr><th></th><th class=n>Now</th><th class=n>Target</th><th class=n>Gap</th></tr>
      <tr>
        <td><i class="sw sl-inv"></i> Invested</td>
        <td class=n>{{ saa_tgt.inv_now|money }}</td>
        <td class=n>{{ saa_tgt.inv_tgt|money }}</td>
        <td class="n {{ 'pos' if saa_tgt.inv_gap > 0.5 else 'neg' if saa_tgt.inv_gap < -0.5 }}">
          {{- '+' if saa_tgt.inv_gap > 0.5 }}{{ saa_tgt.inv_gap|money -}}
        </td>
      </tr>
      <tr>
        <td><i class="sw sl-cash"></i> Cash <span class=muted>(after floor &amp; debt)</span></td>
        <td class="n {{ 'neg' if saa_tgt.cash_now < 0 }}">{{ saa_tgt.cash_now|money }}</td>
        <td class=n>{{ saa_tgt.cash_tgt|money }}</td>
        <td class="n {{ 'pos' if saa_tgt.cash_gap > 0.5 else 'neg' if saa_tgt.cash_gap < -0.5 }}">
          {{- '+' if saa_tgt.cash_gap > 0.5 }}{{ saa_tgt.cash_gap|money -}}
        </td>
      </tr>
    </table>
    {% endif %}
    {% for cls, msg in saa_msgs %}<p class="reco {{ cls }}">{{ msg }}</p>{% endfor %}
    <p class=note>Invested share = invested / (net worth &minus; security floor).
       The floor is cash you always hold; it sits outside the allocation, and
       debt is already netted inside net worth.</p>
  </div>
  <form method=post action="{{ url_for('save_settings') }}" class=saaform>
    <label>Target invested %
      <input type=number name=target min=0 max=100 step=1
             value="{{ '%.0f'|format(target) if target is not none else '' }}" placeholder="e.g. 60">
    </label>
    <label>Security floor (cash always held)
      <input type=number name=floor min=0 step=100 value="{{ '%.0f'|format(floor) }}">
    </label>
    <button>Save</button>
  </form>
</div>

<h2>This month &middot; cash flow</h2>
<div class=tiles>
  <div class=tile><span>Income</span><b class=pos>{{ this.income|money }}</b></div>
  <div class=tile><span>Expenses</span><b class=neg>{{ this.expenses|money }}</b></div>
  <div class=tile><span>Net</span>
    <b class="{{ 'neg' if this.net < 0 else 'pos' }}">{{ this.net|money }}</b></div>
</div>

{% if exp_donut %}
<h2>Where expenses went &middot; {{ this.month }}</h2>
<div class=alloc>
  <div class=chartwrap>
    <svg viewBox="0 0 180 180" class=donut role=img>
      {% for p in exp_donut %}<path d="{{ p.d }}" class="hit {{ p.cls }}" data-tip="{{ p.tip }}" />{% endfor %}
      <text x=90 y=88 text-anchor=middle class=don-big>{{ spend_total|kmoney }}</text>
      <text x=90 y=108 text-anchor=middle class=don-small>spent</text>
    </svg>
    <div class=tip hidden></div>
  </div>
  <div class=alloclegend>
    {% for l in cat_legend %}
    <div class=row><i class="sw {{ l.cls }}"></i>
      {{ l.label }} <span class=pct>{{ '%.0f%%'|format(l.pct) }}</span>
      <b>{{ l.value|money }}</b></div>
    {% endfor %}
  </div>
</div>
{% endif %}

{% for institution, accts in groups.items() %}
  <h2>{{ institution }}</h2>
  <div class=wrap><table>
    <tr><th>Account</th><th>Type</th><th class=n>Available</th><th class=n>Balance</th></tr>
    {% for a in accts %}
    <tr>
      <td>{{ a.name }}{% if a.mask %} <span class=muted>&middot;{{ a.mask }}</span>{% endif %}</td>
      <td class=muted>{{ a.subtype or a.type }}</td>
      <td class="n muted">{{ a.available|money }}</td>
      <td class="n {{ 'neg' if a.is_debt }}">{{ a.signed|money }}</td>
    </tr>
    {% endfor %}
  </table></div>
{% endfor %}
{% endblock %}
"""

CASHFLOW = """
{% extends 'layout.html' %}{% set tab = 'cashflow' %}
{% macro flow(ch) %}
<div class=chartcard>
  <div class=legend>
    <span><i class="sw in"></i>Income</span>
    <span><i class="sw out"></i>Expenses</span>
    <span><i class="sw netsw"></i>Net</span>
  </div>
  <div class=chartwrap>
    <svg viewBox="0 0 {{ ch.W }} {{ ch.H }}" class=flow role=img>
      {% for g in ch.grid %}
        <line x1="{{ ch.L }}" x2="{{ ch.W - ch.R }}" y1="{{ '%.1f'|format(g.y) }}"
              y2="{{ '%.1f'|format(g.y) }}" class=grid />
        <text x="{{ ch.L - 6 }}" y="{{ '%.1f'|format(g.y + 3) }}" class=tick
              text-anchor=end>{{ g.text }}</text>
      {% endfor %}
      <line x1="{{ ch.L }}" x2="{{ ch.W - ch.R }}" y1="{{ '%.1f'|format(ch.y0) }}"
            y2="{{ '%.1f'|format(ch.y0) }}" class=baseline />
      {% for b in ch.bars %}<path d="{{ b.d }}" class="{{ b.cls }}" />{% endfor %}
      {% if ch.net_path %}<path d="{{ ch.net_path }}" class=netline />{% endif %}
      {% for p in ch.net_pts %}
        <circle cx="{{ '%.1f'|format(p.x) }}" cy="{{ '%.1f'|format(p.y) }}" r=4 class=netdot />
      {% endfor %}
      {% for l in ch.xlabels %}
        <text x="{{ '%.1f'|format(l.x) }}" y="{{ ch.H - 6 }}" class=tick
              text-anchor=middle>{{ l.text }}</text>
      {% endfor %}
      {% for l in ch.direct %}
        <text x="{{ '%.1f'|format(l.x) }}" y="{{ '%.1f'|format(l.y) }}" class=direct
              text-anchor=middle>{{ l.text }}</text>
      {% endfor %}
      {% for h in ch.hits %}
        <rect x="{{ '%.1f'|format(h.x) }}" y="{{ ch.T }}" width="{{ '%.1f'|format(h.w) }}"
              height="{{ ch.plot_h }}" class=hit data-label="{{ h.label }}"
              data-income="{{ h.income }}" data-expenses="{{ h.expenses }}"
              data-net="{{ h.net }}" />
      {% endfor %}
    </svg>
    <div class=tip hidden></div>
  </div>
</div>
{% endmacro %}
{% block body %}
<p class=sub>This month</p>
<h1 class="{{ 'neg' if this.net < 0 else 'pos' }}">{{ this.net|money }}</h1>
<div class=tiles>
  <div class=tile><span>Income</span><b class=pos>{{ this.income|money }}</b></div>
  <div class=tile><span>Expenses</span><b class=neg>{{ this.expenses|money }}</b></div>
</div>

<h2>Cash flow</h2>
<div class=seg>
  {% for key, p in periods.items() %}
  <button type=button data-p="{{ key }}" class="{{ 'on' if key == 'monthly' }}">{{ p.name }}</button>
  {% endfor %}
</div>
{% for key, p in periods.items() %}
<section class=period id="p-{{ key }}" {{ 'hidden' if key != 'monthly' }}>
  {{ flow(p.chart) }}
  <div class=wrap><table>
    <tr><th>Period</th><th class=n>Income</th><th class=n>Expenses</th><th class=n>Net</th></tr>
    {% for r in p.rows %}
    <tr>
      <td class=nw>{{ r.tlabel }}</td>
      <td class="n pos">{{ r.income|money }}</td>
      <td class="n neg">{{ r.expenses|money }}</td>
      <td class="n {{ 'neg' if r.net < 0 else 'pos' }}">{{ r.net|money }}</td>
    </tr>
    {% endfor %}
  </table></div>
</section>
{% endfor %}
<script>
// Time-axis toggle for the cash-flow chart.
document.querySelectorAll('.seg button[data-p]').forEach(function (b) {
  b.addEventListener('click', function () {
    document.querySelectorAll('.seg button[data-p]').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
    document.querySelectorAll('.period').forEach(function (s) {
      s.hidden = (s.id !== 'p-' + b.dataset.p);
    });
  });
});
</script>

{% if sel %}
<h2>Where it went &middot; {{ sel }}</h2>
<div class=selrow>
  <form method=get class=selrow style="margin:0">
    <select name=y onchange="this.form.submit()">
      {% for yy in years %}<option value="{{ yy }}" {{ 'selected' if yy == y }}>{{ yy }}</option>{% endfor %}
    </select>
    <select name=m onchange="this.form.submit()">
      {% for o in month_opts %}<option value="{{ o.value }}" {{ 'selected' if o.sel }}>{{ o.label }}</option>{% endfor %}
    </select>
  </form>
  <div class=seg id=sortseg style="margin:0">
    <button type=button data-s=date class=on>Date</button>
    <button type=button data-s=amount>Amount</button>
  </div>
</div>
<div class=alloc>
  <div class=chartwrap>
    <svg viewBox="0 0 180 180" class=donut role=img>
      {% for p in cat_donut %}<path d="{{ p.d }}" class="hit {{ p.cls }}" data-tip="{{ p.tip }}" />{% endfor %}
      <text x=90 y=88 text-anchor=middle class=don-big>{{ spend_total|kmoney }}</text>
      <text x=90 y=108 text-anchor=middle class=don-small>spent</text>
    </svg>
    <div class=tip hidden></div>
  </div>
  <div class=catlist>
    {% for r in cat_rows %}
    <details>
      <summary><i class="sw {{ r.cls }}"></i>{{ r.label }}
        <span class=pct>{{ '%.0f%%'|format(r.pct) }}</span><b>{{ r.value|money }}</b></summary>
      <table>
        {% for t in r.txns %}
        <tr data-date="{{ t.date }}" data-amount="{{ t.amount }}">
          <td class=nw>{{ t.date }}</td>
          <td>{{ t.merchant_name or t.name }}{% if t.pending %} <span class=muted>pending</span>{% endif %}</td>
          <td class=muted>{{ t.institution }}</td>
          <td class=n>{{ t.amount|money }}</td>
        </tr>
        {% endfor %}
      </table>
    </details>
    {% endfor %}
  </div>
</div>

<h2>Income &middot; {{ sel }}</h2>
{% if sel_income %}
<div class=wrap><table>
  <tr><th>Date</th><th>Description</th><th>Source</th><th class=n>Amount</th></tr>
  {% for t in sel_income %}
  <tr>
    <td class=nw>{{ t.date }}</td>
    <td>{{ t.merchant_name or t.name }}{% if t.pending %} <span class=muted>pending</span>{% endif %}</td>
    <td class=muted>{{ t.institution }}</td>
    <td class="n pos">{{ (-t.amount)|money }}</td>
  </tr>
  {% endfor %}
  <tr>
    <td colspan=3><b>Total</b></td>
    <td class="n pos"><b>{{ income_total|money }}</b></td>
  </tr>
</table></div>
{% else %}
<div class=empty><p>No income recorded in {{ sel }}.</p></div>
{% endif %}
{% endif %}

<h2>Recent transactions</h2>
<div class=wrap><table id=recenttable>
  <tr><th>Date</th><th>Description</th><th>Category</th><th>Source</th><th class=n>Amount</th></tr>
  {% for t in recent %}
  <tr {{ 'hidden' if loop.index0 >= 25 }}>
    <td class=nw>{{ t.date }}</td>
    <td>{{ t.merchant_name or t.name }}{% if t.pending %} <span class=muted>pending</span>{% endif %}</td>
    <td class=muted>{{ (t.category or '').split(' > ')[-1] }}</td>
    <td class=muted>{{ t.institution }}</td>
    <td class="n {{ 'pos' if t.amount is not none and t.amount < 0 }}">
      {{ (-t.amount)|money if t.amount is not none else '--' }}</td>
  </tr>
  {% endfor %}
</table></div>
{% if recent|length > 25 %}
<p><button type=button id=morebtn>Show more ({{ recent|length - 25 }} remaining)</button></p>
{% endif %}
{% if transfers_hidden %}
<p class=note>{{ transfers_hidden }} internal transfer{{ '' if transfers_hidden == 1 else 's' }}
   (card payments, account moves) excluded from income and expenses.</p>
{% endif %}
{% endblock %}
"""

INVEST = """
{% extends 'layout.html' %}{% set tab = 'invest' %}
{% block body %}
<p class=sub>Portfolio value</p>
<h1>{{ total|money }}</h1>
<div class=tiles>
  <div class=tile><span>Cost basis</span><b>{{ basis_total|money if basis_total else '--' }}</b></div>
  <div class=tile><span>Unrealized gain</span>
    <b class="{{ 'pos' if gain_total and gain_total > 0 else 'neg' if gain_total and gain_total < 0 }}">
      {{ gain_total|money if gain_total is not none else '--' }}
      {% if gain_total_pct is not none %}<span class=muted>{{ '%+.1f%%'|format(gain_total_pct) }}</span>{% endif %}
    </b></div>
</div>

{% if invest_accounts %}
<h2>Accounts</h2>
<div class=wrap><table>
  <tr><th>Account</th><th>Institution</th><th class=n>Balance</th></tr>
  {% for a in invest_accounts %}
  <tr><td>{{ a.name }}</td><td class=muted>{{ a.institution }}</td>
      <td class=n>{{ a.signed|money }}</td></tr>
  {% endfor %}
</table></div>
{% endif %}

<h2>Holdings</h2>
{% if holdings %}
<div class=wrap><table>
  <tr><th>Security</th><th>Source</th><th class=n>Qty</th><th class=n>Price</th>
      <th class=n>Value</th><th class=n>% of total</th><th class=n>Gain</th></tr>
  {% for h in holdings %}
  <tr>
    <td>{% if h.ticker %}<b>{{ h.ticker }}</b> {% endif %}
        <span class=muted>{{ h.security_name or '' }}</span></td>
    <td class=muted>{{ h.institution }}</td>
    <td class="n muted">{{ '%.4f'|format(h.quantity) if h.quantity is not none else '--' }}</td>
    <td class="n muted">{{ h.price|money }}</td>
    <td class=n>{{ h.value|money }}</td>
    <td class="n muted">{{ '%.1f%%'|format(h.pct) if h.pct is not none else '--' }}</td>
    <td class="n {{ 'pos' if h.gain and h.gain > 0 else 'neg' if h.gain and h.gain < 0 }}">
      {% if h.gain is not none %}{{ h.gain|money }}
        <span class=muted>{{ '%+.1f%%'|format(h.gain_pct) if h.gain_pct is not none }}</span>
      {% else %}--{% endif %}</td>
  </tr>
  {% endfor %}
</table></div>
{% else %}
<div class=empty><p>No holdings synced yet. Link Wealthfront as
  <b>Brokerage</b> in the link server, then refresh.</p></div>
{% endif %}
{% endblock %}
"""

SUBS = """
{% extends 'layout.html' %}{% set tab = 'subs' %}
{% block body %}
<p class=sub>Active subscriptions</p>
<h1>{{ monthly_total|money }}<span class=persuffix>/month</span></h1>
<div class=tiles>
  <div class=tile><span>Per month</span><b>{{ monthly_total|money }}</b></div>
  <div class=tile><span>Per year</span><b>{{ yearly_total|money }}</b></div>
  <div class=tile><span>Active / detected</span><b>{{ active_count }} / {{ subs|length }}</b></div>
</div>

<h2>Subscriptions</h2>
<div class=seg id=subseg>
  <button type=button data-f=active class=on>Active</button>
  <button type=button data-f=all>All detected</button>
</div>
{% if subs %}
<div class="catcard catlist">
  {% for s in subs %}
  <details class="subrow{{ ' lapsed' if not s.active }}" {{ 'hidden' if not s.active }}>
    <summary>{{ s.name }}
      {% if not s.active %}<span class=lapsetag>lapsed</span>{% endif %}
      <span class=muted>{{ s.cadence }} &middot; {{ s.count }} charge{{ '' if s.count == 1 else 's' }}
        &middot; last {{ s.last }}{% if not s.active %} ({{ s.days_since }} days ago){% endif %}</span>
      <b>{{ s.typical|money }}<span class=muted>/charge</span></b></summary>
    <table>
      {% for t in s.txns %}
      <tr>
        <td class=nw>{{ t.date }}</td>
        <td>{{ t.merchant_name or t.name }}</td>
        <td class=muted>{{ t.institution }}</td>
        <td class=n>{{ t.amount|money }}</td>
      </tr>
      {% endfor %}
    </table>
  </details>
  {% endfor %}
</div>
<p class=note>Detected heuristically: merchants charging a steady amount on a regular
   interval, plus anything Plaid categorizes as a subscription. A subscription counts
   as <b>lapsed</b> once more than ~1&frac14; billing periods pass without a charge;
   the run-rate tiles above count active ones only.</p>
{% else %}
<div class=empty><p>No recurring charges detected yet &mdash; detection needs at
   least two charges from the same merchant.</p></div>
{% endif %}
<script>
document.querySelectorAll('#subseg button').forEach(function (b) {
  b.addEventListener('click', function () {
    document.querySelectorAll('#subseg button').forEach(function (x) {
      x.classList.toggle('on', x === b);
    });
    document.querySelectorAll('.subrow.lapsed').forEach(function (d) {
      d.hidden = (b.dataset.f === 'active');
    });
  });
});
</script>
{% endblock %}
"""

app.jinja_loader = DictLoader({
    "layout.html": LAYOUT,
    "overview.html": OVERVIEW,
    "cashflow.html": CASHFLOW,
    "invest.html": INVEST,
    "subs.html": SUBS,
})


if __name__ == "__main__":
    print("\n  {}  ->  http://localhost:8001\n".format(plaid_api.ENV.upper()))
    app.run(port=8001, debug=False)
