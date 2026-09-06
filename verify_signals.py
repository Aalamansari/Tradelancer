"""
Verification tool: list the exact BUY/SELL *arrows* the GCP Trend Sniper would
print, computed by our shared Python engine (gcp_engine.build_indicators).

Purpose: prove the port is faithful. You run this, then open the SAME symbol /
timeframe / dates on your TradingView chart and confirm the arrows line up. If
they match, the Python engine reproduces the indicator and the live bot can be
trusted. If they drift, we fix gcp_engine.py and re-run.

This reproduces the indicator's *label/alert* logic exactly:
  arrow fires on a CLOSED bar when  raw_buy & filt & confirm & (>=8 bars since
  last entry) & not-already-in-that-direction; the indicator's own exit monitor
  (SL=2*ATR / TP4=+4*ATR / opposite signal) frees the direction for re-entry.
That is the same stateful logic gcp_backtest.simulate() uses for entries, so the
arrow timestamps here equal the entry_time column of the backtest blotter.

Each arrow is also annotated with ADX and the ratio the LIVE bot would target
(ADX>25 -> 1:3, else 1:2). That annotation is live-bot info; the arrow *timing*
is the indicator's.

Read-only. Reads a CSV of M5 candles. Places no orders, touches no account.

Usage:
  python verify_signals.py                 # last 10 days of ./xauusd_m5.csv
  python verify_signals.py --days 5
  python verify_signals.py --data path/to/xauusd_m5.csv --days 30
"""
from __future__ import annotations

import argparse

import numpy as np
import pandas as pd

from gcp_engine import build_indicators

COOLDOWN = 8


def find_arrows(d: pd.DataFrame) -> pd.DataFrame:
    """Walk bars with the indicator's stateful arrow logic; return one row per arrow."""
    h = d["high"].to_numpy()
    l = d["low"].to_numpy()
    c = d["close"].to_numpy()
    atr = d["atr"].to_numpy()
    t = d["time"].to_list()
    adx = d["adx"].to_numpy()

    raw_buy = d["raw_buy"].to_numpy()
    raw_sell = d["raw_sell"].to_numpy()
    filt = d["filt"].to_numpy()
    cb = d["confirm_bull"].to_numpy()
    cs = d["confirm_bear"].to_numpy()
    A_buy, B_buy, C_buy = d["A_buy"].to_numpy(), d["B_buy"].to_numpy(), d["C_buy"].to_numpy()
    A_sell, B_sell, C_sell = d["A_sell"].to_numpy(), d["B_sell"].to_numpy(), d["C_sell"].to_numpy()
    valid = d["atr"].notna().to_numpy() & d["vol_ok"].notna().to_numpy()
    n = len(d)

    active = "None"       # None / Long / Short
    entry = sl = tp4 = 0.0
    last_trade_bar = -10 ** 9
    rows = []

    def setup_label(is_buy: bool, i: int) -> str:
        if is_buy:
            return "PULLBACK" if B_buy[i] else "SQUEEZE" if C_buy[i] else "TREND"
        return "PULLBACK" if B_sell[i] else "SQUEEZE" if C_sell[i] else "TREND"

    for i in range(n):
        if not valid[i]:
            continue
        cooldown_ok = (i - last_trade_bar) > COOLDOWN
        buy_sig = bool(raw_buy[i] and filt[i] and cb[i] and cooldown_ok)
        sell_sig = bool(raw_sell[i] and filt[i] and cs[i] and cooldown_ok)

        # exit monitor frees the direction (SL / TP4 / opposite) -- indicator logic
        if active == "Long":
            if l[i] <= sl or h[i] >= tp4 or sell_sig:
                active = "None"
        elif active == "Short":
            if h[i] >= sl or l[i] <= tp4 or buy_sig:
                active = "None"

        # entries (Long then Short, same bar)
        if buy_sig and active != "Long":
            entry = c[i]; sl = entry - 2 * atr[i]; tp4 = entry + 4 * atr[i]
            active = "Long"; last_trade_bar = i
            rows.append(dict(time=t[i], side="BUY", setup=setup_label(True, i),
                             price=round(entry, 3), adx=round(float(adx[i]), 1),
                             target="1:3" if adx[i] > 25 else "1:2"))
        elif sell_sig and active != "Short":
            entry = c[i]; sl = entry + 2 * atr[i]; tp4 = entry - 4 * atr[i]
            active = "Short"; last_trade_bar = i
            rows.append(dict(time=t[i], side="SELL", setup=setup_label(False, i),
                             price=round(entry, 3), adx=round(float(adx[i]), 1),
                             target="1:3" if adx[i] > 25 else "1:2"))

    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description="List GCP Trend Sniper BUY/SELL arrows from the Python engine.")
    ap.add_argument("--data", default="xauusd_m5.csv", help="CSV of M5 candles (default ./xauusd_m5.csv)")
    ap.add_argument("--days", type=int, default=10, help="show arrows from the last N days of data (default 10)")
    args = ap.parse_args()

    df = pd.read_csv(args.data, parse_dates=["time"])
    d = build_indicators(df)

    tmax = d["time"].max()
    cutoff = tmax - pd.Timedelta(days=args.days)

    arrows = find_arrows(d)
    span_note = f"{d['time'].min()} -> {tmax}  ({len(d)} M5 bars)"
    print(f"Data: {args.data}")
    print(f"Coverage: {span_note}")
    print("NOTE: times are the CSV's timestamps = your MT5 broker's SERVER time")
    print("      (often UTC+2/+3), NOT IST and NOT your TradingView timezone.")
    print("      Set TradingView to the broker's timezone before comparing bars.\n")

    if arrows.empty:
        print("No arrows in the dataset.")
        return

    recent = arrows[arrows["time"] >= cutoff].copy()
    print(f"Total arrows in dataset: {len(arrows)}  "
          f"(BUY {int((arrows['side']=='BUY').sum())} / SELL {int((arrows['side']=='SELL').sum())})")
    print(f"Showing last {args.days} days: {len(recent)} arrows "
          f"(from {cutoff})\n")

    if recent.empty:
        print("(none in that window -- widen --days)")
        return

    recent["time"] = recent["time"].astype(str)
    print(recent.to_string(index=False))
    print("\nCompare these BUY/SELL bars against the arrows on your TradingView chart.")
    print("The 'target' column is what the LIVE bot would aim for (ADX>25 -> 1:3, else 1:2);")
    print("the arrow timing itself is the indicator's.")


if __name__ == "__main__":
    main()
