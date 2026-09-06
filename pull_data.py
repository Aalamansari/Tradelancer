"""
Pull XAUUSD historical data (D1 + M1) from the running MT5 terminal to CSV.
Read-only: this script only downloads price history. It places no orders.
"""
import sys
from datetime import datetime, timedelta, timezone
import MetaTrader5 as mt5
import pandas as pd

SYMBOL = "XAUUSD"
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "."


def main():
    if not mt5.initialize():
        print("initialize() failed:", mt5.last_error())
        sys.exit(1)

    info = mt5.account_info()
    print("Connected account:", info.login, "type=",
          "DEMO" if info.trade_mode == 0 else ("CONTEST" if info.trade_mode == 1 else "REAL"),
          "balance=", info.balance, info.currency)

    si = mt5.symbol_info(SYMBOL)
    if si is None:
        if not mt5.symbol_select(SYMBOL, True):
            print("cannot select", SYMBOL)
            sys.exit(1)
        si = mt5.symbol_info(SYMBOL)
    print(f"Symbol {SYMBOL}: digits={si.digits} point={si.point} "
          f"trade_contract_size={si.trade_contract_size} "
          f"volume_min={si.volume_min} volume_step={si.volume_step} "
          f"spread(pts,current)={si.spread}")

    # --- Daily candles: pull plenty ---
    d1 = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_D1, 0, 1500)
    d1 = pd.DataFrame(d1)
    d1["time"] = pd.to_datetime(d1["time"], unit="s")
    d1.to_csv(f"{OUT_DIR}/xauusd_d1.csv", index=False)
    print(f"D1: {len(d1)} rows  {d1['time'].min()} -> {d1['time'].max()}")

    # --- M1 candles: pull month-by-month range chunks going back, until empty ---
    # MT5 wants naive datetimes (interpreted in terminal/server time).
    frames = []
    end = datetime.now() + timedelta(days=2)
    MONTHS_BACK = 36
    empty_streak = 0
    for m in range(MONTHS_BACK):
        chunk_end = end - timedelta(days=30 * m)
        chunk_start = end - timedelta(days=30 * (m + 1))
        rates = mt5.copy_rates_range(SYMBOL, mt5.TIMEFRAME_M1, chunk_start, chunk_end)
        n = 0 if rates is None else len(rates)
        if n == 0:
            empty_streak += 1
            if empty_streak >= 2:  # two empty older chunks => no more history
                break
            continue
        empty_streak = 0
        frames.append(pd.DataFrame(rates))

    if not frames:
        print("M1 pull returned nothing; err=", mt5.last_error())
        sys.exit(1)

    m1 = pd.concat(frames, ignore_index=True)
    m1["time"] = pd.to_datetime(m1["time"], unit="s")
    m1 = m1.drop_duplicates(subset="time").sort_values("time").reset_index(drop=True)
    m1.to_csv(f"{OUT_DIR}/xauusd_m1.csv", index=False)
    print(f"M1: {len(m1)} rows  {m1['time'].min()} -> {m1['time'].max()}")

    mt5.shutdown()


if __name__ == "__main__":
    main()
