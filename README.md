# Polyarb – Polymarket Arbitrage Bot (MVP, Paper Trading)

Der Bot scannt Polymarket nach **Complete-Set-Arbitrage**, dimensioniert Trades über eine Risk-Engine für ein **2.500-$-Portfolio** und simuliert die Ausführung gegen die **echten Live-Orderbücher**. Die Simulation rechnet Latenz, Fees, Teilausführungen und Leg-Failures ein. Echtes Geld wird nicht bewegt.

## Schnellstart

```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt       # nur der Bot
pip install -r requirements-dev.txt   # zusätzlich pytest + websockets für die Tests

python run.py scan                 # 1 Live-Scan: Wie nah sind die Märkte an Arbitrage? Gibt es Chancen?
python run.py paper --hours 24     # Paper Trading gegen den echten Markt (läuft im Vordergrund)
python run.py dashboard            # dashboard.html aus data/polyarb.sqlite erzeugen
python run.py mock --hours 72 --db data/mock.sqlite   # Offline-Pipeline-Test mit synthetischem Markt
python -m pytest -q                # 54 Tests (Arb-Mathe, Sizing, Fills, Legging, Settlement, WebSocket-Feed, Phantom-Schutz, CSV-Export, Leg-Reparatur, Order-Verzögerung, Szenarien, Studie, Export); braucht requirements-dev.txt
```

Für Dauerbetrieb: `nohup python run.py paper > bot.log 2>&1 &`, oder als systemd-Service bzw. per tmux auf einem kleinen VPS. Das Dashboard kannst du jederzeit neu erzeugen, auch während der Bot läuft.

## Strategie

Ein vollständiges Set aller Outcomes zahlt immer genau **1 $**. Bei einem Multi-Outcome-Event mit n Kandidaten zahlen alle NO-Tokens zusammen immer genau **n−1 $**, denn nur einer der Kandidaten gewinnt.

| Strategie | Signal | Umsetzung | Kapitalbindung |
|---|---|---|---|
| `binary_buy_all` | Ask(YES) + Ask(NO) + Fees < 1 | Beide kaufen, sofort **mergen**, USDC zurück | keine |
| `binary_sell_all` | Bid(YES) + Bid(NO) − Fees > 1 | 1 $ **splitten**, beide Seiten verkaufen | keine |
| `negrisk_buy_all` | Σ Ask(YES aller Kandidaten) + Fees < 1 | Alle YES kaufen, bis zur Auflösung halten | bis zur Resolution |
| `negrisk_no_buy_all` | Σ Ask(NO aller Kandidaten) + Fees < n−1 | Alle NO kaufen, über den NegRiskAdapter sofort in n−1 USDC **konvertieren** | keine |

Der Scanner geht das Orderbuch **Level für Level** durch. Er nimmt eine Einheit nur dann mit, wenn ihre *marginale* Netto-Edge `min_edge_bps` erreicht. Die Größe entspricht damit der tatsächlich handelbaren Tiefe und nicht nur dem Top-of-Book.

**Fees (Stand 2026):** `fee = shares × rate × p × (1−p)`. Die Rate liegt je nach Kategorie bei 0 (Geopolitik) bis 0,07 (Krypto). Liefert die API ein `feeSchedule`, gilt dieses. Sonst nutzt der Bot die Kategorie-Tabelle in `config.yaml`, im Zweifel konservativ 0,05.

## Ausführungssimulation (Paper)

