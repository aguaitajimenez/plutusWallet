# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**PlutusTracker** — a personal, local-only tool that pulls the owner's own financial data (Chase transactions, Wealthfront holdings) through the Plaid API into a SQLite file, and serves a local dashboard over it. Read-only: no money-movement products are used or wanted.

## Commands

```bash
pip install -r requirements.txt
python app.py            # TUI entry point: configure credentials, dashboard, link, sync
sqlite3 plaid_data.db    # inspect results
```

First run needs `[c] Configure` to store a Plaid `client_id`/secret pair. The secret's environment is detected automatically by minting a throwaway link token against both hosts (creates no Item, costs nothing).

`link_server.py`, `sync.py`, and `dashboard.py` remain runnable individually; `app.py` just orchestrates them (sync on entry + browser launch). Refreshing after that is the page's Refresh button, which runs the sync in-process; its dropdown has an auto-refresh toggle (client-side timer that re-submits the same form, persisted in localStorage).

There is no test suite. The smoke check is that modules import and the credential guard fires:

```bash
python -c "import plaid_api, store, link_server, sync; store.connect(); print(plaid_api.ENV)"
```

## Architecture

Two phases, deliberately separated:

1. **Linking** (`link_server.py`) — a throwaway local Flask server whose only job is to mint an `access_token`. Plaid Link runs in the browser because credentials must reach Plaid, never this code. Run it, link, close it.
2. **Syncing** (`sync.py`) — headless, uses stored tokens forever after. This is the part that runs repeatedly.

The `access_token` is the durable artifact; everything else in phase 1 is scaffolding to obtain it.

`plaid_api.call()` posts to Plaid's REST API with `client_id`/`secret` injected. Plain `requests` rather than `plaid-python` — the typed SDK's model imports change shape between releases and add nothing at this size.

### Environment switching

`plaid_api.ENV`, `CLIENT_ID`, and `SECRET` are module-level globals resolved by `plaid_api.reload()` — at import, and again whenever credentials are saved. Other modules must read them as attributes (`plaid_api.ENV`), never `from plaid_api import ENV`, or they will pin a stale value. `items.env` scopes rows by environment, so sandbox and production tokens coexist in one database without colliding — every query for Items must filter on env (`store.items(conn, plaid_api.ENV)`).

Credential precedence: `~/.plutusTracker` first, then a project `.env` (kept as a fallback for older setups), then the process environment.

### One product family per Item

`/link/token/create` with multiple products hides every institution that doesn't support *all* of them. Requesting `transactions` + `investments` together would remove Wealthfront from the picker. Hence the dropdown in the Link page: banks link as `transactions`, brokerages as `investments`, each producing its own Item.

Consequently `sync.py` doesn't know what an Item supports. It attempts all three pulls against every Item and treats the error codes in `SKIPPABLE` as "n/a" rather than failure. `ITEM_LOGIN_REQUIRED` is called out separately because it means the connection genuinely needs re-authentication.

Transactions are incremental via a cursor stored on the Item row. Holdings are a full snapshot, upserted.

## Constraints that are not visible in the code

- **The Plaid Trial plan allows 10 Production Items, lifetime.** Removing an Item does *not* free a slot. Never develop or debug against production — use Sandbox, which is free and unlimited.
- **Never apply for full Production access.** A pending or approved Production application disqualifies the team from the free Trial plan permanently.
- **Chase relinking is destructive.** Creating a new Item for Chase invalidates the existing one when the account sets differ. Reuse the stored token; don't relink casually.
- Sandbox credentials are `user_good` / `pass_good`, MFA code `1234`.

## Secrets

Credentials live in `~/.plutusTracker` (outside the repo). `.env` and `*.db` are gitignored. Note that `plaid_data.db` contains access tokens in plaintext — treat the database file itself as a credential, not just as data.

Access tokens are bound to the `client_id` that created them. Swapping in a different `client_id` invalidates every stored token, and re-linking costs Items from the lifetime Trial allowance.
