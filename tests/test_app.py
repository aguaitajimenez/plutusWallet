"""The menu in app.py, which no other test touches.

Both bugs these guard against were real and hit users on their first run: a
stale `plaid_api.CONFIG_PATH` that crashed [c] Configure, and a logger that
sync.py used without ever defining. Neither is reachable from the rest of the
suite, so nothing caught them.
"""
import ast
import importlib
import pathlib

import app

from source import config

ROOT = pathlib.Path(__file__).resolve().parent.parent
MODULES = ("config", "plaid_api", "store", "sync", "crypto", "security",
           "dashboard", "link_server")


def test_configure_shows_the_credential_path_and_can_be_cancelled(monkeypatch,
                                                                  capsys):
    """[c] Configure died with AttributeError before it read any input."""
    monkeypatch.setattr("builtins.input", lambda *a: "")

    app.configure()

    out = capsys.readouterr().out
    assert str(config.credentials_path()) in out
    assert "Cancelled" in out


def test_no_module_attribute_reference_is_stale():
    """Every `module.attr` in the package must resolve.

    Paths moved into config.py during the package refactor and one caller was
    left pointing at an attribute that no longer existed. A sweep is cheap and
    catches the whole class, including the lines no test executes.
    """
    mods = {n: importlib.import_module("source." + n) for n in MODULES}
    stale = []
    for path in [ROOT / "app.py"] + sorted((ROOT / "source").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in mods
                    and not hasattr(mods[node.value.id], node.attr)):
                stale.append("{}:{} {}.{}".format(
                    path.name, node.lineno, node.value.id, node.attr))
    assert stale == [], "stale module references: {}".format(stale)
