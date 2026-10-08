"""Read-only server diagnostics for the settings page (/admin/diag, /admin/diag.json).

Shows what otherwise needs an SSH console: state of the polyarb services and timers, memory, disk, load, the
size of the data files and the tail of a service's log. It only runs a fixed list of read-only queries
(systemctl show/list-units/list-timers, journalctl) with argument lists, never a shell; the only input is the
unit name, which must be one of the polyarb units systemd itself lists, and a line count (capped).

Logs can contain secrets (an exception about a failed request may print the URL with ?apiKey=…). Every line
shown is redacted: all values from /etc/polyarb.env and data/secrets.env, and anything that looks like
key=…/token=…/password=… or a bot token.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from typing import Callable, Dict, List, Optional

UNIT_RE = re.compile(r"^polyarb[A-Za-z0-9_@.\-]*\.(service|timer)$")
ENV_FILES = ("/etc/polyarb.env",)
MAX_LINES = 300
SHOW_PROPS = "ActiveState,SubState,Result,ActiveEnterTimestamp,NRestarts,MemoryCurrent,ExecMainStatus"
PATTERNS = [
    re.compile(r"(?i)\b(api[_-]?key|apikey|key|token|secret|pass(?:word)?|auth(?:orization)?)(\s*[=:]\s*)([^\s&\"',;]+)"),
    re.compile(r"\bbot\d+:[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)(basic|bearer)\s+[A-Za-z0-9+/=._\-]{8,}"),
]


def _run(argv: List[str]) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=15, check=False)
        return r.stdout if r.returncode == 0 or r.stdout else (r.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return f"nicht verfügbar ({type(e).__name__})"


class Diag:
    def __init__(self, data_dir: str, run: Callable[[List[str]], str] = _run, env_files=ENV_FILES):
        self.data, self.run = data_dir, run
        self.env_files = list(env_files) + [os.path.join(data_dir, "secrets.env")]

    # ------------------------------------------------------------------ redaction
    def secrets(self) -> List[str]:
        out = []
        for path in self.env_files:
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        v = line.strip().partition("=")[2].strip().strip("'\"")
                        if len(v) >= 6:
                            out.append(v)
            except OSError:
                pass
        return sorted(set(out), key=len, reverse=True)

    def redact(self, text: str, secrets: Optional[List[str]] = None) -> str:
        for s in self.secrets() if secrets is None else secrets:
            text = text.replace(s, "•••")
        text = PATTERNS[0].sub(lambda m: m.group(1) + m.group(2) + "•••", text)
        text = PATTERNS[1].sub("bot•••", text)
        return PATTERNS[2].sub(lambda m: m.group(1) + " •••", text)

    # ------------------------------------------------------------------ queries
    def units(self) -> List[dict]:
        out = []
        for line in self.run(["systemctl", "list-units", "polyarb*", "--all", "--no-legend", "--plain",
                              "--no-pager"]).splitlines():
            p = line.split(None, 4)
            if len(p) >= 4 and UNIT_RE.match(p[0]):
                out.append(dict(unit=p[0], load=p[1], active=p[2], sub=p[3], desc=p[4] if len(p) > 4 else ""))
        return out

    def show(self, unit: str) -> Dict[str, str]:
        if not UNIT_RE.match(unit):
            return {}
        txt = self.run(["systemctl", "show", unit, "-p", SHOW_PROPS, "--no-pager"])
        return dict(l.split("=", 1) for l in txt.splitlines() if "=" in l)

    def timers(self) -> str:
        return self.run(["systemctl", "list-timers", "polyarb*", "--all", "--no-pager"])

    def logs(self, unit: str, lines: int = 80, warnings_only: bool = False) -> str:
        known = {u["unit"] for u in self.units()}
        if unit not in known:
            return "unbekannte Unit"
        argv = ["journalctl", "-u", unit, "-n", str(max(1, min(int(lines), MAX_LINES))), "--no-pager", "-o", "short-iso"]
        if warnings_only:
            argv += ["-p", "warning"]
        return self.redact(self.run(argv))

    def problems(self, hours: int = 24) -> str:
        """Warnings and errors of all polyarb units in the last hours (newest 150)."""
        return self.redact(self.run(["journalctl", "-u", "polyarb*", "-p", "warning", "--since", f"-{int(hours)}h",
                                     "-n", "150", "--no-pager", "-o", "short-iso"]))

    def system(self) -> dict:
        mem = {}
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    k, _, v = line.partition(":")
                    if k in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                        mem[k] = round(int(v.split()[0]) / 1024)
        except (OSError, ValueError):
            pass
        disk = {}
        for name, path in (("/", "/"), ("data", self.data)):
            try:
                u = shutil.disk_usage(path)
                disk[name] = dict(total_gb=round(u.total / 1e9, 1), free_gb=round(u.free / 1e9, 1),
                                  used_pct=round(100 * u.used / u.total, 1))
            except OSError:
                pass
        try:
            load = [round(x, 2) for x in os.getloadavg()]
        except OSError:
            load = []
        try:
            with open("/proc/uptime", encoding="utf-8") as f:
                up_h = round(float(f.read().split()[0]) / 3600, 1)
        except (OSError, ValueError):
            up_h = None
        return dict(mem_mb=mem, disk=disk, load=load, uptime_h=up_h, cpus=os.cpu_count())

    def files(self, top: int = 25) -> List[dict]:
        """Largest files in the data directory (no contents, secrets.env and *.env left out)."""
        out = []
        for root, dirs, names in os.walk(self.data):
            dirs[:] = [d for d in dirs if not d.startswith("archive-")]
            for n in names:
                if n.endswith(".env"):
                    continue
                p = os.path.join(root, n)
                try:
                    st = os.stat(p)
                except OSError:
                    continue
                out.append(dict(file=os.path.relpath(p, self.data), mb=round(st.st_size / 1e6, 1),
                                age_min=round((time.time() - st.st_mtime) / 60)))
        return sorted(out, key=lambda x: -x["mb"])[:top]

    def summary(self) -> dict:
        units = self.units()
        for u in units:
            if u["unit"].endswith(".service"):
                s = self.show(u["unit"])
                u.update(restarts=s.get("NRestarts", ""), since=s.get("ActiveEnterTimestamp", ""),
                         result=s.get("Result", ""), mem_mb=_mb(s.get("MemoryCurrent")))
        return dict(ts=time.strftime("%Y-%m-%d %H:%M:%S"), system=self.system(), units=units,
                    failed=[u["unit"] for u in units if u["active"] == "failed" or u.get("result", "") not in ("", "success")],
                    timers=self.redact(self.timers()), files=self.files(), problems=self.problems())


def _mb(v: Optional[str]):
    try:
        n = int(v)
        return None if n >= 2 ** 63 else round(n / 1e6)  # "[not set]" / UINT64_MAX when accounting is off
    except (TypeError, ValueError):
        return None
