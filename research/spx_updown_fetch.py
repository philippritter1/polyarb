import json, sys, os, time, datetime, urllib.request
from concurrent.futures import ThreadPoolExecutor
D = sys.argv[1]; UA = {"User-Agent": "Mozilla/5.0 polyarb-research"}
def get(u):
    for i in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=30) as f: return json.load(f)
        except Exception as e:
            if i == 3: return None
            time.sleep(1.5 * 2 ** i)
days = []
d = datetime.date(2025, 12, 1)
while d <= datetime.date(2026, 11, 19):
    if d.weekday() < 5: days.append(d)
    d += datetime.timedelta(days=1)
def ev(day):
    slug = f"spx-up-or-down-on-{day.strftime('%B').lower()}-{day.day}-{day.year}"
    r = get(f"https://gamma-api.polymarket.com/events?slug={slug}")
    if not r: return None
    e = r[0]; m = e["markets"][0]
    return dict(day=str(day), slug=slug, end=m.get("endDate"), start=m.get("startDate") or e.get("startDate"),
                outcomes=m.get("outcomes"), prices=m.get("outcomePrices"), tokens=m.get("clobTokenIds"),
                closed=m.get("closed"), volume=m.get("volumeNum"), uma=m.get("umaResolutionStatus"))
with ThreadPoolExecutor(6) as ex:
    evs = [x for x in ex.map(ev, days) if x]
json.dump(evs, open(f"{D}/events.json", "w"))
print("Märkte", len(evs), evs[0]["day"] if evs else "", "->", evs[-1]["day"] if evs else "")
print(evs[0])
def hist(e):
    fn = f"{D}/pm/{e['day']}.json"
    if os.path.exists(fn): return
    tok = json.loads(e["tokens"])[0]
    end = datetime.datetime.fromisoformat(e["end"].replace("Z", "+00:00")).timestamp()
    h = get(f"https://clob.polymarket.com/prices-history?market={tok}&startTs={int(end - 36 * 3600)}&endTs={int(end)}&fidelity=1")
    if h is not None: json.dump([(p["t"], p["p"]) for p in h.get("history", [])], open(fn, "w"))
with ThreadPoolExecutor(6) as ex:
    list(ex.map(hist, evs))
print("Verläufe", len(os.listdir(f"{D}/pm")))
