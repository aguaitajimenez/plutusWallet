# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**PlutusTracker** — a personal, local-only tool that pulls a user's own bank and brokerage data through the Plaid API into a SQLite file, and serves a local dashboard over it. Read-only: no money-movement products are used or wanted.

## Commands

```bash
pip install -r requirements.txt
python app.py                      # TUI entry point: configure credentials, dashboard, link
sqlite3 ~/.plutus/plaid_data.db    # inspect results
```

First run needs `[c] Configure` to store a Plaid `client_id`/secret pair. The secret's environment is detected automatically by minting a throwaway link token against both hosts (creates no Item, costs nothing).

Everything else lives in the `source/` package and stays individually runnable — `python -m source.sync`, `python -m source.dashboard`, `python -m source.link_server` (module form, not `python source/sync.py`, because of the relative imports). `app.py` just orchestrates them.

Layout: `app.py` (TUI) at the root; `source/` holds `config` (paths, permissions, logging), `plaid_api` (REST wrapper + credential store), `store` (SQLite), `sync`, `link_server`, and `dashboard`.

Nothing user-specific lives in the checkout. `source/config.py` owns the layout under `~/.plutus/` — `credentials`, `plaid_data.db`, `backups/`, `plutus.log`, `session_key` — so the repo stays disposable. Always address those through `config.db_path()` / `config.credentials_path()`, never by joining paths from `__file__`. `PLUTUS_HOME` relocates the whole directory. Pre-1.0 locations (`~/.plutusTracker`, `<project>/plaid_data.db`) are migrated by `config.ensure_home()` on first run, which copies rather than moves.

`tests/conftest.py` points `PLUTUS_HOME` at a temp directory, sets `PLUTUS_NO_KEYRING`, and stubs `plaid_api.call` **at module scope** — before any `source` module is imported. Do not move that into a fixture: `ensure_home()` runs on first import, and an import that beat the stub would read, and encrypt, the developer's real database.

## Architecture

Two phases, deliberately separated:

1. **Linking** (`link_server.py`) — a throwaway local Flask server whose only job is to mint an `access_token`. Plaid Link runs in the browser because credentials must reach Plaid, never this code. Run it, link, close it.
2. **Syncing** (`sync.py`) — headless, uses stored tokens forever after. This is the part that runs repeatedly.

The `access_token` is the durable artifact; everything else in phase 1 is scaffolding to obtain it.

`plaid_api.call()` posts to Plaid's REST API with `client_id`/`secret` injected. Plain `requests` rather than `plaid-python` — the typed SDK's model imports change shape between releases and add nothing at this size.

### Environment switching

`plaid_api.ENV`, `CLIENT_ID`, and `SECRET` are module-level globals resolved by `plaid_api.reload()` — at import, and again whenever credentials are saved. Other modules must read them as attributes (`plaid_api.ENV`), never `from plaid_api import ENV`, or they will pin a stale value. `items.env` scopes rows by environment, so sandbox and production tokens coexist in one database without colliding — every query for Items must filter on env (`store.items(conn, plaid_api.ENV)`).

Credential precedence: `~/.plutus/credentials` first, then the process environment. `plaid_api.call()` retries 429 and 5xx with backoff; a `PlaidError` is not retried because an expired login will not fix itself.

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

Credentials and the database both live in `~/.plutus/` (outside the repo), locked to the owner by `config.restrict()` — POSIX modes elsewhere, `icacls` on Windows, best-effort either way. `.env` and `*.db` are gitignored as a backstop.

Access tokens are encrypted at rest by `source/crypto.py` (Fernet, `enc1:` prefix), with the key in the OS credential store via `keyring` and a key-file fallback; `crypto.key_source()` reports which is active. `store.items()` decrypts transparently and re-encrypts any plaintext rows it finds, so callers always see usable tokens — never write a raw token into `items.access_token`. `config.setup_logging()` installs a filter that redacts secrets and Plaid tokens from log output.

Access tokens are bound to the `client_id` that created them. Swapping in a different `client_id` invalidates every stored token, and re-linking costs Items from the lifetime Trial allowance.
