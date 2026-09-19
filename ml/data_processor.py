"""FarmDirect — mandi (Agmarknet) data loading & feature engineering.

Ported from the team's `demand forecast model` (data_processor.py). Logic is
unchanged: clean the CSV, convert arrivals to quintals, aggregate to one row
per day and add calendar / lag / rolling features.

New here: `load_all()` reads every *.csv in ml/data/, so adding more crops or
districts is just a matter of dropping another Agmarknet export in that folder
and re-running `python ml/train_models.py`.
"""

import glob
import os

import pandas as pd

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE, "ml", "data")


def load_and_clean_data(file_path):
    """Load one agmarket CSV, keep essential columns, convert arrival quantity
    to Quintals, standardise dates and return the processed frame."""
    df = pd.read_csv(file_path)
    df.columns = [c.strip() for c in df.columns]

    required_cols = {
        "Arrival Date": "Date",
        "Commodity": "Commodity",
        "Modal Price": "Modal Price",
        "Arrival Quantity": "Arrival Quantity",
        "Arrival Unit": "Arrival Unit",
    }
    df = df.rename(columns={c: required_cols[c] for c in df.columns if c in required_cols})

    # numeric clean-up ("1,000.00" -> 1000.0)
    for col in ("Modal Price", "Arrival Quantity"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col].astype(str).str.replace(",", ""), errors="coerce")

    # Tonnes -> Quintals (1 tonne = 10 quintals)
    if "Arrival Unit" in df.columns and "Arrival Quantity" in df.columns:
        unit = df["Arrival Unit"].astype(str).str.lower()
        is_tonne = unit.str.contains("ton", na=False) | unit.str.contains("mt", na=False)
        df.loc[is_tonne, "Arrival Quantity"] = df.loc[is_tonne, "Arrival Quantity"] * 10

    # The Agmarknet export mixes "31-08-2026" and "12/8/2026" styles. Without
    # format="mixed", pandas locks onto the first row's format and silently turns
    # every row in the other style into NaT (dropping ~40% of the data).
    df["Date"] = pd.to_datetime(df["Date"].astype(str).str.strip(), format="mixed",
                                dayfirst=True, errors="coerce")
    df = df.dropna(subset=["Date"]).sort_values("Date").reset_index(drop=True)

    df["Commodity"] = df["Commodity"].astype(str).str.strip().str.title()
    for col in ("Market", "Commodity Group"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()

    df["Modal Price"] = df["Modal Price"].ffill().bfill()
    df["Arrival Quantity"] = df["Arrival Quantity"].ffill().bfill()
    return df


def load_all(data_dir=DATA_DIR):
    """Concatenate every CSV in ml/data/ (Agmarknet export format)."""
    files = sorted(glob.glob(os.path.join(data_dir, "*.csv")))
    if not files:
        return None
    frames = [load_and_clean_data(f) for f in files]
    return pd.concat(frames, ignore_index=True).sort_values("Date").reset_index(drop=True)


def prepare_commodity_dataset(df, commodity_name):
    """Filter one commodity, aggregate daily totals (quintals) and create the
    lag / calendar features used by the RandomForest models."""
    filtered = df[df["Commodity"].str.lower() == commodity_name.strip().lower()].copy()
    if filtered.empty:
        return None

    daily = filtered.groupby("Date").agg({"Modal Price": "mean",
                                          "Arrival Quantity": "sum"}).reset_index()

    daily["day_of_week"] = daily["Date"].dt.dayofweek
    daily["month"] = daily["Date"].dt.month
    daily["day"] = daily["Date"].dt.day

    daily["price_lag_1"] = daily["Modal Price"].shift(1)
    daily["demand_lag_1"] = daily["Arrival Quantity"].shift(1)
    daily["price_rolling_7"] = daily["Modal Price"].rolling(window=7, min_periods=1).mean()
    daily["demand_rolling_7"] = daily["Arrival Quantity"].rolling(window=7, min_periods=1).mean()

    return daily.ffill().bfill()


def latest_mandi_price_per_kg(df, commodity, days=14):
    """Recent average modal price in Rs/kg (used to seed listing prices)."""
    if df is None:
        return None
    daily = prepare_commodity_dataset(df, commodity)
    if daily is None or daily.empty:
        return None
    return round(float(daily["Modal Price"].tail(days).mean()) / 100.0, 2)
