import json, sys, csv, math, bisect, datetime, statistics as st, collections
from zoneinfo import ZoneInfo
D = sys.argv[1]; ET = ZoneInfo("America/New_York"); UTC = datetime.timezone.utc
N = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))
def series(fn):
    d = json.load(open(f"{D}/{fn}"))["chart"]["result"][0]
    return [(t, c) for t, c in zip(d["timestamp"], d["indicators"]["quote"][0]["close"]) if c]
es = series("ES%3DF_1h.json"); est = [t for t, _ in es]
def es_at(ts):  # value at time ts = close of the hourly bar that ended at or before ts
    i = bisect.bisect_right(est, ts - 3600) - 1
    if i < 0 or ts - 3600 - est[i] > 3 * 3600: return None
    return es[i][1]
spx = {datetime.datetime.fromtimestamp(t, ET).date(): c for t, c in series("%5EGSPC_1d.json")}
vix1d = {}
for r in list(csv.reader(open(f"{D}/VIX1D.csv")))[1:]:
    m, d_, y = r[0].split("/"); vix1d[datetime.date(int(y), int(m), int(d_))] = float(r[4])
days = sorted(spx)
evs = json.load(open(f"{D}/events.json"))
CP = [("Vorabend 20 Uhr ET", -4.0), ("03 Uhr ET (09 Uhr DE)", 3.0), ("08 Uhr ET", 8.0), ("10 Uhr ET", 10.0), ("12 Uhr ET", 12.0), ("14 Uhr ET", 14.0), ("15 Uhr ET", 15.0)]
OVN = 0.25  # share of a day's variance overnight
rows = []; skip = collections.Counter()
for e in evs:
    day = datetime.date.fromisoformat(e["day"])
    if day not in spx: skip["kein Handelstag"] += 1; continue
    i = days.index(day)
    if i == 0: continue
    prev = days[i - 1]; K = spx[prev]
    up = json.loads(e["prices"])[0] == "1"
    if (spx[day] > K) != up: skip["Ergebnis passt nicht zu Yahoo"] += 1
    if prev not in vix1d: skip["kein VIX1D"] += 1; continue
    sig_d = vix1d[prev] / 100 / math.sqrt(252)
    close_prev = datetime.datetime.combine(prev, datetime.time(16, 0), ET).timestamp()
    es0 = es_at(close_prev)
    if not es0: skip["kein Future"] += 1; continue
    h = json.load(open(f"{D}/pm/{e['day']}.json")); ht = [a for a, _ in h]
    open_t = datetime.datetime.combine(day, datetime.time(9, 30), ET).timestamp()
    close_t = datetime.datetime.combine(day, datetime.time(16, 0), ET).timestamp()
    for lab, hr in CP:
        base = datetime.datetime.combine(day, datetime.time(0, 0), ET)
        t = (base + datetime.timedelta(hours=hr)).timestamp()
        j = bisect.bisect_right(ht, t) - 1
        if j < 0 or t - ht[j] > 180: skip["kein PM-Preis"] += 1; continue
        pm = h[j][1]
        e1 = es_at(t)
        if not e1: skip["kein Future"] += 1; continue
        S = K * e1 / es0
        if t < open_t:
            w = (1 - OVN) + OVN * (open_t - t) / (open_t - close_prev)
        else:
            w = (1 - OVN) * (close_t - t) / (close_t - open_t)
        s = sig_d * math.sqrt(max(w, 1e-6))
        p = N((math.log(S / K) - 0.5 * s * s) / s)
        rows.append(dict(day=e["day"], cp=lab, y=1.0 if up else 0.0, pm=pm, md=p, mv=S / K - 1, month=e["day"][:7]))
print("Märkte", len(evs), "Zeitpunkte", len(rows), dict(skip))
br = lambda v, k: st.mean((r[k] - r["y"]) ** 2 for r in v)
print("\n1) Genauigkeit (Brier, kleiner = besser) und Abweichung Polymarket − Modell:")
for lab, _ in CP:
    v = [r for r in rows if r["cp"] == lab]
    if not v: continue
    print(f"   {lab:22s} n={len(v):3d}  Polymarket {br(v,'pm'):.4f}  Modell {br(v,'md'):.4f}  Ø PM−Modell {st.mean(r['pm']-r['md'] for r in v)*100:+5.1f} Pkt  |PM−Modell| Ø {st.mean(abs(r['pm']-r['md']) for r in v)*100:4.1f}")
