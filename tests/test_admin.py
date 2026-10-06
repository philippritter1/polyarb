import os
import stat
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))

import admin  # noqa: E402
from arb.sharp import read_key  # noqa: E402


def _server(tmp_path, monkeypatch, fetch):
    monkeypatch.setattr(admin, "DATA", str(tmp_path))
    monkeypatch.setattr(admin, "SECRETS", str(tmp_path / "secrets.env"))
    monkeypatch.setattr(admin.Handler, "fetch", staticmethod(fetch) if fetch else None)
    srv = admin.serve(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def _post(base, key):
    data = urllib.parse.urlencode({"key": key}).encode()
    try:
        with urllib.request.urlopen(base + "/admin/odds-key", data=data, timeout=5) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def test_key_is_stored_privately_and_never_shown(tmp_path, monkeypatch):
    srv, base = _server(tmp_path, monkeypatch, fetch=lambda url: None)  # The Odds API accepts it
    try:
        key = "abcdef0123456789abcdef0123456789"
        code, body = _post(base, key)
        assert code == 200 and "Gespeichert" in body and key not in body and "…6789" in body
        f = tmp_path / "secrets.env"
        assert f.read_text() == f"ODDS_API_KEY={key}\n"
        assert stat.S_IMODE(os.stat(f).st_mode) == 0o600
        assert read_key(str(f)) == key  # what arb/sharp.py reads
        with urllib.request.urlopen(base + "/admin/", timeout=5) as r:
            page = r.read().decode()
        assert key not in page and "…6789" in page
        code, body = _post(base, "nope; rm -rf /")
        assert code == 400 and (tmp_path / "secrets.env").read_text() == f"ODDS_API_KEY={key}\n"
        with urllib.request.urlopen(base + "/admin/", timeout=5):
            pass
        try:
            urllib.request.urlopen(base + "/etc/passwd", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()


def test_rejected_key_is_not_stored(tmp_path, monkeypatch):
    def reject(url):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)
    srv, base = _server(tmp_path, monkeypatch, fetch=reject)
    try:
        code, body = _post(base, "abcdef0123456789abcdef0123456789")
        assert code == 400 and "abgelehnt" in body and not (tmp_path / "secrets.env").exists()
    finally:
        srv.shutdown()
