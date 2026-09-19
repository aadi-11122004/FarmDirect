# ML pipeline

1. **Load** — every `ml/data/*.csv` (Agmarknet export) is cleaned by `data_processor.load_and_clean_data`:
   numeric clean-up, tonnes → quintals, mixed date styles (`31-08-2026` and `12/8/2026`) parsed with
   `format="mixed", dayfirst=True`, commodity names normalised. Raw rows go to `market_price_historical`.
2. **Features** — per commodity, one row per day: mean modal price, summed arrivals (quintals),
   day-of-week / month / day, lag-1 and 7-day rolling mean of price and demand.
3. **Evaluate** — last 14 days held out; scored one-step (MAPE for price, WAPE for demand) and as the full
   14-day recursive forecast the app shows, next to a naive "last 7-day mean" baseline.
4. **Train + forecast** — two `RandomForestRegressor(n_estimators=100)` (price, demand) on all data; recursive
   forecast for the next 14 days (predictions feed the next day's lag and rolling features).
5. **Persist** — `demand_forecast_logs` (read by the app and `/api/forecast/<crop>`), `ml/models/models.pkl`,
   `ml/models/metrics.json`.

Outlook label: mean predicted demand of the next 7 days vs the last 7 observed days — HIGH ≥ +8 %, LOW ≤ −8 %.
Suggested farm-gate price = 0.84 × mean forecast mandi price, rounded to ₹0.5.
A commodity needs ≥ 40 daily rows to be modelled.
