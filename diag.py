"""Diagnostics / controls to stress the verdict from backtest.py.

Guards against: a mechanization bug, a too-strict entry filter, the
SL-first tie-break, and costs being the sole cause. Also tests the
mirror-image (momentum/continuation) reading in case the edge is on the
opposite side of the fade.
"""
import sys
import numpy as np
import pandas as pd
from backtest import load_data, prev_level_map, metrics, fmt, POINT

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else "."


def sim(d1, m1, direction="fade", rr=3.0, max_sl=8.0, slip=2.0,
        use_cost=True, tp_first=False, max_trades=3, max_full_sl=2):
    """direction: 'fade' (transcript) or 'momentum' (continuation, mirror).
    Returns list of R-multiples."""
    plevels = prev_level_map(d1)
    R = []
    for date, day in m1.groupby("date"):
        if date not in plevels:
            continue
        day = day.sort_values("time").reset_index(drop=True)
        if len(day) < 30:
            continue
        prevH, prevL = plevels[date]
        o = day["open"].to_numpy(); h = day["high"].to_numpy()
        l = day["low"].to_numpy();  c = day["close"].to_numpy()
        spr = day["spread"].to_numpy(); n = len(day)

        armed_hi = armed_lo = False
        sig_up = sig_dn = None          # up=green signal, dn=red signal
        last_hi = -1e18; last_lo = 1e18
        ntr = nfull = 0
        i = 0
        while i < n and ntr < max_trades and nfull < max_full_sl:
            if h[i] > prevH: armed_hi = True
            if l[i] < prevL: armed_lo = True
            red = c[i] < o[i]; green = c[i] > o[i]

            # signal candles depend on which sweep is relevant per direction
            if direction == "fade":
                # short after high-sweep (red sig above line); long after low-sweep (green sig below line)
                if armed_hi and red and h[i] >= prevH and h[i] > last_hi:
                    sig_dn = (l[i], h[i], i)
                if armed_lo and green and l[i] <= prevL and l[i] < last_lo:
                    sig_up = (h[i], l[i], i)
            else:  # momentum: long after high-sweep (green sig), short after low-sweep (red sig)
                if armed_hi and green and h[i] >= prevH and h[i] > last_hi:
                    sig_up = (h[i], l[i], i)
                if armed_lo and red and l[i] <= prevL and l[i] < last_lo:
                    sig_dn = (l[i], h[i], i)

            side = entry = slp = risk = None
            # trigger: short on break of red sig low; long on break of green sig high
            if sig_dn is not None and i > sig_dn[2] and l[i] < sig_dn[0]:
                lo, hi, s = sig_dn
                entry = lo - slip * POINT; slp = hi; risk = slp - entry
                side = "short"; last_hi = hi; sig_dn = None
            elif sig_up is not None and i > sig_up[2] and h[i] > sig_up[0]:
                hi, lo, s = sig_up
                entry = hi + slip * POINT; slp = lo; risk = entry - slp
                side = "long"; last_lo = lo; sig_up = None

            if side is None or not (0 < risk <= max_sl):
                i += 1; continue

            cost = ((spr[i] * POINT) + slip * POINT) if use_cost else 0.0
            tp = entry - rr * risk if side == "short" else entry + rr * risk
            j = i + 1; r = None
            while j < n:
                if side == "short":
                    hs = h[j] >= slp; ht = l[j] <= tp
                else:
                    hs = l[j] <= slp; ht = h[j] >= tp
                if hs and ht:
                    if tp_first: hs = False
                    else: ht = False
                if hs:
                    g = (entry - slp) if side == "short" else (slp - entry)
                    r = (g - cost) / risk; nfull += 1; break
                if ht:
                    g = (entry - tp) if side == "short" else (tp - entry)
                    r = (g - cost) / risk; break
                j += 1
            if r is None:
                px = c[n - 1]
                g = (entry - px) if side == "short" else (px - entry)
                r = (g - cost) / risk
            R.append(r); ntr += 1
            i = max(i + 1, s + 1)
    return R


def line(tag, R):
    print(f"{tag:38s} {fmt(metrics([type('T',(),{'pnl_r':x})() for x in R]))}")


def main():
    d1, m1 = load_data()
    print("CONTROLS  (RR 1:3 unless noted)\n" + "-" * 90)
    line("FADE  base (cost, SL-first)",   sim(d1, m1, "fade"))
    line("FADE  optimistic (TP-first)",   sim(d1, m1, "fade", tp_first=True))
    line("FADE  NO costs",                sim(d1, m1, "fade", use_cost=False))
    line("FADE  no SL cap, no cost, TPfirst", sim(d1, m1, "fade", max_sl=1e9, use_cost=False, tp_first=True))
    print("-" * 90)
    line("MOMENTUM base (cost, SL-first)", sim(d1, m1, "momentum"))
    line("MOMENTUM optimistic (TP-first)", sim(d1, m1, "momentum", tp_first=True))
    line("MOMENTUM NO costs",             sim(d1, m1, "momentum", use_cost=False))
    print("-" * 90)
    for rr in (1, 1.5, 2, 3):
        line(f"FADE rr1:{rr} no-cost TPfirst(upper bound)",
             sim(d1, m1, "fade", rr=rr, use_cost=False, tp_first=True))

    # eyeball 6 sample fade trades against raw data
    print("\nSAMPLE TRADES (fade, rr3) — verify against chart logic")
    tr = pd.read_csv(f"{DATA_DIR}/trades_rr3.csv")
    print(tr[["date", "side", "entry_time", "entry", "sl", "risk_usd",
              "exit", "outcome", "pnl_r"]].head(6).to_string(index=False))
    print(f"\ntrades/day avg = {len(tr)/tr['date'].nunique():.2f}, "
          f"days with >=1 trade = {tr['date'].nunique()}")


if __name__ == "__main__":
    main()
