# polyarb – Kontext für neue Sessions

Paper-Trading- und Research-Bot für Polymarket (plus Kalshi-Studie). Ziel: einen echten Edge finden und
validieren, bevor echtes Geld (später 1–2k €) eingesetzt wird. Der Nutzer schreibt Deutsch – auf Deutsch antworten.

## Arbeitsweise
- Direkt auf `main` committen und pushen. Der Hetzner-Server holt sich alle 10 Minuten den Stand
  (`deploy/update.sh` → `deploy/install.sh`), Dienste laufen als systemd-Timer (`deploy/systemd`).
- Vor jedem Push `python -m pytest -q` (alle Tests müssen grün sein).
- **Regeln der laufenden Szenarien nicht ändern**, solange der Live-Test läuft (seit Anfang Oktober 2026, 2–3 Wochen).
  Verbesserungen als *neues* Szenario daneben bauen, nicht das bestehende umschreiben.
- Geheimnisse (Odds-API-Key etc.) liegen in `/etc/polyarb.env` auf dem Server, nie im Repo oder Chat.

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
- `arb/kalshi.py` – Kalshi-Studie; `arb/ladder.py` + `arb/scanner.py` – Arbitrage
- `dashboard.py` – statisches Dashboard + Exporte; `deploy/notify.py` – 4-h-Bericht per Push

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

Aktive Kandidaten im Live-Test:
- `wetter_no_breit` / `wetter_no_mess` / `wetter_no_streng`: NO auf Temperatur-Buckets, 55–97 %, 2–12 h vor Schluss,
  max. 1.000 $/Tag. Studie +8,2 % (OOS +5,8 %); Stations-Filter streng +10,7 % (n≈5.200) – beides mit dem
  Endvolumen-Filter, ohne ihn ≈ 0 % (siehe oben). Live bisher Treffer ≈ Preis oder darunter.
  Kapazität: Orderbücher deutlich tiefer als das Tageslimit.
- `fussball_dog`: Außenseiter 3–25 % in Über/Unter, Spread, Remis, Halbzeit. Studie +8,8 %, phasenabhängig.
  Bekannte Lücke: `sport_kind` fällt auf „Fussball“ zurück, daher landen auch NHL/NFL/College/WNBA und
  E-Sports-Handicaps/„Games Total“ darin (Backtest nutzt dieselbe Einteilung). Nicht ändern während des Tests;
  ggf. ein reines Fußball-Szenario daneben bauen.
- `mlb_spread`, `ladder` (Arbitrage, nach Bugfix „T“=Billionen und `max_edge_bps` 1500 neu gestartet).
