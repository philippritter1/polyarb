# Research-Skripte (lokal ausführen, nicht Teil des Bots)

## S&P 500 „Up or Down“ (Polymarket) gegen fairen Wert aus Future + VIX1D (20.11.2026)
1. `python research/spx_updown_fetch.py <ordner>` – alle täglichen SPX-Up/Down-Märkte (gamma, Slug
   `spx-up-or-down-on-<monat>-<tag>-<jahr>`) und minutengenaue Preise (CLOB `prices-history`, fidelity 1) nach `<ordner>/`.
2. Yahoo-Daten in denselben Ordner: `ES%3DF_1h.json` (ES=F, interval=60m, includePrePost), `%5EGSPC_1d.json`
   (Tagesschluss), dazu `VIX1D.csv` von `https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX1D_History.csv`.
3. `python research/spx_updown_backtest.py <ordner>` – Modell: Index jetzt = Vortagesschluss × ES(t)/ES(Schluss Vortag),
   Restschwankung = VIX1D(Vortag)/√252 × √(Anteil verbleibender Tagesvarianz, 25 % über Nacht), P(Up) = N(d2) ohne Schieflage.
