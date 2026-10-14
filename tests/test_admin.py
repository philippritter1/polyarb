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
    monkeypatch.setattr(admin, "geocheck", lambda: {"blocked": False, "country": "DE"})
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


def test_wallet_is_stored_privately_never_shown_and_live_needs_a_release(tmp_path, monkeypatch):
    from arb.orders import read_mode
    srv, base = _server(tmp_path, monkeypatch, None)

    def post(path, **form):
        try:
            with urllib.request.urlopen(base + path, data=urllib.parse.urlencode(form).encode(), timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    key = "ab" * 32
    try:
        assert post("/admin/wallet", key="nope", funder="0x" + "1" * 40, sig_type="1")[0] == 400
        assert post("/admin/wallet", key=key, funder="", sig_type="1")[0] == 400  # proxy wallet needs the funder
        code, body = post("/admin/wallet", key=key, funder="0x" + "1" * 40, sig_type="1")
        assert code == 200 and key not in body and "Wallet gespeichert" in body
        sec = tmp_path / "secrets.env"
        assert stat.S_IMODE(os.stat(sec).st_mode) == 0o600 and f"POLY_PRIVATE_KEY={key}" in sec.read_text()
        # change only the signature type: the stored key and funder stay
        assert post("/admin/wallet", key="", funder="", sig_type="2")[0] == 200
        assert f"POLY_PRIVATE_KEY={key}" in sec.read_text() and "POLY_SIG_TYPE=2" in sec.read_text()
        # no scenario released in config: live stays locked even with a wallet
        monkeypatch.setattr(admin, "released", lambda: [])
        assert post("/admin/live-mode", mode="live")[0] == 400
        with urllib.request.urlopen(base + "/admin/", timeout=5) as r:
            page = r.read().decode()
        assert key not in page and "gesperrt: kein Szenario freigegeben" in page
        # released + wallet: live can be chosen
        monkeypatch.setattr(admin, "released", lambda: ["mm_pilot"])
        assert post("/admin/live-mode", mode="live")[0] == 200 and read_mode(str(tmp_path / "live.json")) == "live"
        # wallet deleted: live locked again
        assert post("/admin/wallet-delete")[0] == 200 and "POLY_PRIVATE_KEY" not in sec.read_text()
        assert post("/admin/live-mode", mode="live")[0] == 400
    finally:
        srv.shutdown()


def test_connection_test_button_needs_a_wallet_and_shows_the_steps(tmp_path, monkeypatch):
    srv, base = _server(tmp_path, monkeypatch, None)
    calls = []
    done = [{"step": "Order stornieren", "ok": True, "info": "ok"}, {"step": "Order weg", "ok": True, "info": True}]

    def fake(self):
        calls.append(1)
        with open(admin.LIVE_TEST_PATH(), "w") as f:
            admin.json.dump({"ts": 1, "steps": done}, f)
        return done
    monkeypatch.setattr(admin.Handler, "run_test", fake)

    def post(path, **form):
        try:
            with urllib.request.urlopen(base + path, data=urllib.parse.urlencode(form).encode(), timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    try:
        assert post("/admin/live-test")[0] == 400 and not calls  # no wallet yet
        post("/admin/wallet", key="ab" * 32, funder="0x" + "1" * 40, sig_type="1")
        code, body = post("/admin/live-test")
        assert code == 200 and "erfolgreich" in body and "Order stornieren" in body and calls == [1]
        assert "✅ bestanden" in body
        assert post("/admin/live-test")[0] == 429  # once a minute at most
    finally:
        srv.shutdown()


def test_reward_test_button(tmp_path, monkeypatch):
    srv, base = _server(tmp_path, monkeypatch, None)
    done = [{"step": "Zählt für Rewards (Polymarkets Prüfung)", "ok": True, "info": "ja (nach 20 s)"},
            {"step": "Order weg", "ok": True, "info": True}]

    def fake(self):
        with open(admin.REWARD_TEST_PATH(), "w") as f:
            admin.json.dump({"ts": 1, "steps": done}, f)
        return done
    monkeypatch.setattr(admin.Handler, "run_reward", fake)

    def post(path, **form):
        try:
            with urllib.request.urlopen(base + path, data=urllib.parse.urlencode(form).encode(), timeout=5) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    try:
        assert post("/admin/reward-test")[0] == 400  # no wallet yet
        post("/admin/wallet", key="ab" * 32, funder="0x" + "1" * 40, sig_type="3")
        code, body = post("/admin/reward-test")
        assert code == 200 and "unser Gebot zählt für Rewards" in body and "ja (nach 20 s)" in body
        assert 'name="sig_type" value="3"' in body  # the stored type is shown, not the default
        assert post("/admin/reward-test")[0] == 429
    finally:
        srv.shutdown()
