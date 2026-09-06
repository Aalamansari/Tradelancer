"""
Backtest for the "liquidity sweep" scalping strategy distilled from the
transcript (Gautam bhai / XAUUSD).

RULES (mechanized):
  - Levels = previous completed DAILY candle's high (prevH) and low (prevL).
  - Work on M1 within each trading day.
  - SHORT: after price sweeps ABOVE prevH, wait for a red 1m candle that
    printed above the line; enter short when a later bar breaks that red
    candle's LOW. SL = red candle's high. (fade the liquidity grab)
  - LONG: after price sweeps BELOW prevL, wait for a green 1m candle that
    printed below the line; enter long when a later bar breaks that green
    candle's HIGH. SL = green candle's low.
  - "Fresh liquidity": a second setup on the same side needs a NEW session
    extreme beyond the last signal candle.
  - Daily caps: max 3 trades/day; stop for the day after 2 FULL stop-losses.
  - Skip setups whose stop distance exceeds MAX_SL_USD.
  - Costs: per-bar spread + slippage on the stop entry, one round trip.

EXIT MODES:
  - fixed R:R (2/3/5/10): full exit at target or stop.
  - swing: 80% at nearest opposite swing (fractal), 20% runner to the next
    swing or end of day; if 80% booked, remainder can't be a "full SL".

Read-only. Places no orders. Simulates on downloaded CSV history.
"""
from __future__ import annotations
import sys
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else "."
POINT = 0.01
CONTRACT = 100.0  # oz per lot


@dataclass
class Config:
    exit_mode: str = "rr"          # "rr" or "swing"
    rr: float = 3.0                # target R multiple for fixed mode
    max_sl_usd: float = 8.0        # skip setups with stop distance > this ($)
    slip_pts: float = 2.0          # slippage on stop entry, in points
    extra_spread_pts: float = 0.0  # add to per-bar spread (stress test)
    max_trades_day: int = 3
    max_full_sl_day: int = 2
    swing_k: int = 3               # fractal half-window for swing target
    require_signal_beyond_line: bool = True
    risk_pct: float = 0.01         # for the $ equity curve (fixed fractional)
    start_equity: float = 1000.0


# ----------------------------- data loading -----------------------------

def load_data():
    d1 = pd.read_csv(f"{DATA_DIR}/xauusd_d1.csv", parse_dates=["time"])
    m1 = pd.read_csv(f"{DATA_DIR}/xauusd_m1.csv", parse_dates=["time"])
    d1["date"] = d1["time"].dt.date
    m1["date"] = m1["time"].dt.date
    if "spread" not in m1:
        m1["spread"] = 20
    return d1, m1


def prev_level_map(d1: pd.DataFrame):
    """For each daily date, the previous available day's (high, low)."""
    d1s = d1.sort_values("time").reset_index(drop=True)
    dates = list(d1s["date"])
    highs = list(d1s["high"]); lows = list(d1s["low"])
    out = {}
    for i in range(1, len(dates)):
        out[dates[i]] = (highs[i - 1], lows[i - 1])
    return out


# --------------------------- swing detection ----------------------------

def swing_points(lo, hi, k):
    """Return boolean arrays: is_swing_low, is_swing_high (fractal, +/-k)."""
    n = len(lo)
    sl = np.zeros(n, bool); sh = np.zeros(n, bool)
    for i in range(k, n - k):
        wlo = lo[i - k:i + k + 1]; whi = hi[i - k:i + k + 1]
        if lo[i] == wlo.min() and (wlo.argmin() == k):
            sl[i] = True
        if hi[i] == whi.max() and (whi.argmax() == k):
            sh[i] = True
    return sl, sh


# ------------------------------ simulation ------------------------------

@dataclass
class Trade:
    date: object; side: str; entry_time: object
    entry: float; sl: float; risk_usd: float
    exit_time: object = None; exit: float = None
    pnl_r: float = 0.0; outcome: str = ""; full_sl: bool = False


