"""FarmDirect — demand & price forecasting models.

Ported from the team's `demand forecast model` (model_trainer.py):
  * one RandomForest for the modal PRICE   (Rs / quintal)
  * one RandomForest for DEMAND / arrivals (quintals)
  * recursive 14-day forecast starting from today

Changes vs. the Streamlit prototype (behaviour of the models is otherwise the same):
  1. Training / forecasting are split so models can be persisted (joblib) and
     evaluated — the web app never retrains on a page load.
  2. The 7-day rolling means are now *updated* inside the recursive loop
     (the prototype froze them at their last observed value).
  3. Hold-out metrics (one-step and full 14-day recursive) are computed so the
     admin / AI-insight pages can show honest model accuracy.
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestRegressor

FEATURES = ["day_of_week", "month", "day", "price_lag_1", "demand_lag_1",
            "price_rolling_7", "demand_rolling_7"]
HORIZON = 14
HOLDOUT = 14
MIN_ROWS = 40          # need at least ~6 weeks of daily data to train sensibly


def _rf():
    return RandomForestRegressor(n_estimators=100, random_state=42, n_jobs=-1)


def fit_models(df):
    """Fit the price and demand RandomForests on a prepared commodity frame."""
    X = df[FEATURES]
    price = _rf().fit(X, df["Modal Price"])
    demand = _rf().fit(X, df["Arrival Quantity"])
    return price, demand


def recursive_forecast(models, df, future_dates):
    """Recursive forecast for `future_dates`, seeded from the last rows of `df`.

    Predicted price / demand feed back in as the next day's lag and rolling
    features, exactly like the prototype (but with rolling means kept current).
    """
    price_model, demand_model = models
    p_hist = [float(v) for v in df["Modal Price"]]
    d_hist = [float(v) for v in df["Arrival Quantity"]]
    out_p, out_d = [], []
    for fd in future_dates:
        feat = pd.DataFrame([{
            "day_of_week": fd.weekday(), "month": fd.month, "day": fd.day,
            "price_lag_1": p_hist[-1], "demand_lag_1": d_hist[-1],
            "price_rolling_7": float(np.mean(p_hist[-7:])),
            "demand_rolling_7": float(np.mean(d_hist[-7:])),
        }])[FEATURES]
        p = max(0.0, round(float(price_model.predict(feat)[0]), 2))
        d = max(0.0, round(float(demand_model.predict(feat)[0]), 2))
        out_p.append(p)
        out_d.append(d)
        p_hist.append(p)
        d_hist.append(d)
    return out_p, out_d


def forecast_dates(days=HORIZON, start=None):
    start = start or date.today()
    return [start + timedelta(days=i) for i in range(1, days + 1)]


def train_and_forecast(commodity_df, days=HORIZON):
    """Prototype-compatible helper: returns a DataFrame like the Streamlit app did."""
    models = fit_models(commodity_df)
    dates = forecast_dates(days)
    prices, demands = recursive_forecast(models, commodity_df, dates)
    return pd.DataFrame({
        "Date": [d.strftime("%Y-%m-%d") for d in dates],
        "Predicted Price (Rs/Quintal)": prices,
        "Predicted Demand (Quintals)": demands,
    })


def _mape(pred, actual):
    pred, actual = np.asarray(pred, float), np.asarray(actual, float)
    mask = actual > 0
    return float(np.mean(np.abs(pred[mask] - actual[mask]) / actual[mask]) * 100) if mask.any() else float("nan")


def _wape(pred, actual):
    pred, actual = np.asarray(pred, float), np.asarray(actual, float)
    return float(np.abs(pred - actual).sum() / max(actual.sum(), 1e-9) * 100)


def evaluate(df, holdout=HOLDOUT):
    """Hold out the last `holdout` days and score two ways:
       * one-step  : each day predicted from the *actual* previous day
       * recursive : the full 14-day forecast as the app actually produces it
    """
    if len(df) < MIN_ROWS + holdout:
        return None
    train, test = df.iloc[:-holdout], df.iloc[-holdout:]
    models = fit_models(train)

    p1 = models[0].predict(test[FEATURES])
    d1 = models[1].predict(test[FEATURES])

    dates = [d.date() for d in test["Date"]]
    pr, dr = recursive_forecast(models, train, dates)

    # honesty check: does the RandomForest beat "tomorrow = the last 7-day mean"?
    naive_price = [float(train["Modal Price"].tail(7).mean())] * len(test)

    return {
        "price_mape_naive_14d": round(_mape(naive_price, test["Modal Price"]), 1),
        "price_mape_1step": round(_mape(p1, test["Modal Price"]), 1),
        "price_mape_14d": round(_mape(pr, test["Modal Price"]), 1),
        "demand_wape_1step": round(_wape(d1, test["Arrival Quantity"]), 1),
        "demand_wape_14d": round(_wape(dr, test["Arrival Quantity"]), 1),
        "price_mae_qtl": round(float(np.mean(np.abs(np.array(pr) - test["Modal Price"].values))), 1),
        "n_train": int(len(train)), "n_test": int(len(test)),
    }


def trend_label(last7_mean, next7_mean, band=8.0):
    """HIGH / STABLE / LOW demand outlook (+/- `band` % vs the last 7 days)."""
    if not last7_mean:
        return "STABLE", 0
    pct = (next7_mean - last7_mean) / last7_mean * 100
    label = "HIGH" if pct >= band else "LOW" if pct <= -band else "STABLE"
    return label, round(pct)