print(f"   Up-Tage: {sum(r['y'] for r in rows if r['cp']==CP[0][0])}/{sum(1 for r in rows if r['cp']==CP[0][0])}")
print("\n2) Kaufen, wenn Modell ≥ Schwelle vom Polymarket-Preis abweicht (Ø je Anteil nach 1 ct Kosten):")
for shift, slab in ((0.0, "Modell"), (0.03, "Modell +3 Pkt (Options-Schieflage)")):
    for thr in (0.03, 0.05, 0.10):
        out = []
        for r in rows:
            m = min(0.999, r["md"] + shift); d = m - r["pm"]
            if not (0.03 < r["pm"] < 0.97): continue
            if d >= thr: out.append((r, r["y"] - r["pm"] - 0.01))
            elif -d >= thr: out.append((r, (1 - r["y"]) - (1 - r["pm"]) - 0.01))
        if len(out) < 10: continue
        mu = st.mean(x for _, x in out); se = st.pstdev(x for _, x in out) / math.sqrt(len(out))
        bycp = collections.defaultdict(list); bym = collections.defaultdict(list)
        for r, x in out: bycp[r["cp"]].append(x); bym[r["month"]].append(x)
        print(f"   {slab:34s} ≥{thr:.2f}: n={len(out):4d} Ø {mu*100:+5.1f} ct (SE {se*100:.1f}) | " +
              " ".join(f"{k.split(' ')[0]}:{st.mean(v)*100:+.0f}({len(v)})" for k, v in bycp.items()))
        h1 = [x for r, x in out if r["day"] < "2026-06-01"]; h2 = [x for r, x in out if r["day"] >= "2026-06-01"]
        print(f"   {'':34s}        bis Mai {st.mean(h1)*100 if h1 else 0:+.1f} ct ({len(h1)}) | ab Juni {st.mean(h2)*100 if h2 else 0:+.1f} ct ({len(h2)})")

print("\n3) Strenger: nur EIN Zeitpunkt je Tag, 2 ct Kosten, je Seite, je Monat")
for lab in ("Vorabend 20 Uhr ET", "03 Uhr ET (09 Uhr DE)", "08 Uhr ET", "10 Uhr ET"):
    for thr in (0.03, 0.05):
        out = []
        for r in rows:
            if r["cp"] != lab or not (0.03 < r["pm"] < 0.97): continue
            d = r["md"] - r["pm"]
            if d >= thr: out.append((r, "Up kaufen", r["y"] - r["pm"] - 0.02))
            elif -d >= thr: out.append((r, "Down kaufen", (1 - r["y"]) - (1 - r["pm"]) - 0.02))
        if len(out) < 10: continue
        x = [o[2] for o in out]; mu = st.mean(x); se = st.pstdev(x) / math.sqrt(len(x))
        side = collections.defaultdict(list); mon = collections.defaultdict(list)
        for r, s_, v in out: side[s_].append(v); mon[r["month"]].append(v)
        print(f"   {lab:22s} ≥{thr:.2f}: {len(x):3d} Tage, Ø {mu*100:+5.1f} ct (SE {se*100:.1f}), Treffer {sum(v > 0 for v in x)/len(x)*100:.0f} % | "
              + " ".join(f"{k}: {st.mean(v)*100:+.1f} ({len(v)})" for k, v in side.items()))
        print("        Monate " + " ".join(f"{k[2:]}:{st.mean(v)*100:+.0f}({len(v)})" for k, v in sorted(mon.items())))
# simple no-model check: how often up when PM says p (calibration) before the open
print("\n4) Ohne Modell – Kalibrierung Polymarket vor der Eröffnung (03 + 08 Uhr ET):")
b = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
for r in rows:
    if r["cp"] not in ("03 Uhr ET (09 Uhr DE)", "08 Uhr ET"): continue
    k = min(int(r["pm"] * 10), 9); a = b[k]; a[0] += 1; a[1] += r["pm"]; a[2] += r["y"]; a[3] += r["md"]
print("   " + "  ".join(f"{k*10}-{k*10+10}%: n={a[0]} PM {a[1]/a[0]*100:.0f} Modell {a[3]/a[0]*100:.0f} tatsächlich {a[2]/a[0]*100:.0f}" for k, a in sorted(b.items()) if a[0] >= 8))
