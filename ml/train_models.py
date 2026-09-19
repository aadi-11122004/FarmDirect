"""FarmDirect — ML training pipeline.

  1. Load every Agmarknet CSV in ml/data/  ->  table `market_price_historical`
  2. For each commodity with enough history:
       - hold-out evaluation (one-step + 14-day recursive)
       - fit RandomForest PRICE + DEMAND models on all data
       - recursive 14-day forecast starting tomorrow
  3. Persist:
       ml/models/models.pkl      trained models
       ml/models/metrics.json    hold-out accuracy
       demand_forecast_logs      forecast rows read by the web app / JSON API

Run after seeding:   python seed/seed_db.py && python ml/train_models.py
"""

import json
import os
import sqlite3
import sys
from datetime import datetime

import joblib
import numpy as np

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
from ml.data_processor import load_all, prepare_commodity_dataset  # noqa: E402
from ml.model_trainer import (HORIZON, MIN_ROWS, evaluate, fit_models,  # noqa: E402
                              forecast_dates, recursive_forecast, trend_label)

DB = os.path.join(BASE, "farmlink.db")
MODELS_DIR = os.path.join(BASE, "ml", "models")


def main():
    if not os.path.exists(DB):
        sys.exit("farmlink.db not found — run `python seed/seed_db.py` first.")
    df = load_all()
    if df is None or df.empty:
        sys.exit("No CSV found in ml/data/ — drop an Agmarknet export there.")

    os.makedirs(MODELS_DIR, exist_ok=True)
    con = sqlite3.connect(DB)

    # ---- 1. historical mandi data -------------------------------------
    con.execute("DELETE FROM market_price_historical")
    rows = [(r["Date"].strftime("%Y-%m-%d"), r.get("Commodity Group", ""), r["Commodity"],
             r["Commodity"], r.get("Market", ""), float(r["Modal Price"]),
             float(r["Arrival Quantity"])) for _, r in df.iterrows()]
    con.executemany("INSERT INTO market_price_historical(recorded_date,category,commodity,"
                    "crop_name,mandi_name,modal_price,arrival_volume) VALUES(?,?,?,?,?,?,?)", rows)
    print(f"[train] loaded {len(rows)} mandi rows "
          f"({df['Date'].min():%Y-%m-%d} -> {df['Date'].max():%Y-%m-%d}), "
          f"commodities: {sorted(df['Commodity'].unique())}")

    # ---- 2. train + forecast per commodity ----------------------------
    con.execute("DELETE FROM demand_forecast_logs")
    models, metrics, n_rows_fc = {}, {}, 0
    dates = forecast_dates(HORIZON)
    for commodity in sorted(df["Commodity"].unique()):
        daily = prepare_commodity_dataset(df, commodity)
        if daily is None or len(daily) < MIN_ROWS:
            print(f"[train] {commodity:<12} skipped — only {0 if daily is None else len(daily)} "
                  f"daily rows (need {MIN_ROWS}+)")
            continue
        met = evaluate(daily)
        m = fit_models(daily)
        p_fc, d_fc = recursive_forecast(m, daily, dates)

        last7 = float(daily["Arrival Quantity"].tail(7).mean())
        label, pct = trend_label(last7, float(np.mean(d_fc[:7])))
        category = str(df.loc[df["Commodity"] == commodity, "Commodity Group"].iloc[0])
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        con.executemany(
            "INSERT INTO demand_forecast_logs(category,commodity,forecast_date,"
            "predicted_demand_qty,predicted_price_qtl,demand_trend_label,generated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            [(category, commodity, d.isoformat(), dq, pq, label, now)
             for d, dq, pq in zip(dates, d_fc, p_fc)])
        n_rows_fc += len(dates)
        models[commodity] = m
        metrics[commodity] = met or {}
        print(f"[train] {commodity:<12} {len(daily):>3} days  outlook {label} ({pct:+d}%)  "
              f"price MAPE(14d) {met and met['price_mape_14d']}%  "
              f"demand WAPE(14d) {met and met['demand_wape_14d']}%")
    con.commit()
    con.close()

    # ---- 3. artefacts --------------------------------------------------
    joblib.dump(models, os.path.join(MODELS_DIR, "models.pkl"))
    scored = [m for m in metrics.values() if m]
    avg = lambda k: round(float(np.mean([m[k] for m in scored])), 1) if scored else None  # noqa: E731
    overall = {
        "price_mape": avg("price_mape_14d"), "price_mape_naive": avg("price_mape_naive_14d"),
        "demand_mape": avg("demand_wape_14d"),
        "n_crops": len(models), "horizon_days": HORIZON,
        "trained_on": f"{df['Date'].min():%d %b} – {df['Date'].max():%d %b %Y}",
    }
    with open(os.path.join(MODELS_DIR, "metrics.json"), "w") as f:
        json.dump({"overall": overall, "per_crop": metrics}, f, indent=2)
    print(f"[train] done — {n_rows_fc} forecast rows for {len(models)} commodities. Overall: {overall}")


if __name__ == "__main__":
    main()
