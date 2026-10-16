# polyarb – Kontext für neue Sessions

Paper-Trading- und Research-Bot für Polymarket (plus Kalshi-Studie). Ziel: einen echten Edge finden und
validieren, bevor echtes Geld (später 1–2k €) eingesetzt wird. Der Nutzer schreibt Deutsch – auf Deutsch antworten.

## Arbeitsweise
- Direkt auf `main` committen und pushen. Der Hetzner-Server holt sich alle 10 Minuten den Stand
  (`deploy/update.sh` → `deploy/install.sh`), Dienste laufen als systemd-Timer (`deploy/systemd`).
- Vor jedem Push `python -m pytest -q` (alle Tests müssen grün sein).
- **Regeln der laufenden Szenarien nicht ändern**, solange der Live-Test läuft (seit Anfang Oktober 2026, 2–3 Wochen).
  Verbesserungen als *neues* Szenario daneben bauen, nicht das bestehende umschreiben.
- Geheimnisse liegen in `/etc/polyarb.env` auf dem Server, nie im Repo oder Chat. Den Odds-API-Key trägt der Nutzer
  über die Einstellungsseite `$POLYARB_URL/admin/` ein (`deploy/admin.py`, gleiches Passwort wie das Dashboard,
  speichert nach `data/secrets.env`, Modus 600, wirkt ohne Neustart). Die Seite kann nur das – keine Befehle.
- Server-Diagnose ohne SSH: `$POLYARB_URL/admin/diag` bzw. `/admin/diag.json` (`deploy/diag.py`, nur lesend):
  Dienst-Status, Neustarts, RAM/Platte, größte Dateien, Warnungen 24 h; Log je Unit unter
  `/admin/diag/log?unit=polyarb-scenario@mm_rewards.service&n=300` (`&warn=1` nur Warnungen). Geheimnisse geschwärzt.

