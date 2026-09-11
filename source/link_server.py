"""Local Plaid Link server. Run it once per institution, then close it.

    python link_server.py   ->   http://localhost:8000

Bank credentials go to Plaid's widget (or, for banks that use OAuth, to the
bank's own site). They never touch this process.
"""
from flask import Flask, jsonify, render_template_string, request

from . import config, plaid_api, security, store

TRIAL_ITEM_LIMIT = 10  # lifetime, not concurrent: removing an Item does not free a slot

# Everything this app knows how to pull. One of these is the *required*
# product on a new link and the rest ride along as optional_products, which
# Plaid documents as not filtering the institution picker.
ALL_PRODUCTS = ("transactions", "investments")

PAGE = """
<!doctype html><meta charset=utf-8>
<title>PlutusTracker - link an account</title>
<style>
 body{font:15px/1.5 system-ui,sans-serif;max-width:34rem;margin:3rem auto;padding:0 1rem}
 .env{padding:.5rem .75rem;border-radius:6px;font-weight:600;display:inline-block}
 .sandbox{background:#e6f4ea;color:#1e6b34} .production{background:#fdeaea;color:#a11}
 button{font:inherit;padding:.6rem 1.1rem;border-radius:6px;border:1px solid #888;
        background:#111;color:#fff;cursor:pointer}
 select{font:inherit;padding:.5rem}
 li{margin:.35rem 0} .warn{color:#a11}
 button.small{font-size:.82rem;padding:.2rem .5rem;background:#fff;color:#111}
 .note{color:#555;font-size:.9rem}
</style>
<p class="env {{env}}">{{env|upper}}</p>
{% if env == 'production' %}
<p><b>{{used}} of {{limit}}</b> Trial Items used. This counter never goes down.</p>
{% endif %}
<h3>Already linked</h3>
<ul>{% for i in linked %}<li>{{i['institution_name']}} <small>({{i['linked_at'][:10]}})</small>
      <button class=small onclick="addProducts('{{i['item_id']}}')">Add missing products</button></li>
    {% else %}<li><i>nothing yet</i></li>{% endfor %}</ul>
<p class=note><b>Add missing products</b> uses Plaid update mode to grant an existing
   connection something it was not linked for &mdash; a brokerage's cash-account
   transactions, say. It keeps the same Item, so it costs nothing from the Trial
   allowance and nothing already synced is lost.</p>
<h3>Link a new one</h3>
<p>
  <select id=kind>
    <option value=transactions>Bank / card &mdash; transactions + balances</option>
    <option value=investments>Brokerage &mdash; holdings + investment activity</option>
  </select>
</p>
<p><button onclick=go()>Open Plaid Link</button></p>
<p id=msg></p>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
const CSRF = {{ csrf_token|tojson }};
async function go() {
  const kind = document.getElementById('kind').value;
  const msg  = document.getElementById('msg');
  msg.textContent = 'requesting link token...';
  const r = await fetch('/api/link_token?products=' + kind,
      {method: 'POST', headers: {'X-CSRF-Token': CSRF}});
  const d = await r.json();
  if (d.error) { msg.textContent = d.error; msg.className = 'warn'; return; }
  msg.textContent = '';
  Plaid.create({
    token: d.link_token,
    onSuccess: async (public_token) => {
      msg.textContent = 'exchanging token...';
      const res = await fetch('/api/exchange', {
        method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF},
        body: JSON.stringify({public_token: public_token})
      });
      const out = await res.json();
      msg.textContent = out.error ? out.error
                                  : 'Linked ' + out.institution + '. Run: python sync.py';
      if (!out.error) setTimeout(function () { location.reload(); }, 1500);
    },
    onExit: (err) => { if (err) msg.textContent = 'exited: ' + err.error_code; }
  }).open();
}

async function addProducts(itemId) {
  const msg = document.getElementById('msg');
  msg.className = ''; msg.textContent = 'checking what is missing...';
  const r = await fetch('/api/update_token', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': CSRF},
      body: JSON.stringify({item_id: itemId})});
  const d = await r.json();
  if (d.error) { msg.textContent = d.error; msg.className = 'warn'; return; }
  msg.textContent = 'adding ' + d.adding.join(' + ') + '...';
  Plaid.create({
    token: d.link_token,
    // Update mode reuses the existing access token. There is nothing to
    // exchange, and exchanging would read as a relink and purge the Item.
    onSuccess: () => {
      msg.textContent = 'Added ' + d.adding.join(' + ') + ' to ' + d.institution
                      + '. Run a sync to pull it in.';
      setTimeout(function () { location.reload(); }, 2500);
    },
    onExit: (err) => {
      if (err) { msg.textContent = 'exited: ' + err.error_code; msg.className = 'warn'; }
    }
  }).open();
}
</script>
"""

app = Flask(__name__)
# Plaid Link loads from Plaid's CDN, so this app needs the wider policy.
security.harden(app, csp=security.CSP_LINK)


@app.route("/")
def home():
    conn = store.connect()
    linked = store.items(conn, plaid_api.ENV)
    return render_template_string(
        PAGE,
        env=plaid_api.ENV,
        linked=linked,
        used=len(linked),
        limit=TRIAL_ITEM_LIMIT,
    )