def simulate_day(day_df: pd.DataFrame, prevH: float, prevL: float, cfg: Config):
    o = day_df["open"].to_numpy(); h = day_df["high"].to_numpy()
    l = day_df["low"].to_numpy();  c = day_df["close"].to_numpy()
    spr = day_df["spread"].to_numpy(); t = day_df["time"].to_list()
    n = len(day_df)
    sl_arr, sh_arr = swing_points(l, h, cfg.swing_k)

    trades: list[Trade] = []
    full_sl_count = 0

    # arming / signal state per side
    armed_s = False; sig_s = None; last_sig_high_s = -np.inf
    armed_l = False; sig_l = None; last_sig_low_l = np.inf
    in_trade = False
    i = 0
    while i < n:
        if len(trades) >= cfg.max_trades_day or full_sl_count >= cfg.max_full_sl_day:
            break
        # ---- update arming from this bar ----
        if h[i] > prevH:
            armed_s = True
        if l[i] < prevL:
            armed_l = True

        # ---- update signal candles (completed bar i) ----
        is_red = c[i] < o[i]
        is_green = c[i] > o[i]
        if armed_s and is_red:
            if (not cfg.require_signal_beyond_line) or (h[i] >= prevH):
                if h[i] > last_sig_high_s - 1e-9:   # fresh: new extreme
                    sig_s = (l[i], h[i], i)
        if armed_l and is_green:
            if (not cfg.require_signal_beyond_line) or (l[i] <= prevL):
                if l[i] < last_sig_low_l + 1e-9:
                    sig_l = (h[i], l[i], i)

        # ---- check entry trigger on the NEXT bar(s): use current bar break ----
        entered = None
        # short: break of active red signal's low
        if sig_s is not None and i > sig_s[2] and l[i] < sig_s[0]:
            sig_low, sig_high, sidx = sig_s
            entry = sig_low - cfg.slip_pts * POINT
            slp = sig_high
            risk = slp - entry
            if 0 < risk <= cfg.max_sl_usd:
                entered = ("short", entry, slp, risk, sidx)
            last_sig_high_s = sig_high; sig_s = None
        # long: break of active green signal's high
        if entered is None and sig_l is not None and i > sig_l[2] and h[i] > sig_l[0]:
            sig_high, sig_low, sidx = sig_l
            entry = sig_high + cfg.slip_pts * POINT
            slp = sig_low
            risk = entry - slp
            if 0 < risk <= cfg.max_sl_usd:
                entered = ("long", entry, slp, risk, sidx)
            last_sig_low_l = sig_low; sig_l = None

        if entered is None:
            i += 1
            continue

        side, entry, slp, risk, sidx = entered
        spread_cost = (spr[i] + cfg.extra_spread_pts) * POINT
        cost = spread_cost + cfg.slip_pts * POINT  # round-trip approx

        # ---------- manage the trade forward ----------
        tr = Trade(date=day_df["date"].iloc[0], side=side, entry_time=t[i],
                   entry=entry, sl=slp, risk_usd=risk)

        if cfg.exit_mode == "rr":
            tp = entry - cfg.rr * risk if side == "short" else entry + cfg.rr * risk
            j = i + 1
            done = False
            while j < n:
                if side == "short":
                    hit_sl = h[j] >= slp
                    hit_tp = l[j] <= tp
                else:
                    hit_sl = l[j] <= slp
                    hit_tp = h[j] >= tp
                if hit_sl and hit_tp:      # ambiguous bar -> assume SL first
                    hit_tp = False
                if hit_sl:
                    gross = (entry - slp) if side == "short" else (slp - entry)
                    tr.exit = slp; tr.exit_time = t[j]
                    tr.pnl_r = (gross - cost) / risk
                    tr.outcome = "SL"; tr.full_sl = True
                    done = True; break
                if hit_tp:
                    gross = (entry - tp) if side == "short" else (tp - entry)
                    tr.exit = tp; tr.exit_time = t[j]
                    tr.pnl_r = (gross - cost) / risk
                    tr.outcome = "TP"
                    done = True; break
                j += 1
            if not done:  # close at last bar
                px = c[n - 1]
                gross = (entry - px) if side == "short" else (px - entry)
                tr.exit = px; tr.exit_time = t[n - 1]
                tr.pnl_r = (gross - cost) / risk
                tr.outcome = "EOD"; tr.full_sl = tr.pnl_r <= -0.99
            i = max(i + 1, sidx + 1)

        else:  # ------------- swing mode: 80% at TP1, 20% runner -------------
            # TP1 = nearest opposite swing beyond entry
            if side == "short":
                cand = [l[x] for x in range(sidx + 1) if sl_arr[x] and l[x] < entry]
                tp1 = max(cand) if cand else entry - 3 * risk
                cand2 = [v for v in cand if v < tp1]
                tp2 = max(cand2) if cand2 else entry - 6 * risk
            else:
                cand = [h[x] for x in range(sidx + 1) if sh_arr[x] and h[x] > entry]
                tp1 = min(cand) if cand else entry + 3 * risk
                cand2 = [v for v in cand if v > tp1]
                tp2 = min(cand2) if cand2 else entry + 6 * risk

            filled1 = False; be = False; j = i + 1
            r_realized = 0.0; done = False
            while j < n:
                if side == "short":
                    hit_sl = h[j] >= slp
                    hit_tp1 = l[j] <= tp1
                    hit_tp2 = l[j] <= tp2
                else:
                    hit_sl = l[j] <= slp
                    hit_tp1 = h[j] >= tp1
                    hit_tp2 = h[j] >= tp2
                if not filled1:
                    if hit_sl and hit_tp1:
                        hit_tp1 = False
                    if hit_sl:
                        gross = (entry - slp) if side == "short" else (slp - entry)
                        r_realized += (gross - cost) / risk
                        tr.exit = slp; tr.exit_time = t[j]; tr.outcome = "SL"
                        tr.full_sl = True; done = True; break
                    if hit_tp1:
                        g = (entry - tp1) if side == "short" else (tp1 - entry)
                        r_realized += 0.8 * ((g - cost) / risk)
                        filled1 = True; be = True; slp = entry  # runner to BE
                        tr.outcome = "TP1"
                else:
                    if side == "short":
                        hit_be = h[j] >= slp
                    else:
                        hit_be = l[j] <= slp
                    if hit_tp2:
                        g = (entry - tp2) if side == "short" else (tp2 - entry)
                        r_realized += 0.2 * ((g - cost) / risk)
                        tr.exit = tp2; tr.exit_time = t[j]; tr.outcome = "TP2"
                        done = True; break
                    if hit_be:
                        g = (entry - slp) if side == "short" else (slp - entry)
                        r_realized += 0.2 * ((g - cost) / risk)
                        tr.exit = slp; tr.exit_time = t[j]
                        tr.outcome = tr.outcome + "+BE"; done = True; break
                j += 1
            if not done:
                px = c[n - 1]
                g = (entry - px) if side == "short" else (px - entry)
                frac = 0.2 if filled1 else 1.0
                r_realized += frac * ((g - cost) / risk)
                tr.exit = px; tr.exit_time = t[n - 1]
                tr.outcome = (tr.outcome + "+EOD") if filled1 else "EOD"
            tr.pnl_r = r_realized
            i = max(i + 1, sidx + 1)

        trades.append(tr)
        if tr.full_sl:
            full_sl_count += 1
        # continue scanning same day for more setups (fresh liquidity gated)
    return trades


