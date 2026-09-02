# PlutusTracker

A personal, local-only dashboard over your own bank and brokerage accounts.
It pulls Chase transactions and Wealthfront holdings through the
[Plaid API](https://plaid.com) into a SQLite file and serves four tabs on
`localhost`: net worth and asset allocation, income and expenses, investments,
and detected subscriptions.

Everything runs on your machine. Nothing is uploaded anywhere, and the app is
**read-only** — it uses no money-movement products, so it cannot transfer funds
or place trades even if asked to.

---

## Requirements

- Python 3.11 or newer
- A Plaid account (free — see below)
- US or Canadian bank / brokerage accounts

```bash
pip install -r requirements.txt
```

---

## Part 1 — Set up Plaid

Plaid is the service that actually talks to your bank. You need your own free
account; it takes a few minutes.

### 1. Create an account

Go to **[dashboard.plaid.com/signup](https://dashboard.plaid.com/signup)** and
verify your email.

### 2. Apply for the Trial plan

Use the button on the dashboard homepage, or go straight to
**[dashboard.plaid.com/trial-plan](https://dashboard.plaid.com/trial-plan)**.

The Trial plan is free and gives you **real production data** — actual balances
from your actual accounts. Most applications are approved automatically after
identity verification; if yours is flagged for manual review, Plaid emails you
within 2–3 business days.

It includes the products this app uses (Transactions, Investments, Balance,
and both Refresh products) and covers OAuth banks — Chase, Bank of America,
Wells Fargo, Capital One, Citi, US Bank, PNC, Amex — without the full
Production application.

> **Do not apply for full Production access.** A pending or approved Production
> application permanently disqualifies your team from the free Trial plan.
> Apply for Trial and stop there.

Eligibility: new Plaid teams in the US/Canada created on or after
15 April 2026.

### 3. Copy your keys

Dashboard → **Developers → Keys**. You need your `client_id` and at least one
secret. There is one secret per environment:

| Environment | What it is |
|---|---|
| **Sandbox** | Free and unlimited. Fake banks, fake data. Use it to try the app. |
| **Production** | Your real accounts. Limited to 10 Items on the Trial plan. |

---

## Part 2 — Set up the app

Everything happens through one entry point:

```bash
python app.py
```

```
====================================================
  PlutusTracker   [production]
  Linked: Chase, Wealthfront
====================================================

  [Enter]  Dashboard   (syncs on entry; Refresh button after)
  [l]      Link a new institution
  [c]      Configure Plaid credentials
  [q]      Quit
```

### 1. Enter your credentials — press `c`

Paste your `client_id`, then your secret. The secret is hidden as you type so
it never lands in your terminal history.

You don't need to say which environment the secret belongs to — the app finds
out by minting a throwaway link token against both Plaid hosts. That creates
no Item and costs nothing. Run `c` again later to add the other environment's
secret; both are kept, and the one you entered most recently becomes active.

Credentials are stored in `~/.plutus/credentials`, outside the repository.

### 2. Link your accounts — press `l`

This opens a small local server at **http://localhost:8000**. Click *Open Plaid
Link*, choose your institution, and log in.

Your bank credentials go to Plaid's widget — or, for OAuth banks like Chase,
to the bank's own website — and never touch this code.

**Link one product family at a time**, using the dropdown:

| Institution type | Choose | You get |
|---|---|---|
| Bank, credit card | **Bank — transactions** | transactions, balances |
| Brokerage, robo-advisor | **Brokerage — holdings** | positions, cost basis, buy/sell activity |

Chase and Wealthfront are two separate links, producing two separate Items.
They cannot be combined: a link token requesting both products hides every
institution that doesn't support both, which would remove Wealthfront from the
picker entirely.

When linking, **select every account you may ever want**. Changing the account
set later means a destructive relink (see below).

### 3. Open the dashboard — press Enter

The dashboard opens at **http://localhost:8001** immediately with your last
known values, while a sync runs in the background. A pulsing dot in the header
shows it working; the page refreshes itself when fresh data lands.

After that, the **Refresh** button pulls on demand. Its dropdown has an
auto-refresh toggle if you'd rather it update every 10 minutes on its own.

---

## Trying it without real accounts

Sandbox is free, unlimited, and completely separate from your real data — no
path connects them. Enter your **Sandbox** secret with `c`, then link any
institution using Plaid's test credentials:

- Username `user_good`
- Password `pass_good`
- MFA code `1234`

You can relink as many times as you like. Sandbox Items don't count against
your Trial allowance.

---

## Using it

**Overview** — net worth, cash/invested/debt, and your strategic asset
allocation. Set a target invested percentage and a *security floor* (cash you
always keep, held outside the allocation entirely), and the app tells you how
much to invest, or how much cash to rebuild, to reach your target:

```
invested share = invested / (net worth − security floor)
```

Defaults are 50% and $3,000. Below that, this month's cash flow and a donut of
where your expenses went.

**Income & Expenses** — cash flow charted biweekly, monthly, or yearly, with
income above the line, expenses below, and net overlaid. Expenses break down by
category for any month you pick; each category expands to its transactions,
sortable by date or amount. Income for the month is listed separately.

Money moving between your own accounts — card payments, transfers to
Wealthfront — is excluded from both sides, so paying your credit card doesn't
count as spending twice.

**Investments** — holdings with quantity, price, allocation share and
unrealized gain, plus every buy, sell, dividend and fee.

**Subscriptions** — recurring charges detected from your transactions, with an
estimated monthly and yearly cost. Anything that hasn't charged in more than
one billing period is marked *lapsed* and hidden by default, so the run-rate
reflects what you actually still pay for.

---

## Where your data lives

```
~/.plutus/
    credentials       Plaid client_id and secrets
    plaid_data.db     accounts, transactions, holdings, settings
    backups/          rotating copies taken before each sync
    plutus.log        application log (secrets redacted)
```

Nothing user-specific is stored in the checkout, so the repository stays
disposable. Set `PLUTUS_HOME` to relocate the directory.

Plaid **access tokens are encrypted at rest** in the database. The encryption
key is held in your operating system's credential store — Windows Credential
Manager, macOS Keychain, or Secret Service on Linux — rather than beside the
data, so a copied database is inert on another machine or account. Where no OS
keystore is available (a headless server, say), the app falls back to a key
file next to the database and reports that it has done so; that still protects
against casual disclosure through backups, but anyone who can read the
directory can read the key too.

The app also restricts file permissions to your user account where the OS
allows it. Still, treat `~/.plutus/` as sensitive: your full transaction
history lives there.

---

## Things that will bite you

**The Trial plan allows 10 Production Items, for the lifetime of the account.**
An Item is one connected institution. Removing an Item does *not* free a slot.
Chase plus Wealthfront uses two, leaving eight for mistakes — so if you want to
experiment, do it in Sandbox.

**Relinking Chase is destructive.** Creating a new Chase Item invalidates the
existing one whenever the account sets differ. Only relink when a sync reports
`REAUTH NEEDED`, which means the connection genuinely expired.

**Transaction history depth is fixed when you link.** The app requests Plaid's
maximum of 730 days, but banks vary in what they return, and the window cannot
be widened afterwards — extending it requires deleting the Item and relinking,
which costs another Item. Investment history has no such limit.

**Plaid serves cached data by default.** Each sync explicitly forces a refresh
before reading, which is why a sync takes about half a minute. Without it,
balances can lag reality by a day or more.

---

## Running pieces individually

`app.py` only orchestrates; each module runs on its own:

```bash
python -m source.sync           # headless pull, safe to re-run
python -m source.dashboard      # dashboard only, no sync
python -m source.link_server    # link flow only
```

Use the module form — `python source/sync.py` will not work, because the
package uses relative imports.

Inspect the database directly with:

```bash
sqlite3 ~/.plutus/plaid_data.db
```

---

## Troubleshooting

**"No Plaid credentials"** — press `c` and enter them. If you just switched
`client_id`, note that access tokens are bound to the `client_id` that created
them; a different one invalidates every stored token and requires relinking.

**A sync prints `REAUTH NEEDED`** — that connection expired. Press `l` and
relink that institution, selecting the same accounts.

**A sync prints `n/a`** — expected, not an error. Each Item supports one
product family, so the app tries all of them and reports the ones that don't
apply as unavailable.

**Numbers don't match your bank** — press Refresh and give it ~30 seconds. If
they still differ, the transactions may be pending; the amounts settle later
and the sync corrects them.