def _institution_products(inst_id):
    """What an institution supports, or None when that cannot be determined."""
    if not inst_id:
        return None
    try:
        res = plaid_api.call("/institutions/get_by_id",
                             institution_id=inst_id, country_codes=["US"])
        return set(res["institution"].get("products") or [])
    except plaid_api.PlaidError:
        return None


def _missing_products(access_token, inst_id):
    """Products this app uses that the Item does not carry yet.

    An Item is created against one product family, so a brokerage linked for
    `investments` never pulls its cash account's transactions - the sync just
    reports "n/a" and the money is silently missing from cash flow. Anything
    the institution supports but the Item lacks can be consented to later,
    which is what update mode is for.
    """
    item = plaid_api.call("/item/get", access_token=access_token)["item"]
    have = set()
    for key in ("products", "billed_products", "consented_products"):
        have |= set(item.get(key) or [])
    supported = _institution_products(inst_id)
    return [p for p in ALL_PRODUCTS
            if p not in have and (supported is None or p in supported)]


@app.route("/api/link_token", methods=["POST"])
def link_token():
    # The required product is what filters the institution picker, so ask for
    # exactly one and let the other ride along as optional: institutions that
    # cannot serve it still appear, and the ones that can - a brokerage with a
    # cash account, a bank with a brokerage arm - yield everything from a
    # single Item instead of costing a second one.
    product = request.args.get("products", "transactions")
    if product not in ALL_PRODUCTS:
        return jsonify(error="unknown product {!r}".format(product)), 400
    body = {
        "user": {"client_user_id": "local-user"},
        "client_name": "PlutusTracker",
        "products": [product],
        "optional_products": [p for p in ALL_PRODUCTS if p != product],
        "country_codes": ["US"],
        "language": "en",
        # Default history is 90 days and is fixed at Item creation, so ask for
        # the maximum up front whether transactions is the required product or
        # arrives as an optional one.
        "transactions": {"days_requested": 730},
    }
    try:
        res = plaid_api.call("/link/token/create", **body)
    except (plaid_api.PlaidError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(link_token=res["link_token"])


@app.route("/api/update_token", methods=["POST"])
def update_token():
    """Link token for update mode: give an Item we already hold a product it
    is missing, without creating a second one.

    Linking the same institution again would cost a slot from the lifetime
    Trial allowance and, because /api/exchange treats a repeat institution as
    a relink, would purge the original Item's accounts and history. Update
    mode keeps the same Item and the same access token, so neither happens -
    Plaid bills the new product only once its endpoints are actually called.
    """
    item_id = (request.get_json(silent=True) or {}).get("item_id", "")
    conn = store.connect()
    item = next((dict(i) for i in store.items(conn, plaid_api.ENV)
                 if i["item_id"] == item_id), None)
    if not item:
        return jsonify(error="no such linked institution"), 400
    try:
        missing = _missing_products(item["access_token"], item["institution_id"])
    except plaid_api.PlaidError as exc:
        return jsonify(error=str(exc)), 400
    if not missing:
        return jsonify(error="{} already carries every product this app uses"
                             .format(item["institution_name"])), 400
    try:
        res = plaid_api.call(
            "/link/token/create",
            user={"client_user_id": "local-user"},
            client_name="PlutusTracker",
            access_token=item["access_token"],
            country_codes=["US"],
            language="en",
            # The parameter update mode uses to ask for product consent an
            # Item never had.
            additional_consented_products=missing,
            transactions={"days_requested": 730},
        )
    except (plaid_api.PlaidError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(link_token=res["link_token"], adding=missing,
                   institution=item["institution_name"])


@app.route("/api/exchange", methods=["POST"])
def exchange():
    try:
        res = plaid_api.call(
            "/item/public_token/exchange",
            public_token=request.json["public_token"],
        )
        access_token, item_id = res["access_token"], res["item_id"]
        item = plaid_api.call("/item/get", access_token=access_token)["item"]
        inst_id = item.get("institution_id")
        name = plaid_api.institution_name(inst_id)
    except plaid_api.PlaidError as exc:
        return jsonify(error=str(exc)), 400

    conn = store.connect()

    # Relinking an institution replaces its old Item: remove it at Plaid so
    # the dead connection stops counting, and purge its rows so accounts and
    # transactions aren't double-counted.
    replaced = [dict(i) for i in store.items(conn, plaid_api.ENV)
                if i["institution_id"] == inst_id and i["item_id"] != item_id]
    for old in replaced:
        try:
            plaid_api.call("/item/remove", access_token=old["access_token"])
        except plaid_api.PlaidError as exc:
            print("item/remove for old {} item: {}".format(name, exc.code))
        store.remove_item(conn, old["item_id"])

    store.save_item(conn, item_id, access_token, inst_id, name, plaid_api.ENV)
    return jsonify(institution=name, item_id=item_id,
                   replaced=len(replaced))


if __name__ == "__main__":
    print("\n  {}  ->  http://localhost:8000\n".format(plaid_api.ENV.upper()))
    config.serve(app, 8000, "Link server")
