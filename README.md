# PlutusTracker

A local personal-finance dashboard. It pulls your own bank and brokerage data
through the [Plaid API](https://plaid.com) into a SQLite database on your
machine and serves a dashboard over it at `http://localhost:8001`.

Read-only by construction: no money-movement product is enabled, so nothing
here can place a trade or move a dollar.

## What it shows

| Tab | Contents |
|---|---|
| **Overview** | Net worth, allocation donut, strategic asset allocation with a target and a cash floor, this month's cash flow, expenses by category |
| **Income & Expenses** | Biweekly / monthly / yearly cash-flow charts with a net overlay, expandable per-category transaction tables, income table |
| **Investments** | Holdings with cost basis and unrealised gain, plus buys, sells, dividends and fees |
| **Subscriptions** | Recurring charges detected from your transaction history, with lapsed ones filtered out and a monthly and yearly run rate |

## Install

Requires Python 3.9 or newer.

```bash
git clone https://github.com/aguaitajimenez/plutusWallet plutustracker && cd plutustracker && pip install -r requirements.txt
```

Then start it:

```bash
python app.py
```

## First run

PlutusTracker ships with no credentials of its own: you point it at a Plaid
account you control. Everything below is free.

1. **Sign up** at [dashboard.plaid.com/signup](https://dashboard.plaid.com/signup).
   When asked *How will you use Plaid?*, choose **Personal use**.

   Avoid the alternatives. *Business* opens a company application, and *App
   user* is for people connecting an account to somebody else's Plaid-powered
   app — here you are building the app, not using one.

2. **Create your login** and verify your e-mail address. When asked for a use
   case, choose **Personal finance**.

3. **Request the Trial plan.** Signing up leaves you on the Dashboard
   overview; request it from there, or go straight to
   [dashboard.plaid.com/trial-plan](https://dashboard.plaid.com/trial-plan).
   Most applications are approved automatically. The plan costs nothing,
   returns real production data, and covers up to 10 linked institutions.

   > **Never apply for full Production access.** The Trial plan is offered
   > only to accounts that have never applied for, or held, any form of
   > Production access, and there is no self-service way back once you have.

4. **Wait until the plan is granted**, then copy your `client_id` and secret
   from **Developers → Keys**. Collecting them earlier leaves you on Sandbox:
   the app infers the environment from the secret itself, not from anything
   you tell it.

5. **Configure the app.** Run `python app.py`, choose **[c] Configure**, and
   paste both values. The secret stays hidden as you type.

6. **Link your institutions.** Choose **[l] Link**. Pick **Bank** for chequing,
   savings and cards; pick **Brokerage** for investment accounts.

7. **Open the dashboard** with **[Enter]**.

### Practise first

Sandbox is free and unlimited, and uses fake data — no real account is ever
touched. Log in with `user_good` / `pass_good`, MFA code `1234`. Production
Items are capped at 10 **for the lifetime of the account**, and removing one
does not free the slot, so rehearse in Sandbox before linking anything real.

## Where your data lives

Everything is in `~/.plutus`, never in this checkout — so you can delete and
re-clone the repository without losing anything.

```
~/.plutus/
    credentials       your Plaid client_id and secrets
    plaid_data.db     accounts, transactions, holdings, settings
    backups/          the last 7 database copies, taken before each sync
    plutus.log        rotating log
    session_key       signs the dashboard's session cookie
```

Set `PLUTUS_HOME` to move it elsewhere.

## Security

- **Access tokens are encrypted at rest.** The key lives in your operating
  system's credential store (Windows Credential Manager, macOS Keychain,
  Secret Service on Linux), not next to the database, so a copied
  `plaid_data.db` is useless on another machine. Where no OS keystore exists
  the app falls back to a key file and says so in the log.
- **Your bank password never reaches this code.** Plaid Link runs in the
  browser and, for banks that use OAuth, redirects you to the bank's own site.
- **Both servers bind to `127.0.0.1` only** and refuse requests addressed to
  any other hostname, which blocks DNS-rebinding attacks.
- **State-changing requests need a CSRF token**, so a web page you happen to
  have open cannot drive the dashboard behind your back.
- **Secrets are scrubbed from the log**, including inside tracebacks.

There is no login screen, deliberately. The threat model is a hostile *web
page*, not a hostile *user*: anyone with an account on your machine can read
the database directly, and a password on a localhost page would not change
that. Do not expose these ports to a network.

## Everyday use

`python app.py` and press Enter. The dashboard opens immediately with the last
known figures and syncs in the background; a pulsing dot shows it working and
the page refreshes itself when fresh data lands. **Refresh** syncs on demand,
and its dropdown has an auto-refresh toggle.

Plaid serves cached data unless asked otherwise, so every sync explicitly
requests a fresh pull from your institutions first. That is why a sync takes
roughly half a minute.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `REAUTH NEEDED` during sync | The bank invalidated the connection, which happens every few months. Use **[l] Link** and reconnect that institution. |
| Balances look a day old | The refresh did not land in time. Press **Refresh** again. |
| Only ~90 days of transactions | Plaid fixes the history window when an Item is created. New links request the full 2 years; existing ones need re-linking, which costs an Item. |
| `INVALID_API_KEYS` | The `client_id` and secret are from different Plaid accounts, or the secret is for the other environment. Re-run **[c] Configure**. |
| Tokens cannot be decrypted | The database came from another machine or the OS keyring was reset. Re-link your institutions. |
| Everything is empty | No institutions linked yet, or `PLAID_ENV` points at the environment you did not link. |

Logs are in `~/.plutus/plutus.log`.

## Development

Individual components stay runnable on their own:

```bash
python -m source.sync
```

Nothing in this repository should ever contain a credential: they all live
in `~/.plutus`.

## Licence

[PolyForm Noncommercial 1.0.0](LICENSE) — use it, change it and share it
freely for any noncommercial purpose: personal projects, study, research,
and use by charities, schools and government bodies. Commercial use is not
granted by this licence.
