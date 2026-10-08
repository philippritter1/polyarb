import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy"))

import admin  # noqa: E402
import diag  # noqa: E402

UNITS = """polyarb.service loaded active running Polyarb bot
polyarb-scenario@mm_rewards.service loaded failed failed Polyarb paper scenario mm_rewards
polyarb-dash.timer loaded active waiting dash
evil;rm.service loaded active running not ours
"""


def _fake(calls):
    def run(argv):
        calls.append(argv)
        if argv[:2] == ["systemctl", "list-units"]:
            return UNITS
        if argv[:2] == ["systemctl", "show"]:
            return "ActiveState=active\nResult=success\nNRestarts=3\nMemoryCurrent=52000000\n"
        if argv[0] == "journalctl":
            return ("10:00 GET https://api.the-odds-api.com/v4/sports?apiKey=abcdef1234567890XYZ&regions=eu\n"
                    "10:01 token: s3cr3tvalue push to bot123456:AAAAAAAAAAAAAAAAAAAAAAAAAA\n"
                    "10:02 pass DASHPASSWORD123 in plain text\n")
        return ""
    return run


def test_redacts_secrets_and_only_reads_known_units(tmp_path):
    env = tmp_path / "polyarb.env"
    env.write_text("DASH_PASS=DASHPASSWORD123\nDASH_USER=x\n")
    calls = []
    d = diag.Diag(str(tmp_path), run=_fake(calls), env_files=[str(env)])
    assert [u["unit"] for u in d.units()] == ["polyarb.service", "polyarb-scenario@mm_rewards.service",
                                              "polyarb-dash.timer"]
    out = d.logs("polyarb.service", 9999)
    for s in ("abcdef1234567890XYZ", "s3cr3tvalue", "AAAAAAAAAAAAAAAA", "DASHPASSWORD123"):
        assert s not in out
    assert "regions=eu" in out and "-n" in calls[-1] and calls[-1][calls[-1].index("-n") + 1] == "300"
    assert d.logs("sshd.service") == "unbekannte Unit" and d.logs("evil;rm.service") == "unbekannte Unit"
    assert d.show("evil;rm.service") == {}
    assert all(c[0] in ("systemctl", "journalctl") for c in calls)
    s = d.summary()
    assert s["failed"] == ["polyarb-scenario@mm_rewards.service"] and s["units"][0]["mem_mb"] == 52


def test_diag_pages_are_read_only(tmp_path, monkeypatch):
    (tmp_path / "secrets.env").write_text("ODDS_API_KEY=abcdef1234567890XYZ\n")
    (tmp_path / "scenario-x.sqlite").write_bytes(b"0" * 2_000_000)
    monkeypatch.setattr(admin, "DATA", str(tmp_path))
    monkeypatch.setattr(admin.Handler, "run", staticmethod(_fake([])))
    srv = admin.serve(port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/admin/diag", timeout=5) as r:
            body = r.read().decode()
        assert "Server-Diagnose" in body and "scenario-x.sqlite" in body and "secrets.env" not in body
        assert "abcdef1234567890XYZ" not in body
        with urllib.request.urlopen(base + "/admin/diag.json", timeout=5) as r:
            assert json.loads(r.read())["failed"] == ["polyarb-scenario@mm_rewards.service"]
        with urllib.request.urlopen(base + "/admin/diag/log?unit=polyarb.service&n=50", timeout=5) as r:
            body = r.read().decode()
        assert "regions=eu" in body and "abcdef1234567890XYZ" not in body
        try:
            urllib.request.urlopen(base + "/admin/diag", data=b"x=1", timeout=5)
            assert False
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.shutdown()
