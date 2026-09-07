"""PlutusTracker - single entry point for everything.

    python app.py

Menu-driven: configure Plaid credentials, open the dashboard (syncs on entry;
the page's Refresh button pulls after that, with an optional auto-refresh
toggle in its dropdown), link a new institution, or just sync. Ctrl+C inside a
mode returns to the menu. Menu keys act on the keypress itself - no Enter.
"""
import getpass
import os
import sys
import webbrowser

from source import config, plaid_api, store, sync


def read_key():
    """Wait for one keypress and return it lowercased - no Enter required.

    Menu commands are single letters, so making the user confirm each one with
    Enter is a keystroke of pure ceremony. Returns "" for Enter and for keys
    carrying no printable character (arrows, function keys); the caller already
    ignores anything it does not recognise.

    When stdin is not a terminal - piped input, a test harness - there is no
    keypress to wait for, so fall back to a whole line and keep its first
    character.
    """
    if not sys.stdin.isatty():
        line = sys.stdin.readline()
        if not line:
            raise EOFError
        return line.strip().lower()[:1]

    if os.name == "nt":
        import msvcrt

        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):  # arrow/function key: drop its scan code too
            msvcrt.getwch()
            return ""
        if ch == "\x03":  # getwch reads Ctrl+C as data, never as a signal
            raise KeyboardInterrupt
        if ch == "\x04":
            raise EOFError
        return "" if ch in ("\r", "\n") else ch.lower()

    import termios
    import tty

    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)  # cbreak rather than raw, so Ctrl+C still signals
        ch = sys.stdin.read(1)
        if ch == "\x1b":  # escape sequence: swallow the rest before restoring
            import select

            while select.select([fd], [], [], 0)[0]:
                sys.stdin.read(1)
            return ""
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    if not ch:
        raise EOFError
    return "" if ch in ("\r", "\n") else ch.lower()


def _safe_sync(label):
    try:
        sync.main()
    except Exception as exc:  # never let a flaky connection kill the app
        print("{} failed: {}".format(label, exc))


def configure():
    """Prompt for a Plaid client_id/secret pair and store it in ~/.plutus."""
    print("\nPlaid credentials -> {}".format(config.credentials_path()))
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
    from source import dashboard

    # Sync on a background thread so the page opens immediately with the last
    # known values; it reloads itself when the fresh ones land.
    if plaid_api.have_credentials():
        dashboard.start_sync()
        note = "syncing in the background; the page refreshes when it lands"
    else:
        note = "no credentials - showing stored data only"

    url = "http://localhost:8001"
    print("\nDashboard -> {}   ({})".format(url, note))
    print("   (Ctrl+C to return to menu)\n")
    webbrowser.open(url)
    try:
        config.serve(dashboard.app, 8001, "Dashboard")
    except KeyboardInterrupt:
        pass
    print("\nBack to menu.")


def run_link():
    from source import link_server

    url = "http://localhost:8000"
    print("\nLink server -> {}   (Ctrl+C to return to menu)\n".format(url))
    webbrowser.open(url)
    try:
        config.serve(link_server.app, 8000, "Link server")
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
    config.setup_logging()
    for note in config.ensure_home():
        print("  {}".format(note))
    while True:
        banner()
        print("""
  [Enter]  Dashboard   (syncs on entry; Refresh button after)
  [l]      Link a new institution
  [c]      Configure Plaid credentials
  [q]      Quit
""")
        print("> ", end="", flush=True)
        try:
            choice = read_key()
        except (KeyboardInterrupt, EOFError):
            print()
            return
        print(choice)  # a keypress is not echoed, so show what was pressed

        if choice == "c":
            configure()
        elif choice == "q":
            return
        elif choice in ("", "d"):
            run_dashboard()
        elif choice == "l" and not plaid_api.have_credentials():
            print("\n  Plaid credentials required first - choose [c].")
        elif choice == "l":
            run_link()


if __name__ == "__main__":
    main()
