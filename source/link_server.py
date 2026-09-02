"""Local Plaid Link server. Run it once per institution, then close it.

    python link_server.py   ->   http://localhost:8000

Bank credentials go to Plaid's widget (or, for OAuth banks like Chase, to the
bank's own site). They never touch this process.
"""
from flask import Flask, jsonify, render_template_string, request

from . import plaid_api, store

TRIAL_ITEM_LIMIT = 10  # lifetime, not concurrent: removing an Item does not free a slot

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
 li{margin:.2rem 0} .warn{color:#a11}
</style>
<p class="env {{env}}">{{env|upper}}</p>
{% if env == 'production' %}
<p><b>{{used}} of {{limit}}</b> Trial Items used. This counter never goes down.</p>
{% endif %}
<h3>Already linked</h3>
<ul>{% for i in linked %}<li>{{i['institution_name']}} <small>({{i['linked_at'][:10]}})</small></li>
    {% else %}<li><i>nothing yet</i></li>{% endfor %}</ul>
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
async function go() {
  const kind = document.getElementById('kind').value;
  const msg  = document.getElementById('msg');
  msg.textContent = 'requesting link token...';
  const r = await fetch('/api/link_token?products=' + kind, {method: 'POST'});
  const d = await r.json();
  if (d.error) { msg.innerHTML = '<span class=warn>' + d.error + '</span>'; return; }
  msg.textContent = '';
  Plaid.create({
    token: d.link_token,
    onSuccess: async (public_token) => {
      msg.textContent = 'exchanging token...';
      const res = await fetch('/api/exchange', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
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
</script>
"""

app = Flask(__name__)


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


@app.route("/api/link_token", methods=["POST"])
def link_token():
    # Requesting several products at once hides every institution that does not
    # support all of them, so link one product family at a time.
    product = request.args.get("products", "transactions")
    body = {
        "user": {"client_user_id": "local-user"},
        "client_name": "PlutusTracker",
        "products": [product],
        "country_codes": ["US"],
        "language": "en",
    }
    if product == "transactions":
        # Default history is only 90 days and is fixed at Item creation;
        # ask for the maximum two years up front.
        body["transactions"] = {"days_requested": 730}
    try:
        res = plaid_api.call("/link/token/create", **body)
    except (plaid_api.PlaidError, RuntimeError) as exc:
        return jsonify(error=str(exc)), 400
    return jsonify(link_token=res["link_token"])


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
    app.run(port=8000, debug=False)