1. Die Chance wird im Snapshot erkannt. Gekreuzte Bücher (Bid ≥ Ask) werden ignoriert, weil sie nur eine veraltete lokale Kopie sein können.
2. Der Bot wartet `latency_ms` (Standard 350 ms) und **lädt das Buch neu**. Bei Märkten mit Order-Verzögerung (Live-Sport, Feld `secondsDelay` der Gamma-API) kommt diese Verzögerung für jede Order-Runde dazu (`respect_market_delay`). Im WebSocket-Modus kommt dieses Buch per REST direkt von Polymarket (`confirm_with_rest`). Eine Chance, die nur in der lokalen Kopie existiert, zählt damit als „verpasst“.
3. **Sequential Mode:** Das knappste Leg geht zuerst raus (IOC zum Limit). Die restlichen Legs werden auf dessen Fill skaliert. Verfehlt das erste Leg, kostet das nichts.
4. Vom sichtbaren Volumen wird nur `depth_haircut` (50 %) gefüllt. Das bildet ab, dass andere Bots schneller sind.
5. **Leg-Reparatur:** Bleibt ein Leg hinter den anderen zurück, versucht der Bot zuerst, die fehlenden Shares nachzukaufen. Das tut er nur, solange das Ergebnis besser ist als ein Notverkauf der übrigen Legs (`repair_legs`).
   Erst danach werden ungehedgte Reste sofort in den Bid verkauft. Was dort nicht absetzbar ist, bleibt als „Residual“ offen. Der Bot versucht es in jedem Zyklus erneut.
6. **Verbrauchte Liquidität:** Paper-Orders erreichen Polymarket nie, das Live-Buch zeigt die „gekauften“ Orders also weiter an. Der Bot zieht deshalb die gefüllten Mengen vom Buch ab, bis das Preislevel real verschwindet oder `consumed_ttl_s` (60 s) vorbei ist. So wird dieselbe Liquidität nicht mehrfach gezählt.

## Risk-Engine (`arb/risk.py`)

| Limit | Default | Zweck |
|---|---|---|
| `max_trade_pct` | 10 % | max. 250 $ Kapital pro Trade |
| `max_unhedged_usd` | 75 $ | **Legging-Cap**: Das größte einzelne Leg muss ins verbleibende Budget für ungehedgte Positionen passen. |
| `max_market_pct` | 20 % | Konzentrationslimit je Markt oder Event |
| `max_locked_pct` | 60 % | max. Kapital in negRisk-Körben bis zur Auflösung |
| `kelly_fraction` | 0,5 | Half-Kelly für Körbe (gewinnt die Edge, verliert mit 1 % Wahrscheinlichkeit alles) |
| `min_annualized_return` | 25 % p.a. | Körbe mit langer Bindung müssen sich lohnen |
| `cash_buffer_pct` | 10 % | immer liquide |
| `daily_loss_limit_pct` | 2 % | **Kill-Switch** für den Tag |
| `max_consecutive_leg_failures` | 5 | danach 30 min Pause |

## Szenarien: weitere Strategien parallel testen

Neben der Arbitrage laufen weitere Strategien als eigene **Paper-Szenarien**. Jedes hat ein eigenes Budget (wie die Arbitrage 2.500 $, damit die Renditen vergleichbar sind; ändert sich `capital_usd`, startet das Szenario neu und die alten Daten werden archiviert), eine eigene Datenbank (`data/scenario-<name>.sqlite`), einen eigenen Prozess (`polyarb-scenario@<name>`) und einen eigenen Tab im Dashboard, inklusive CSV-Export. Anders als die Arbitrage **können diese Trades verlieren**. Welche Szenarien laufen, steht in `config.yaml` unter `scenarios:`. `enabled: false` schaltet eines beim nächsten Update ab.

