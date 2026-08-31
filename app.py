"""PlutusTracker - single entry point for everything.

    python app.py

Menu-driven: configure Plaid credentials, open the dashboard (syncs on entry;
the page's Refresh button pulls after that, with an optional auto-refresh
toggle in its dropdown), link a new institution, or just sync. Ctrl+C inside a
mode returns to the menu.
"""
import getpass
import webbrowser

import plaid_api
import store
import sync


def _safe_sync(label):
    try:
        sync.main()
    except Exception as exc:  # never let a flaky connection kill the app
        print("{} failed: {}".format(label, exc))


def configure():
    """Prompt for a Plaid client_id/secret pair and store it in ~/.plutusTracker."""
    print("\nPlaid credentials -> {}".format(plaid_api.CONFIG_PATH))
    print("Find them at https://dashboard.plaid.com/developers/keys")
    print("(leave the Client ID blank to cancel)\n")
    try:
        client_id = input("  Client ID: ").strip()
        if not client_id:
            print("  Cancelled.")
            return
        secret = getpass.getpass("  Secret (hidden while typing): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n  Cancelled.")
        return
    if not secret:
        print("  No secret entered; nothing saved.")
        return

    print("\n  Verifying (this creates no Item and costs nothing)...")
    env, detail = plaid_api.detect_env(client_id, secret)
    if env is None:
        print("  Rejected by Plaid: {}".format(detail or "unknown error"))
        print("  Nothing saved - check the client_id and secret belong together.")
        return

    plaid_api.save_credentials(client_id, secret, env)
    print("  Verified as a {} secret. Saved; active environment is now {}.".format(
        env, plaid_api.ENV))
    print("  Run [c] again to add the secret for the other environment.")


def run_dashboard():
    import dashboard

    if plaid_api.have_credentials():
        print("\nSyncing before opening...")
        _safe_sync("sync")
    else:
        print("\nNo credentials yet - opening with whatever is already stored.")

    url = "http://localhost:8001"
    print("\nDashboard -> {}   (Ctrl+C to return to menu)\n".format(url))
    webbrowser.open(url)
    try:
        dashboard.app.run(port=8001, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    print("\nBack to menu.")


def run_link():
    import link_server

    url = "http://localhost:8000"
    print("\nLink server -> {}   (Ctrl+C to return to menu)\n".format(url))
    webbrowser.open(url)
    try:
        link_server.app.run(port=8000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    print("\nBack to menu.")


def banner():
    conn = store.connect()
    items = store.items(conn, plaid_api.ENV)
    names = ", ".join(i["institution_name"] for i in items) or "none"
    print("\n" + "=" * 52)
    print("  PlutusTracker   [{}]".format(plaid_api.ENV))
    if plaid_api.have_credentials():
        print("  Linked: {}".format(names))
    else:
        print("  No Plaid credentials - choose [c] to add them.")
    print("=" * 52)


def main():
    while True:
        banner()
        print("""
  [Enter]  Dashboard   (syncs on entry; Refresh button after)
  [l]      Link a new institution
  [s]      Sync only
  [c]      Configure Plaid credentials
  [q]      Quit
""")
        try:
            choice = input("> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return

        if choice == "c":
            configure()
        elif choice == "q":
            return
        elif choice in ("", "d"):
            run_dashboard()
        elif choice in ("l", "s") and not plaid_api.have_credentials():
            print("\n  Plaid credentials required first - choose [c].")
        elif choice == "l":
            run_link()
        elif choice == "s":
            _safe_sync("sync")


if __name__ == "__main__":
    main()