def run(cfg: Config, d1, m1):
    plevels = prev_level_map(d1)
    all_trades: list[Trade] = []
    for date, day_df in m1.groupby("date"):
        if date not in plevels:
            continue
        day_df = day_df.sort_values("time").reset_index(drop=True)
        if len(day_df) < 30:
            continue
        prevH, prevL = plevels[date]
        all_trades.extend(simulate_day(day_df, prevH, prevL, cfg))
    return all_trades


# ------------------------------ reporting -------------------------------

def metrics(trades: list[Trade]):
    if not trades:
        return dict(n=0)
    r = np.array([t.pnl_r for t in trades])
    wins = r[r > 0]; losses = r[r <= 0]
    n = len(r)
    return dict(
        n=n,
        win_rate=len(wins) / n,
        avg_r=r.mean(),
        median_r=float(np.median(r)),
        total_r=r.sum(),
        expectancy_r=r.mean(),
        best=r.max(), worst=r.min(),
        avg_win=wins.mean() if len(wins) else 0.0,
        avg_loss=losses.mean() if len(losses) else 0.0,
        profit_factor=(wins.sum() / -losses.sum()) if losses.sum() < 0 else np.inf,
    )


def equity_curve(trades, cfg: Config):
    eq = cfg.start_equity; curve = [eq]; peak = eq; maxdd = 0.0
    for t in trades:
        eq = eq + t.pnl_r * cfg.risk_pct * eq
        curve.append(eq)
        peak = max(peak, eq)
        maxdd = max(maxdd, (peak - eq) / peak)
    return curve, maxdd