| Szenario | Idee | Risiko |
|---|---|---|
| `ladder` Logische Arbitrage | Märkte eines Events, die auseinander folgen („über 110k“ ⇒ „über 100k“, „bis Oktober“ ⇒ „bis Dezember“). Ist die engere Aussage teurer, YES auf die breite und NO auf die enge kaufen: zahlt immer ≥ 1 $, im Zwischenfall 2 $. Läuft mit der Arbitrage-Engine (Legs, Reparatur, REST-Fills) | Auflösungsregeln der beiden Märkte weichen ab; Kapital bis zur Auflösung gebunden |
| `endgame` Endspiel-Ernte | Favorit für 0,95–0,99 kaufen, kurz vor oder nach dem geplanten Ende; Sportspiele erst nach Abpfiff; sicherste Kandidaten zuerst, höchstens 2 neue Positionen pro Durchlauf und 100 $ pro Spiel | gewinnt oft wenig, ein Fehlgriff kostet den ganzen Einsatz |
| `longshot` Longshot-NO | NO auf Außenseiter mit YES-Preis 2–8 % in Multi-Outcome-Events (Favorite-Longshot-Bias) | wie oben, breit gestreut über viele kleine Positionen |
| `weather` Wetter-Modell | Temperatur-Buckets mit Ensemble-Prognosen (Open-Meteo: GFS, ECMWF, ICON) bewerten, kaufen, wenn das Modell 8–30 Prozentpunkte über dem Preis liegt. Nur Tage, die in der Stadt noch nicht begonnen haben | echte Prognosefehler; Station vs. Modellgitter |

Schutzregeln für alle Szenarien: höchstens `max_slippage` (3 Cent) über dem besten Ask kaufen; Wetter zusätzlich nicht, wenn Modell und Markt um mehr als `max_edge` auseinanderliegen oder der Markt den Token praktisch bei 0 sieht (dann weiß der Markt mehr). `reset: <neuer Wert>` startet ein Szenario neu.

Die Ausführung ist so realistisch wie bei der Arbitrage: Latenz, Order-Verzögerung, frisches Orderbuch, Haircut und Fees. Positionen werden bis zur Auflösung gehalten und dann ausgezahlt. Die Karte **„Hat die Strategie einen Edge?“** vergleicht die Gewinnquote der aufgelösten Positionen mit dem Ø Einstiegspreis. Nur wenn die Strategie öfter gewinnt, als der Preis sagt, bleibt nach vielen Trades etwas übrig. Aussagekräftig ist das erst ab etwa 30 Auflösungen.

Lokal: `python run.py scenario weather` startet ein Szenario, `python run.py dashboard --all` baut alle Tabs.

## Markt-Studie und Export

**Studie** (Tab „Studie“): `polyarb-study.timer` sammelt alle 6 h aufgelöste Märkte der letzten 120 Tage (ab 1.000 $ Volumen; Temperatur-Buckets aus Wetter-Events schon ab 50 $, weil sie einzeln wenig gehandelt werden) mit ihrem Preisverlauf und speichert den YES-Preis 7 Tage, 1 Tag, 6 h und 1 h vor Schluss plus das Ergebnis (`data/study.sqlite`, inkrementell). Der Tab zeigt daraus die Kalibrierung: Gewinnen Seiten, die zu 95 % gehandelt wurden, wirklich in 95 % der Fälle? Nach Preisbereich, Zeitpunkt und Kategorie, mit 95-%-Bereich. Grün/rot markiert ist nur, was statistisch klar ist. So lassen sich Ideen wie Endspiel-Ernte oder Longshot-NO an Tausenden vergangener Märkte prüfen, statt Wochen auf Paper-Ergebnisse zu warten. Von Hand: `python run.py study --days 120`.

**Export** (Tab „Export“): alle Daten zum Herunterladen, im Format für deutsches Excel. Pro Szenario Trades und Equity-Verlauf, dazu die Studie (alle Märkte, Kalibrierung) und eine ZIP-Datei mit allem. Wird mit dem Dashboard alle 10 Minuten neu erzeugt.

## Architektur