## Daten abholen
Dashboard: `$POLYARB_URL` (https://2-29-62-225.sslip.io), Basic-Auth wird vom Agent-Proxy eingefügt
(API-Credential der Umgebung) – einfach ohne Zugangsdaten anfragen.
- `$POLYARB_URL/export/` – Übersicht aller Exporte (CSV im deutschen Excel-Format: `;` und Dezimalkomma)
- `export/polyarb-kompakt.zip` – Szenarien (trades/equity/scan/kapazitaet), Kalibrierung, Stresstest, Diagnosen
- `export/studie.zip`, `export/wetter-messwerte.zip`, `export/kalshi.zip` – Rohdaten je Studie;
  zu große werden als `<name>-teil1.zip`, `-teil2.zip`, … geliefert (Zeilen-Chunks mit Kopfzeile)
- `export/polyarb-export.zip` – alles (> 30 MB)

## Aufbau
- `arb/scenarios.py` – Szenario-Engine (PriceBandStrategy, Budget 2.500 $ je Szenario, `max_day_usd`,
  Kapazitätsmessung, Scan-Statistik), Konfiguration in `config.yaml` unter `scenarios`
- `arb/study.py` – Polymarket-Studie aufgelöster Märkte (Kategorien, sport_kind, market_type)
- `arb/backtest.py` – Regeln/Stresstest auf den Studiendaten
- `arb/wxobs.py` – Wetter-Messwert-Studie (METAR-Stationen) und `LiveObs` für den Stations-Filter
- `arb/mm.py` – Market-Making-Simulation mit Rewards (Strategie `mm`)
- `arb/orders.py` – Order-Modul: aus / Trockenlauf / echt. Schalter `data/live.json` über `/admin/`, harte Grenzen je
  Szenario (`live:` in config.yaml), Protokoll `live_orders`, Fills `live_fills`, gezahlte Rewards `live_rewards`,
  Status `data/live-status-*.json`. Echt NUR wenn: Admin-Schalter „Echt“ + `live: allow_real: true` (Commit) + Wallet
  in `data/secrets.env` + Geoblock frei + Tagesverlust-Limit (`max_daily_loss_usd`) nicht erreicht; sonst Trockenlauf.
- `arb/exchange.py` – Polymarket-Anbindung (py-clob-client-v2 aus `requirements-live.txt`, optional installiert; seit
  28.04.2026 CLOB V2 mit pUSD als Sicherheit, der alte py-clob-client geht nicht mehr): Post-only-
  Limitorders, Storno, offene Orders, Fills, Guthaben, Reward-Scoring, gezahlte Rewards, Geoblock, Kontowert.
  Wallet (POLY_PRIVATE_KEY/POLY_FUNDER/POLY_SIG_TYPE) trägt der Nutzer auf `/admin/` ein, nie im Chat/Repo.
  Verbindungstest (Knopf auf `/admin/`, `connection_test`): eine Mindestgrößen-Kauforder 3 Ticks unter dem besten Gebot
  eines großen Markts setzen, offen prüfen, Scoring prüfen, immer stornieren; Ergebnis in `data/live-test.json`.
  Wallet seit 14.10. eingetragen (Signer 0xaAc8…bE83, Funder 0x441A…4438 = Smart-Contract-Wallet, **Signaturtyp 3**).
  Reward-Test (zweiter Knopf, `reward_test`): ein Gebot in einem Reward-Markt in Reward-Mindestgröße innerhalb des
  Reward-Abstands (nie über dem besten Gebot), bis 80 s Scoring abfragen, stornieren; Ausführung möglich und wird gemeldet.
  Reward-Test am 14.10. 14:17 bestanden: 20 Anteile @ 0,08 (Mitte 0,115, Reward-Abstand 6,5 ct) zählte nach 40 s für
  Rewards (Polymarkets eigene Prüfung) – unsere Gebotslogik qualifiziert also wirklich.
  Verbindungstest am 14.10. 13:30 bestanden (11 pUSD, echte Order gesetzt/gefunden/storniert). Pilot startet erst
  nach der Entscheidung am 31.10. (dann `allow_real: true` für mm_pilot + Admin-Schalter „Echt“).
- `arb/sharp.py` – Pinnacle-Quoten (The Odds API) vs. Polymarket-Fußball (Strategie `sharp`)
- `arb/kalshi.py` – Kalshi-Studie; `arb/ladder.py` + `arb/scanner.py` – Arbitrage
- `dashboard.py` – statisches Dashboard + Exporte; `deploy/notify.py` – 4-h-Bericht per Push
- `arb/cockpit.py` – Reiter „Cockpit“ (`/cockpit/`, ab 15.10.): Echtgeld-Kacheln der Generalprobe (Kontowert, echte
  Rewards heute/gestern, Polymarket-Anteil vs. Schätzung) und alle aktiven MM-Szenarien seit 11.10.: Rewards vs. Handel
  (Balken), Rendite-Verlauf (Kleinbilder), Kennzahlen (netto/Tag, Handel je 1 $ Rewards, Bestand, Kurs nach Fill).

## Stand der Erkenntnisse
Widerlegt / kein Edge: echte Arbitrage (praktisch nie erreichbar), Endgame, Longshot, Krypto, E-Sports- und
Sieg-Markt-Außenseiter, Finanz-Außenseiter, Wettermodell, Wetter-Messwerte nachträglich (Markt preist in Minuten ein),
Kalshi (effizient, am echten Ask ≈ 0 oder negativ), Inverse verlierender Regeln.

Fallen in den Studiendaten (Walk-forward-Analyse 03.10.) – bei jedem Backtest beachten:
- `volume` ist das **Endvolumen** (nach Schluss). Überraschungen ziehen spät Handel an, ein Filter darauf schaut in
  die Zukunft. Wetter-NO 55–97 % ohne den 5k-Filter: Studie +0,2 % statt +8 % (passt zu den Live-Verlusten).
  Kalshi Sport-Außenseiter: Endvolumen klein −45 %, groß +35 %. Stattdessen Kalshi `vol_*` (24 h vor dem Zeitpunkt).
- Aufnahme nach Endvolumen verzerrt ebenso (Polymarket ≥ 1.000 $, Kalshi ≥ 100 Kontrakte). Unverzerrt ist nur die
  Zufalls-Stichprobe `sample < 20` (Spalte „Stichprobe“, ab Zeilen-Version 2).
- Bis Zeilen-Version 2 zählten Zeitpunkte vom tatsächlichen Schluss: „Erreicht X $Y?“ schließt beim Treffer
  (Finanz JA 65–80 % gewann 97 %). Jetzt vom geplanten Ende, Zeitpunkte nach dem Schluss bleiben leer.
- Preise ohne Handel: Startwert 0,50 und veraltete 7-Tage-Preise (Fußball O/U: JA und NEIN beide „profitabel“).
  Spalten `a_*` = Stunden seit der letzten Preisänderung.
- Kalshi letzter Preis + 2 ct ist im Schnitt ~8 Punkte zu optimistisch gegenüber dem echten Ask.

Saubere Stichprobe (Auswertung 06.10., PM Aug–Okt n≈82k, Kalshi 14.9.–6.10. n≈71k):
- Polymarket: fast alles negativ nach Kosten (alle Käufe −5,5 %). fussball_dog −5 % (reiner Fußball −10 %),
  Endgame/Longshot-NO −2,5 %, mlb_spread zu wenig Daten. 7-Tage-Preise weiterhin unbrauchbar (beide Seiten „profitabel“).
- Einzige Auffälligkeit: Wetter-NO 50–65 % (NO auf das wahrscheinlichste Bucket), aber stark phasenabhängig:
  Apr–Jul −4 bis −8 %, Aug–Sep +10 bis +22 % (fast nur °C-Märkte), Okt live 42–57 % Treffer bei ~60 % Preis.
- Kalshi am echten Ask: nichts besteht die Walk-forward-Hürde; alles −18 %, nur enge Bücher (≤ 4 ct) −4 %.
- Live-Wetter kauft praktisch immer ~11,5 h vor Ende (beim Eintritt ins 12-h-Fenster, 2 Uhr MESZ), die Studie
  misst bei 6 h – live testet also einen anderen Zeitpunkt als die Studie.

Beendet (10.10., Nutzer-Entscheidung): `fussball_dog`, `wetter_no_breit`, `wetter_no_mess`, `wetter_no_streng`
mit `wind_down: true` ausgelaufen, seit 12.10. `enabled: false` + `archive: true` (Dienst gestoppt, Seite und
Export bleiben, Reiter „Archiv“). Endstand: fussball_dog −37,7 %, breit −19,6 %, mess −13,1 %, streng −14,8 %. Kontrollgruppen `weather_no`, `mlb_spread`, `ladder`
laufen weiter, ebenso `wetter_no_maker` (live ≈ 0, Treffer ≈ Preis).

Beendet (14.10., Nutzer-Entscheidung): `weather_no`, `mlb_spread` (sofort archiviert, keine offenen Positionen);
`ladder`, `wetter_no_maker`, `sharp_fussball` mit `wind_down: true` (laufen aus, danach `enabled: false` + `archive: true`).
Außerdem `services:` in config.yaml: `arbitrage_bot: false` (polyarb.service gestoppt, Startseite = „Übersicht“,
Bot im Archiv) und `wxobs_study: false` (Wetter-Messwert-Studie gestoppt, Reiter ausgeblendet, Export bleibt).
Aktiv bleibt nur Market Making (mm_rewards, mm_breit, mm_neu, mm_neu2, mm_abbau, mm_sofort, mm_treppe, mm_ruhig, mm_nah, mm_ueberbucht, mm_schutz, mm_schutz_neu, mm_pilot, mm_probe) plus Studie/Kalshi-Studie.

Aktive Kandidaten im Live-Test:
- `wetter_no_breit` / `wetter_no_mess` / `wetter_no_streng`: NO auf Temperatur-Buckets, 55–97 %, 2–12 h vor Schluss,
  max. 1.000 $/Tag. Studie +8,2 % (OOS +5,8 %); Stations-Filter streng +10,7 % (n≈5.200) – beides mit dem
  Endvolumen-Filter, ohne ihn ≈ 0 % (siehe oben). Live bisher Treffer ≈ Preis oder darunter.
  Kapazität: Orderbücher deutlich tiefer als das Tageslimit.
- `wetter_no_maker` (ab 03.10.): Auswahl wie `wetter_no_mess`, aber Limit-Kauf 1 Tick über dem Bid, ohne Gebühr
  (`maker: true` in der Szenario-Engine). Streng simuliert: gefüllt erst, wenn der Ask unter das Limit fällt.
  Frage: Reicht der gesparte Spread (+ ~1 % Gebühr), oder füllen nur die Limits, die verlieren?
- `mm_rewards` (ab 06.10., `arb/mm.py`): Market Making in ruhigen Märkten mit Liquidity Rewards (ohne Sport/Krypto),
  Gebote auf JA und NEIN um die Mitte, JA+NEIN-Paare werden zu 1 $ zusammengelegt. Ertrag getrennt ausgewiesen:
  Spread/Auflösung, Rewards (geschätzt nach Polymarkets Formel, Anteil am Buch), Rebates. Ausführung über
  WebSocket-Trades: nur Trades UNTER unserem Gebot zählen. Frage: Decken Spread + Rewards die Verluste an Informierte?
- **Bugfix 10.10. (mm_rewards/mm_neu):** bis dahin wurden auch abgewählte Märkte mit Restbestand weiter bespielt,
  `max_markets` 15 galt also nicht (mm_neu zuletzt ~100 Märkte, Bestand 3.200 $ > Startkapital, finanziert aus
  geschätzten Rewards). Zahlen bis 10.10. sind dadurch aufgebläht – für die Bewertung ab 10.10. getrennt auswerten.
- **Bugfix 10.10. (sharp_fussball):** Sieg-Märkte wurden über ein gemeinsames Wort mit fremden Vereinen verknüpft
  (Yokohama FC → Yokohama F. Marinos, Athletic Club → Charlton Athletic); 3 von 5 Käufen bis dahin falsch.
  Jetzt muss auch der Gegner (Event-Titel „A vs. B“) passen, ohne Titel nur exakter Name.
- Trend des Reward-Geschäfts (ab 08.10., nur gemessen): Tabelle `mm_metrics` stündlich (Pool, unser Anteil, Rewards/Tag),
  Dashboard-Karte „Kennzahlen je Tag“ + `kennzahlen.csv`, Zeile „Trend 24 h“ im 4-h-Bericht. Pool sinkt = Kürzung,
  Anteil sinkt bei gleichem Pool = Konkurrenz.
- `mm_neu` (ab 07.10.): wie `mm_rewards`, aber nur Märkte < 3 Tage alt und Auswahl nach erwartetem Reward
  (Pool × unser Anteil gegen die Gebote im Buch, `rank: share`). Recherche: frühe Märkte zahlen am meisten Rewards.
- `mm_neu2` (ab 12.10.): wie `mm_neu`, frisch mit 2.500 $ und Bestandsgrenze 1.500 $ gesamt. `mm_neu` selbst
  sitzt seit dem Fehler auf ~3.800 $ Altbestand fest (kaum Cash für Gebote) und läuft daneben weiter, damit man
  sieht, wie der Altbestand aus neuen Märkten auflöst. Vergleich bis 31.10.: mm_rewards / mm_breit / mm_neu2.
- `mm_abbau` (ab 14.10.): wie `mm_neu2`, plus aktiver Bestandsabbau (`exit:` in arb/mm.py): einseitige Position
  älter als 24 h bekommt ein Verkaufsgebot zur Mitte + Abstand (gefüllt nur durch Trades darüber), nach 96 h Verkauf
  zum Bid (Taker-Gebühr). Frage: Verluste früh begrenzen und Kapital fürs Bieten freihalten vs. auf Auflösung warten.
- `mm_sofort` (ab 14.10.): wie `mm_abbau`, aber Verkaufsgebot für einseitigen Bestand **sofort** (`exit: after_h: 0`),
  und Verkaufsgebote zählen in der Reward-Schätzung mit (`reward_asks: true`, Anteil getrennt als
  „davon Rewards aus Verkaufsgeboten“). Anlass: in der Generalprobe zählte das echte Verkaufsgebot für Rewards.
  Frage: schneller wieder Cash + Spread + Rewards auf dem Bestand vs. 24 h warten (mm_abbau). Bei mm_abbau und den
  anderen zählen Verkaufsgebote NICHT für die Schätzung (unterschätzt sie leicht) – Regeln nicht geändert.
- `mm_treppe` (ab 15.10., Nutzer-Idee): wie `mm_sofort`, aber das Verkaufsgebot rückt stündlich näher (`exit: decay_h: 6`):
  von Mitte + Abstand in 6 Stufen bis zur Mitte – immer im Reward-Bereich (Mitte .. Mitte + max_spread, Nutzer-Vorgabe),
  nie unter der Mitte, nie am Bid. Frage: Bestand loswerden, bevor Verluste wachsen. Seit 15.10. auch in mm_probe (echt).
- `mm_ruhig` (ab 15.10.): wie `mm_treppe`, plus `pause:` – springt die Mitte binnen 5 Min. um ≥ 3 Ticks, 10 Min. keine
  Gebote in dem Markt. Frage: weniger schlechte Fills vs. weniger Rewards. Dazu für alle MM-Szenarien gemessen
  (`mm_markout`, Scan „Kursbewegung nach Fill 5/60 Min $“): Mitte 5 und 60 Min. nach jedem Fill minus Fill-Preis ×
  Menge – negativ = Verlust an Informierte, das müssen die Rewards decken. Auswertung ab 11.10. (Export): Gewinn
  kam fast nur aus Rewards; JA+NEIN-Paare selten (jede 5.–10. Ausführung) und ≈ 0 $; große Verluste aus Auflösungen.
- `mm_nah` (ab 15.10., Nutzer-Frage): wie `mm_sofort`, aber `quote_frac: 0.25` – Gebote und Verkaufsgebot bei 1/4 statt
  1/2 des Reward-Abstands (Reward-Punkte 56 % statt 25 %, dafür mehr Fills). Frage: nah vs. weiter weg (mm_sofort).
- `mm_ueberbucht` (ab 15.10., Nutzer-Idee): wie `mm_sofort`, aber Gebote zusammen bis 2x das Cash (`overcommit: 2`;
  Limit-Orders binden kein Geld, Rewards zählen die Größe im Buch) und Verkaufsgebot näher an der Mitte
  (`exit: ask_frac: 0.25` statt quote_frac 0,5). Fehlt beim Fill das Cash, wird nur der bezahlbare Teil gefüllt, der
  Rest des Gebots fällt weg (Scan „Fills ohne Cash (überbucht)“). Echt NICHT freigegeben – ob Polymarket offene Orders
  gegen das Guthaben rechnet, ist ungeklärt. Frage: mehr Rewards pro $ vs. Risiko gleichzeitiger Fills.
  Überbuchungs-Test (dritter Knopf auf /admin/, `overbook_test`): setzt EIN Gebot bei der Hälfte des besten Gebots
  eines großen Markts, das allein ins Guthaben passt, zusammen mit den offenen Geboten des Bots aber darüber liegt;
  angenommen = Überbuchen geht, „not enough balance“ = geht nicht; immer storniert. polymarket.com zeigte 15.10.
  „available to trade“ = volles Cash trotz ~56 $ offener Gebote (Hinweis, kein Beweis). **Test 15.10. 08:12: Überbuchen GEHT** –
  Testgebot 24,84 $ + offene 75,20 $ = 100,04 $ bei 82,97 $ Guthaben wurde angenommen (danach storniert).
- `mm_schutz` (ab 16.10., nach MM-Recherche): wie `mm_ruhig`, dazu Finanz/Sport-Saison/Kursmärkte raus (`skip_re`),
  `quote_frac: 0.7`, `fair: micro` (Gebot vom ungünstigeren Wert aus Mitte und Microprice), `pair_frac: 0.25` (bei Bestand
  Gegenseite näher, Paar nie über 1 $) und **Selbstlernen** `learn:` (Tabelle `mm_ledger` je Markt: geschätzte Rewards +
  Kursbewegung 60 Min. nach Fill; < 0 nach 2 Fills oder 3 Sprünge/24 h → 3 Tage gesperrt, Wiederholung doppelt; Scan
  „gelernt: jetzt gesperrt“). learn/micro/pair_frac seit 16.10. auch in mm_probe (echt). Frage: Rewards − Markout > 0?
- **Datenauswertung 16.10.** (alle MM seit 11.10.): Rewards je $ im Buch/Tag in neuen Märkten (< 3 Tage, mm_sofort &
  Co.) ~15–22 %, breite Auswahl (mm_breit, mm_pilot, mm_probe) ~2 % – mm_pilot verliert auch in der Sim. Kursbewegung 60 Min.
  nach Fill ≈ 2x 5 Min. (Kurs läuft weiter gegen uns). Offener Bestand 10–25 % unter Einstand; Handel gesamt (realisiert +
  offen) = 30 % (breit/neu2/sofort) bis 77 % (mm_nah) der Rewards, mm_probe echt ~370 %. Echte Rewards > Schätzung.
- `mm_schutz_neu` (ab 16.10.): wie `mm_schutz`, aber nur neue Märkte (`max_age_days: 3`, `min_liquidity: 0`) und
  `fill_cooldown_s: 3600` (nach Fill 60 Min. kein Gebot auf derselben Seite). Beides seit 16.10. auch in mm_probe (echt).
- `mm_pilot` (ab 14.10.): Pilot für echtes Geld (Phase 0 fertig, `allow_real: false`): mm_breit-Logik in Pilotgröße
  (300 $, 10 Märkte, 15 $ je Gebot, Bestandsabbau), Simulation = echte Ordergröße. Freigabe erst nach Pilot-Entscheidung.
- `mm_probe` (ab 14.10.): **Generalprobe mit echtem Geld** (Nutzer-Entscheidung), einziges Szenario mit
  `allow_real: true` (anfangs 1 Markt, ~5 $, max. 11 $; seit 15.10. siehe unten). Echt nur, wenn auf /admin/ „Echt“ gewählt ist. Nach der Probe allow_real aus und ins Archiv.
  **Gestartet 14.10. ~16:38 (Nutzer hat „Echt“ gewählt):** 1 Order ~5 $, zählt für Rewards (1/1). Beobachtung: sehr
  häufiges Umsetzen (12 gesetzt / 11 storniert in wenigen Minuten, Mitte flackert um 1 Tick) – für den Pilot Toleranz
  (erst ab 2 Ticks umsetzen) als neues Szenario/Option prüfen. „Could not create api key“ im Log ist harmlos.
  14.10. 17:39 erster echter Fill (~6,4 $ Kauf), `apply_real_fills` hat ihn übernommen (Bestand 6,8 $, Cash 4,6 $).
  Danach wiederholte das Gateway eine wegen Guthaben abgelehnte Order alle ~2 s an der Börse (nur einmal
  protokolliert) – behoben (d7891cc): Wiederholung erst nach 60 s, echte Gebote nur bis zum echten Cash. 18:39 echtes
  Verkaufsgebot (Abbau Stufe 1) gesetzt, zählte für Rewards; Neustart dazwischen hat Bestand/Zeitplan behalten.
  **Fehler gefunden 19:39:** „zum Bid verkaufen“ wurde im Echtbetrieb nur intern gebucht (keine Order) – Bot glaubte
  ~11 $ Cash, echt 4,6 $ + Position. Behoben: echt wird ein Fill-and-Kill-Verkauf zum Bid geschickt (`sell_now`,
  höchstens 1/min je Token), gebucht erst beim gemeldeten Fill; Konto-Abgleich alle 5 Min. (`reconcile_positions`,
  data-api /positions + Cash): Abweichung, die beim nächsten Lesen noch besteht, wird ans Konto angepasst + Push-Alarm
  („Bestand an Konto angepasst“). Im Echtbetrieb kein simuliertes Zusammenlegen von JA+NEIN (on-chain Merge fehlt noch).
  20:32 Abgleich korrigierte nur das Cash (10,80 → 4,60): der Markt mit den 20 echten Anteilen war schon aus der
  Liste gefallen. Behoben: Positionen in Märkten, die der Bot nicht mehr führt, werden wieder aufgenommen (Daten aus
  /positions: conditionId, oppositeAsset, outcomeIndex). mm_probe jetzt `after_h: 0` (Nutzer: bei Bestand sofort
  Verkaufsgebot), Verkauf zum Bid weiter nach 2 h. Für den Pilot `after_h: 0` erwägen (mm_sofort misst es). Reward-Abgleich (`live_reward_share`, ab
  Commit acea21e, alle 10 Min.): erste Werte Polymarket-Anteil 47,6 % vs. geschätzt 14,3 %, „heute verdient“ aber
  nur 0,006 $ – Bedeutung von get_reward_percentages noch unklar, mehr Abfragen abwarten. Abends: API-Tag 14.10. = 0,0386 $ =
  polymarket.com „Oct 15 Earnings“ 0,04 $ (Website beschriftet mit dem Auszahlungstag, API mit dem Verdiensttag;
  der Verdienst wird verzögert verbucht). Schätzung desselben Tags ~0,15 $. Verkaufsgebot CT-02: Polymarket-Anteil 3,9 %.
  CT-02 nach 2 h per Fill-and-Kill zum Bid verkauft (20 @ 0,30, −0,56 $ inkl. Gebühr) – ganze Schleife echt geprüft.
  Nacht 14./15.: 5 Fills, Konto 12,36 $ (inkl. Position), echte Rewards 14.10. = 0,19 $ (unter 1-$-Auszahlungsgrenze);
  echt seit Start ~0,28 $ vs. geschätzt ~0,47 $ (→ für den Pilot ~60 % der Schätzung ansetzen).
  **15.10. Nutzer stockt auf ~60 $ auf:** mm_probe 60 $ Kapital, 3 Märkte, Gebote ~8 $, Bestand max. 30 $, echte Orders
  max. 60 $, Not-Aus ab 8 $ Tagesverlust, Notverkauf erst nach 24 h, `reprice_ticks: 2` (neue Option: Gebot erst
  umsetzen, wenn das Ziel ≥ 2 Ticks entfernt ist, solange es noch für Rewards zählt). Guard-Test entsprechend.
  Nutzer hat 76 $ eingezahlt (Konto ~88 $): 4 Märkte, Orders max. 85 $, Bestand max. 40 $, Not-Aus 10 $. Achtung:
  `capital_usd` NICHT ändern – jede Änderung startet das Szenario neu (Daten ins Archiv); live zählt ohnehin das echte Cash.
  15.10. 08:15: 9 Orders / 75 $, alle 9 zählen; Polymarket-Anteil meist ÜBER unserer Schätzung (z. B. 26,9 % vs. 12,7 %,
  6,5 % vs. 5,2 %) – hochgerechnet ~3 $/Tag (Polymarket) vs. ~1,9 $/Tag (Schätzung). reprice_ticks wirkt: 13 gesetzt /
  4 storniert in ~15 Min. (vorher ~1 Umsetzung pro Minute).
  15.10. 13:00: Tag bisher ≈ −2,30 $ Handelsergebnis (v. a. ein Fill: 20 NEIN „Wahlbeteiligung 70 %“ zu ~0,55 gekauft,
  zu 0,46 verkauft, −1,80 $) gegen ≈ +0,70 $ echte Rewards. Nutzer: mm_probe `min_days_to_end: 14` (statt 3), um
  nachrichtenträchtige Märkte mit nahem Ereignis zu meiden.
  15.10. 20:10: Konto 85,30 $ (Tagesstart inkl. Einzahlung ~86,44), echte Rewards heute 0,63 $; Polymarket-Anteil
  jetzt nur 41 % der Schätzung (0,53 vs. 1,29 $/Tag) – Verhältnis schwankt stark (morgens 190 %). Sim-Markout 60 Min.
  nach Fill in allen Szenarien negativ (ca. −1 bis −2 $ je Fill bei 50-$-Geboten; mm_nah −26 $ auf 12 Fills).
  Korrekturen 15.10. abends: „Rewards heute“ war nur die Summe der gerade bespielten Märkte (0,63 $) – richtig 1,90 $
  (Gateway `earned_today` = alle Märkte des Tages). Tagesverlust-Limit verglich mit dem Tagesstart VOR der Einzahlung
  (10,44 $) – eine Einzahlung verdeckte Verluste. Jetzt `live: deposits_usd` (eingezahlt gesamt, 87,00 $; bei jeder
  Ein-/Auszahlung anpassen!): ändert er sich, beginnt die Tagesbasis neu; `pnl_total` = Kontowert − Einzahlungen
  (Cockpit/admin „Handel seit Start“). Polymarket 15.10. 20:42: Portfolio 86,00 $, verfügbar 72,97 $, P/L 24 h −3,10 $.
  Rewards unter 1 $ STAPELN (Nutzer, laut polymarket.com): die 1,90 $ „Oct 16 Earnings“ enthalten die 0,19 $ vom 14.10.
  Auch die API-Tagessumme für den 15.10. (1,907 $) enthält sie offenbar – beim Vergleich echt/geschätzt nicht doppelt zählen.
  **Nacht 15./16.: schwere Verluste.** Konto 76,48 $ bei 87 $ Einzahlung (−10,52 $), obwohl ~2,39 $ Rewards gezahlt wurden
  (Cash-Sprung 02:08; API-Summe 15.10. = 2,48 $). Realisiert −11,22 $ aus wenigen Fills: Gebote, die nach schnellen Kurs-
  sprüngen ÜBER der neuen Mitte standen (EWY: NEIN-Gebot 0,41 bei NEIN-Mitte 0,37 → −3,8 $; Florida Panthers (NHL-
  Saisonmarkt, rutschte durch den Sport-Filter): JA 0,50 bei Mitte 0,385 → −2,6 $, NEIN 0,57 → −1,2 $; Wahlbeteiligung
  −1,8 $; −1,8 $ Altposition). Finanzmärkte (EWY/SPY „hit … in October“) und Sport-Saisonmärkte sind gefährlich.
  16.10. 07:02 MESZ **falscher Not-Aus**: data-api /value lieferte einmal 0 $ Positionen (echt 20 Panthers-NEIN = 10 $),
  Konto also 66,48 statt 76,48 $ → „Tagesverlust 16,80 $“. Behoben: Not-Aus erst, wenn ein zweites Lesen 60 s später
  das Limit ebenfalls zeigt. Echter Tagesverlust zu dem Zeitpunkt ≈ −6,80 $ (Polymarket: 1D −12,54 $ inkl. Nacht).
  16.10. Nutzer: mm_probe meidet jetzt Finanz (`skip_categories`), Sport-Saison- und Kursmärkte (neue Option `skip_re`:
  Regex auf Frage/Slug/Event-Titel, Scan „Muster ausgelassen“) und pausiert nach Kurssprüngen (`pause:` wie mm_ruhig).
  Rest-Bestand 20 Panthers-NEIN bekommt im Echtbetrieb weiter ein Verkaufsgebot (Treppe zur Mitte).
  16.10. Nutzer: Tagesbasis zurückgesetzt – neue Option `live: day_reset` (Text ändern = Tagesstart beim aktuellen Kontowert).
  16.10.: wetter_no_maker archiviert (offen 0); ladder (offen 1, ohne Ergebnis) und sharp_fussball (offen 1) noch aktiv.
  Im echten Modus kommen Bestand/Cash aus den echten Fills (`apply_real_fills`, `own_fills` liest Maker-Seite aus
  maker_orders), echte Rewards je UTC-Tag stehen neben der Schätzung (Dashboard-Karte, 4-h-Bericht), Push-Alarme bei
  Not-Aus, vielen Ablehnungen, wenig Scoring, stummem Order-Modul.
- `mm_breit` (ab 10.10.): wie `mm_rewards`, aber 40 Märkte nach erwartetem Reward (`rank: share`), 20 $ je Gebot,
  nur Märkte mit Reward-Mindestgröße ≤ 25 $ (`max_min_size_usd`), Bestand gesamt max. 1.500 $
  (`max_total_inventory_usd`, darüber nur Gebote, die Positionen abbauen). Frage: breit + klein besser als 15 groß?
- `sharp_fussball` (ab 06.10., `arb/sharp.py`): kauft Polymarket-Fußball (Sieg/Remis), wenn der Ask ≥ 5 Punkte unter
  Pinnacles Wahrscheinlichkeit liegt (live über The Odds API, Gratis-Plan 500 Credits/Monat, Key über `/admin/`
  oder `ODDS_API_KEY` in `/etc/polyarb.env`, Verbrauch in `data/odds-api.json`). Ohne Key passiert nichts.
- `fussball_dog`: Außenseiter 3–25 % in Über/Unter, Spread, Remis, Halbzeit. Studie +8,8 %, phasenabhängig.
  Bekannte Lücke: `sport_kind` fällt auf „Fussball“ zurück, daher landen auch NHL/NFL/College/WNBA und
  E-Sports-Handicaps/„Games Total“ darin (Backtest nutzt dieselbe Einteilung). Nicht ändern während des Tests;
  ggf. ein reines Fußball-Szenario daneben bauen.
- `mlb_spread`, `ladder` (Arbitrage, nach Bugfix „T“=Billionen und `max_edge_bps` 1500 neu gestartet).
