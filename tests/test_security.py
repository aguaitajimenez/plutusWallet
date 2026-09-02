"""The guards that stand between a hostile web page and the user's bank data.

The dashboard listens on localhost, which is reachable by any site the user
visits. Every check here corresponds to an attack that would otherwise work.
"""
import re

import pytest

from source import crypto, dashboard, link_server, security, store


@pytest.fixture
def client():
    return dashboard.app.test_client()


def csrf_of(client):
    body = client.get("/").get_data(as_text=True)
    return re.search(r'name=csrf value="([^"]+)"', body).group(1)


# --- cross-site request forgery ---------------------------------------------

def test_state_change_without_a_token_is_refused(client):
    assert client.post("/refresh", data={"next": "/"}).status_code == 403
    assert client.post("/settings", data={"target": "90"}).status_code == 403


def test_state_change_with_the_right_token_succeeds(client):
    token = csrf_of(client)
    assert client.post("/settings",
                       data={"target": "65", "floor": "2000", "csrf": token}
                       ).status_code == 302


def test_a_forged_token_is_refused(client):
    csrf_of(client)
    assert client.post("/settings",
                       data={"target": "1", "csrf": "not-the-real-token"}
                       ).status_code == 403


def test_a_cross_origin_post_is_refused(client):
    token = csrf_of(client)
    assert client.post("/settings", data={"target": "1", "csrf": token},
                       headers={"Origin": "https://evil.example"}
                       ).status_code == 403


def test_link_server_json_endpoints_are_protected():
    assert link_server.app.test_client().post("/api/link_token").status_code == 403
    assert link_server.app.test_client().post(
        "/api/exchange", json={"public_token": "x"}).status_code == 403


# --- DNS rebinding ----------------------------------------------------------

def test_only_loopback_hosts_are_served(client):
    assert client.get("/", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/", headers={"Host": "localhost:8001"}).status_code == 200
    assert client.get("/", headers={"Host": "127.0.0.1:8001"}).status_code == 200


def test_host_allow_list_rejects_lookalikes():
    assert not security.host_is_local("localhost.evil.example")
    assert not security.host_is_local("127.0.0.1.evil.example")
    assert not security.host_is_local("")
    assert security.host_is_local("127.0.0.1:8001")


# --- open redirect ----------------------------------------------------------

@pytest.mark.parametrize("hostile", ["//evil.example", "/\\evil.example",
                                     "https://evil.example", "javascript:alert(1)"])
def test_refresh_will_not_bounce_to_another_site(hostile):
    assert security.safe_redirect_target(hostile, "/") == "/"


def test_refresh_keeps_a_genuine_local_path():
    assert security.safe_redirect_target("/investments", "/") == "/investments"


def test_redirect_target_is_enforced_end_to_end(client):
    token = csrf_of(client)
    resp = client.post("/refresh", data={"next": "//evil.example", "csrf": token})
    assert resp.headers["Location"].endswith("/")
    assert "evil.example" not in resp.headers["Location"]


# --- cross-site scripting ---------------------------------------------------

def test_no_page_builds_html_from_data(client):
    """Merchant and category names come from Plaid; they are text, never markup."""
    for path in ("/", "/cashflow", "/investments", "/subscriptions"):
        assert "innerHTML" not in client.get(path).get_data(as_text=True)


def test_a_merchant_named_like_a_script_tag_is_escaped(linked, client):
    from conftest import add_txns
    add_txns(linked, [("evil", "acc_check", "2026-08-01",
                       "<img src=x onerror=alert(1)>", 10.0, "Shops")])
    body = client.get("/cashflow").get_data(as_text=True)
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body


# --- response headers -------------------------------------------------------

def test_security_headers_are_present(client):
    h = client.get("/").headers
    assert h["X-Content-Type-Options"] == "nosniff"
    assert h["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in h["Content-Security-Policy"]
    assert h["Cache-Control"] == "no-store"


def test_link_server_may_load_plaid_but_the_dashboard_may_not():
    link_csp = link_server.app.test_client().get("/").headers["Content-Security-Policy"]
    dash_csp = dashboard.app.test_client().get("/").headers["Content-Security-Policy"]
    assert "cdn.plaid.com" in link_csp
    assert "cdn.plaid.com" not in dash_csp


# --- secrets at rest --------------------------------------------------------

def test_access_tokens_are_encrypted_in_the_database(conn):
    store.save_item(conn, "i1", "access-sandbox-supersecret", "ins", "Bank", "sandbox")
    raw = conn.execute("SELECT access_token FROM items").fetchone()[0]

    assert "access-sandbox-supersecret" not in raw
    assert raw.startswith(crypto.PREFIX)
    assert store.items(conn, "sandbox")[0]["access_token"] == "access-sandbox-supersecret"


def test_plaintext_tokens_are_upgraded_on_first_read(conn):
    """Databases created before encryption existed must not need a manual step."""
    conn.execute("INSERT INTO items (item_id, access_token, env) VALUES (?,?,?)",
                 ("old", "access-sandbox-legacy", "sandbox"))
    conn.commit()

    assert store.items(conn, "sandbox")[0]["access_token"] == "access-sandbox-legacy"
    raw = conn.execute("SELECT access_token FROM items").fetchone()[0]
    assert raw.startswith(crypto.PREFIX)


def test_a_database_copied_without_its_key_is_useless(conn):
    store.save_item(conn, "i1", "access-sandbox-secret", "ins", "Bank", "sandbox")
    stolen = conn.execute("SELECT access_token FROM items").fetchone()[0]

    from cryptography.fernet import Fernet
    original = crypto._cached_key
    crypto._cached_key = Fernet.generate_key()      # another machine's key
    try:
        with pytest.raises(RuntimeError, match="another machine"):
            crypto.decrypt(stolen)
    finally:
        crypto._cached_key = original


def test_secrets_are_scrubbed_from_the_log(caplog):
    from source import config, plaid_api
    config.setup_logging()
    plaid_api.log.addFilter(config._Redactor())
    with caplog.at_level("INFO"):
        plaid_api.log.info("token access-production-abc123def456 leaked")
    assert "abc123def456" not in caplog.text