```
run.py            CLI (scan | paper | mock | dashboard)
config.yaml       alle Parameter
arb/client.py     Gamma-API (Märkte, Events) + CLOB-API (/books, batch) – read-only, ohne Keys
arb/scanner.py    Depth-Walk, Fees, Edge, Annualisierung, Ranking
arb/risk.py       Sizing & Limits, Kill-Switches
arb/paper.py      Paper-Broker: IOC-Fills, Merge/Split, Unwind, Settlement, Portfolio
arb/engine.py     Loop: Universe → Books → Scan → Size → Latenz → Fill → Log (Engine = Polling, StreamEngine = WebSocket)
arb/stream.py     WebSocket-Client und lokaler Orderbuch-Store
arb/scenarios.py  Szenario-Engine (Budget, Positionen, Auflösung) + Strategien endgame / longshot / weather
arb/ladder.py     Logik-Leitern in Events erkennen (Schwellen ↑/↓, Stichtage „by …“)
arb/study.py      Markt-Studie: aufgelöste Märkte + Preisverlauf sammeln, Kalibrierung berechnen
arb/weather.py    Temperatur-Märkte parsen, Bucket-Wahrscheinlichkeiten aus Ensemble-Prognosen
arb/storage.py    SQLite (scans, opportunities, executions, equity, settlements)
arb/mock.py       synthetischer Markt für Offline-Tests
arb/netcheck.py   Netzwerk-Diagnose beim Start
dashboard.py      self-contained HTML-Dashboard
deploy/           Server-Installation, Auto-Update, systemd-Units, Push-Nachrichten (notify.py)
```

## Realitätscheck

- **Binär-Arbs sind selten.** Polymarkets Matching-Engine gleicht gegenläufige Orders per Mint und Merge aus. Deshalb schließen sich YES+NO-Lücken meist schon im Buch. Laut Studie (arXiv 2608.00666) ist der Median-Gewinn pro Arb bis Anfang 2026 auf etwa 0,08 USDC gefallen. Die Fenster dauern Sekunden.
- **Fees fressen die Edge.** Bei p≈0,5 und einer Rate von 0,04 kostet ein Paar etwa 2 %. Fee-freie Kategorien (Geopolitik) sind deshalb überproportional interessant.
- **Auch mit WebSocket ist der Bot nicht der Schnellste.** Standard ist der Echtzeit-Feed; im Polling-Modus (`data_source: polling`) scannt der Bot nur alle 8 s. Die Paper-Phase zeigt ehrlich, was damit erreichbar ist. Das Histogramm „Wie nah ist der Markt“ ist dabei die wichtigste Diagnose.
- Die Mock-Ergebnisse sind **synthetisch** und haben keine Aussagekraft über reale Profitabilität.

## Go/No-Go-Kriterien vor Echtgeld (Vorschlag)

Nach mindestens **2 Wochen** Paper-Betrieb:
1. Netto-PnL nach Fees positiv, und zwar in mindestens 2 von 3 Wochen.
2. Capture (realisiert / erwartet) über 40 %.
3. Max. Drawdown unter 3 %, kein Kill-Switch durch Tagesverlust.
4. Leg-Failure-Quote unter 20 %.
5. Mindestens 30 ausgeführte Trades, damit die Stichprobe aussagekräftig ist.

## Roadmap zu Live

1. ~~WebSocket-Feed statt Polling~~ – umgesetzt, siehe unten.
2. **LiveBroker** mit `py-clob-client`: FOK/FAK-Orders, Wallet und API-Keys, Merge und Split über den Relayer.
3. Start mit 10 % des Kapitals und identischen Limits. Den Paper-Broker parallel als Schattenbuch mitlaufen lassen.
4. Erweiterungen: logische Cross-Market-Arbs und Market-Making im fee-freien Bereich mit Maker-Rebates.

> Hinweis: keine Anlageberatung. Polymarket sperrt u. a. DE, FR, UK, IT, NL und die USA. Österreich stand im September 2026 nicht auf der Liste. Prüfe die aktuellen Nutzungsbedingungen und die steuerliche Behandlung selbst.

## Deployment auf einem Server (Hetzner Cloud, Helsinki)

Der Server holt den Code von GitHub und **aktualisiert sich alle 10 Minuten selbst**: Neue Commits auf `main` werden automatisch installiert, danach startet der Bot neu, und du bekommst eine Push-Nachricht.