def split_is_oos(trades, frac=0.6):
    if not trades:
        return [], []
    trades = sorted(trades, key=lambda x: x.entry_time)
    k = int(len(trades) * frac)
    return trades[:k], trades[k:]


def fmt(m):
    if m.get("n", 0) == 0:
        return "no trades"
    return (f"n={m['n']:3d}  win={m['win_rate']*100:4.1f}%  "
            f"avgR={m['avg_r']:+.3f}  totR={m['total_r']:+6.1f}  "
            f"PF={m['profit_factor']:.2f}  "
            f"avgW={m['avg_win']:+.2f} avgL={m['avg_loss']:+.2f}  "
            f"worst={m['worst']:+.1f}")


def main():
    d1, m1 = load_data()
    print(f"M1 span: {m1['time'].min()} -> {m1['time'].max()}  "
          f"({m1['date'].nunique()} days, {len(m1)} bars)\n")

    print("=" * 96)
    print("FIXED R:R MODE  (max_sl<=$8, slip 2pts, per-bar spread, caps 3/2)")
    print("=" * 96)
    best_curve = None
    for rr in (2, 3, 5, 10):
        cfg = Config(exit_mode="rr", rr=rr)
        tr = run(cfg, d1, m1)
        m = metrics(tr)
        is_, oos = split_is_oos(tr)
        curve, dd = equity_curve(tr, cfg)
        print(f"\nRR 1:{rr:<2}  ALL   {fmt(m)}")
        print(f"          IS(60%) {fmt(metrics(is_))}")
        print(f"          OOS(40%){fmt(metrics(oos))}")
        print(f"          $curve: ${curve[0]:.0f}->${curve[-1]:.0f}  maxDD={dd*100:.1f}%")
        if rr == 3:
            best_curve = (f"rr3", tr, curve)

    print("\n" + "=" * 96)
    print("SWING TARGET MODE  (80% at nearest swing, 20% runner to next swing/EOD)")
    print("=" * 96)
    cfg = Config(exit_mode="swing")
    tr = run(cfg, d1, m1)
    m = metrics(tr); is_, oos = split_is_oos(tr); curve, dd = equity_curve(tr, cfg)
    print(f"\nSWING ALL   {fmt(m)}")
    print(f"      IS(60%) {fmt(metrics(is_))}")
    print(f"      OOS(40%){fmt(metrics(oos))}")
    print(f"      $curve: ${curve[0]:.0f}->${curve[-1]:.0f}  maxDD={dd*100:.1f}%")

    # sensitivity to costs on the RR=3 case
    print("\n" + "=" * 96)
    print("COST SENSITIVITY (RR 1:3): extra spread points added")
    print("=" * 96)
    for extra in (0, 10, 20, 40):
        cfg = Config(exit_mode="rr", rr=3, extra_spread_pts=extra)
        tr = run(cfg, d1, m1); m = metrics(tr)
        print(f"  +{extra:2d}pts spread  {fmt(m)}")

    # save the swing + rr3 trade blotters and an equity curve for inspection
    cfg = Config(exit_mode="rr", rr=3)
    tr = run(cfg, d1, m1)
    df = pd.DataFrame([t.__dict__ for t in tr])
    df.to_csv(f"{DATA_DIR}/trades_rr3.csv", index=False)
    curve, _ = equity_curve(tr, cfg)
    pd.DataFrame({"equity": curve}).to_csv(f"{DATA_DIR}/equity_rr3.csv", index=False)
    print(f"\nSaved {len(tr)} RR3 trades -> trades_rr3.csv, equity_rr3.csv")


if __name__ == "__main__":
    main()