1. `python deploy/make_cloud_config.py https://github.com/<user>/polyarb.git` erzeugt `deploy/cloud-config.yaml` (ca. 500 Bytes). Die Zugangsdaten stehen in `deploy/SECRETS.txt`, das nicht im Repo liegt.
2. Beim Anlegen des Servers (Ubuntu 24.04, Standort Helsinki) die Datei unter „Cloud config“ einfügen.
3. Nach ca. 10 Minuten kommt die Startmeldung per ntfy, mit dem Link zum Dashboard.

Was dann läuft (siehe `deploy/systemd/`):

- `polyarb`: der Bot im WebSocket-Modus, startet bei Absturz automatisch neu
- `polyarb-update.timer`: alle 10 Minuten `git pull`, bei Änderungen `install.sh` und Neustart
- `polyarb-scenario@<name>`: je ein Prozess pro aktiviertem Szenario aus `config.yaml`
- `polyarb-dash.timer`: aktualisiert das Dashboard (alle Tabs) alle 10 Minuten, inklusive `trades.csv` mit allen Ausführungen und späteren Auszahlungen (Button „CSV-Export aller Trades“, Format für deutsches Excel)
- `polyarb-watch.timer`: alle 5 Minuten Watchdog und Trade-Alerts (siehe unten)
- `polyarb-report.timer`: Statusbericht über die letzten 4 h um 00, 04, 12, 16 und 20 Uhr
- `polyarb-daily.timer`: Tagesbericht über die letzten 24 h um 08 Uhr

Die Server-Zeitzone ist Europe/Vienna (setzt `install.sh`).

**Paper-Phase neu starten:** Einen neuen Text in `deploy/RESET_ID` committen. Beim nächsten Auto-Update verschiebt `install.sh` Datenbank und Portfolio einmalig nach `data/archive-<Zeitstempel>/`, und der Bot startet wieder mit dem Startkapital. Gelöscht wird nichts.

### Push-Nachrichten (ntfy)

`deploy/notify.py` schickt alles an das ntfy-Topic `NTFY_TOPIC` aus `/etc/polyarb.env`:

| Nachricht | Wann |
|---|---|
| Startmeldung, Update installiert/fehlgeschlagen | nach Erstinstallation bzw. Auto-Update |
| **Problem** / **wieder OK** | Bot-Prozess läuft nicht, oder seit über 15 min keine neuen Daten |
| **Handel pausiert** | Kill-Switch ausgelöst |
| **Trade(s)** | jede Paper-Ausführung seit dem letzten Watchdog-Lauf, mit den letzten 5 im Detail |
| **Update (4h)** | Equity, PnL, Chancen, Trades, Capture der letzten 4 h |
| **Tagesbericht** | dasselbe für die letzten 24 h |

Optionale Schalter in `/etc/polyarb.env`: `NOTIFY_TRADES=0` schaltet die Trade-Alerts ab, `NOTIFY_MISSED=0` meldet nur Runden mit mindestens einem gefüllten Trade. Beim ersten Watchdog-Lauf werden alte Trades nicht nachgemeldet.

Von Hand auf dem Server schickt das sofort einen Bericht über die letzten 12 h:

```bash
sudo -u polyarb bash -c 'set -a; source /etc/polyarb.env; cd /opt/polyarb && .venv/bin/python deploy/notify.py report 12'
```

Auf dem Server: `journalctl -u polyarb -f` zeigt das Live-Log, `cat /opt/polyarb/data/update.log` das Update-Log.

## Datenquelle: WebSocket statt Polling

`data_source: websocket` (Standard) abonniert die Orderbücher aller Märkte in Echtzeit über `wss://ws-subscriptions-clob.polymarket.com/ws/market`. Jede Änderung prüft nur die betroffenen Körbe, und zwar innerhalb von Millisekunden statt nach einem Scan von über 10 Sekunden. Die Paper-Ausführung wartet `latency_ms` und füllt dann gegen das Live-Buch zu diesem Zeitpunkt. Jede Ausführung protokolliert `sum_detect` und `sum_exec`, damit sichtbar wird, warum ein Trade verpasst wurde.
