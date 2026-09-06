"""
Backtest of the "GrowthClubPK Trend Sniper" Pine v6 indicator on XAUUSD M5.

The indicator itself computes NO performance stats -- it only draws signals,
lines/boxes and a dashboard. This script mechanizes its *tradeable* logic
faithfully on CLOSED bars (no repaint, no look-ahead) and imposes an R model
plus realistic costs so we can actually measure it.

FAITHFUL SIGNAL ENGINE (exact translation of the Pine source)
  EMA ribbon  : lengths 20,24,28,...,56 (start 20, step 4) -> e1..e10
  ribbon_bull : e1>e2>e3>e4>e5>e10      (Pine skips e6..e9, so do we)
  MACD(12,26,9) hist sign
  StochRSI(14,3,3) %K / smooth crosses
  Bollinger(20,2) breakouts
  ATR(14); ema_strength=|e1-e10|/atr>0.5 ; vol_ok=atr>sma(atr,50)
  manual ADX(14)>18 ; confirm candle ; 8-bar cooldown
  setups A(trend) / B(pullback) / C(squeeze) OR'd -> buy_signal / sell_signal

TRADE MODEL
  Enter at signal-bar close. risk = 2*ATR (SL = +/-2*ATR = 1R).
  Targets +/-1/2/3/4*ATR  => TP4 = +2R.
  Exit on SL(-1R), TP4(+2R), or an opposite signal (mark-to-close), exactly
  as the indicator's exit-monitor freezes the drawing.

  mode="full"   : one position, exit at SL / TP4 / opposite  (what the code does)
  mode="scaled" : book 25% at each of TP1..TP4, move to break-even after TP1,
                  remainder exits at BE / TP4 / opposite      (how it's marketed)

COSTS  : per-bar spread (from the feed) + slippage each side, round trip,
         with a stress sweep, because the demo spread is optimistic.

Read-only. Places no orders. Simulates on downloaded CSV history.
"""
from __future__ import annotations
import sys
from dataclasses import dataclass, field
import numpy as np
import pandas as pd

from gcp_engine import build_indicators   # single source of truth for the signal engine
from gcp_trailing import compute_levels, targets_reached, next_stop_sched, RULE_A  # shared trail ladder

DATA_DIR = sys.argv[1] if len(sys.argv) > 1 else "."
POINT = 0.01
CONTRACT = 100.0


# The indicator engine (ema/rma/rsi/crossover/crossunder/build_indicators) now
# lives in gcp_engine.py so the live bot and this backtest share identical logic.


# ------------------------------ simulation ------------------------------

@dataclass
class Cfg:
    mode: str = "full"          # "full" or "scaled"
    slip_pts: float = 2.0       # slippage per side, points
    extra_spread_pts: float = 0.0
    use_data_spread: bool = True
    fixed_spread_pts: float = 30.0   # used when use_data_spread=False
    tp_first: bool = False      # tie-break on a bar touching both SL and TP
    cooldown: int = 8
    risk_pct: float = 0.01
    start_equity: float = 1000.0
    point: float = 0.01         # price value of 1 "point" (gold 0.01; 5-digit FX 1e-05)
    trail_schedule: dict = field(default_factory=lambda: dict(RULE_A))  # mode="trail" only
    trail_on_close: bool = False   # trail: advance stop on bar CLOSE (strict) vs HIGH/LOW touch


@dataclass
class Trade:
    dir: str; entry_time: object; entry: float; sl: float; risk: float
    exit_time: object = None; exit: float = None
    r: float = 0.0; outcome: str = ""; full_sl: bool = False


def _cost_usd(spread_pts_bar, cfg: Cfg):
    spr = (spread_pts_bar if cfg.use_data_spread else cfg.fixed_spread_pts)
    spr = spr + cfg.extra_spread_pts
    return spr * cfg.point + 2.0 * cfg.slip_pts * cfg.point   # 1 spread crossing + slip both sides


def simulate(d: pd.DataFrame, cfg: Cfg):
    o = d["open"].to_numpy(); h = d["high"].to_numpy()
    l = d["low"].to_numpy();  c = d["close"].to_numpy()
    atr = d["atr"].to_numpy(); spr = d["spread"].to_numpy()
    t = d["time"].to_list()
    raw_buy = d["raw_buy"].to_numpy(); raw_sell = d["raw_sell"].to_numpy()
    filt = d["filt"].to_numpy()
    cb = d["confirm_bull"].to_numpy(); cs = d["confirm_bear"].to_numpy()
    valid = d["atr"].notna().to_numpy() & d["vol_ok"].notna().to_numpy()
    n = len(d)

    trades: list[Trade] = []
    active = "None"
    entry = sl = tp4 = risk = 0.0
    tps = [0.0, 0.0, 0.0, 0.0]
    booked = 0            # scaled: number of TPs booked
    r_acc = 0.0           # scaled: realized R so far (net of proportional cost)
    cur_sl = 0.0          # scaled/trail: working stop (ratchets up)
    run_high = run_low = 0.0   # trail: favourable running extreme since entry
    cost_at_entry = 0.0
    last_trade_bar = -10 ** 9
    cur: Trade | None = None

    def open_trade(i, direction):
        nonlocal active, entry, sl, tp4, risk, tps, booked, r_acc, cur_sl
        nonlocal last_trade_bar, cur, cost_at_entry, run_high, run_low
        entry = c[i]
        a = atr[i]
        side = "BUY" if direction == "Long" else "SELL"
        sl, tps[:] = compute_levels(side, entry, a)   # shared: SL=2ATR, TP1..4=1/2/3/4 ATR
        tp4 = tps[3]
        risk = 2 * a
        cur_sl = sl
        run_high = run_low = entry
        booked = 0
        r_acc = 0.0
        cost_at_entry = _cost_usd(spr[i], cfg)
        active = direction
        last_trade_bar = i
        cur = Trade(dir=direction, entry_time=t[i], entry=entry, sl=sl, risk=risk)

    def close_trade(i, px, outcome, extra_r=0.0, full_sl=False):
        nonlocal active, cur
        gross = (px - entry) if active == "Long" else (entry - px)
        r = extra_r + (gross - cost_at_entry) / risk if cfg.mode != "scaled" else extra_r
        cur.exit = px; cur.exit_time = t[i]; cur.outcome = outcome
        cur.r = r; cur.full_sl = full_sl
        trades.append(cur)
        active = "None"

    for i in range(n):
        if not valid[i]:
            continue
        cooldown_ok = (i - last_trade_bar) > cfg.cooldown
        buy_sig = bool(raw_buy[i] and filt[i] and cb[i] and cooldown_ok)
        sell_sig = bool(raw_sell[i] and filt[i] and cs[i] and cooldown_ok)

        # ---------------- EXIT MONITOR (trade active from prior bars) --------
        if active == "Long":
            hit_sl = l[i] <= cur_sl
            hit_tp4 = h[i] >= tp4
            if cfg.mode == "full":
                if hit_sl and hit_tp4 and not cfg.tp_first:
                    hit_tp4 = False
                if hit_sl:
                    close_trade(i, cur_sl, "SL", full_sl=(cur_sl == sl));
                elif hit_tp4:
                    close_trade(i, tp4, "TP4")
                elif sell_sig:
                    close_trade(i, c[i], "REV")
            elif cfg.mode == "trail":
                if hit_sl and hit_tp4 and not cfg.tp_first:
                    hit_tp4 = False
                if hit_sl:
                    outc = "SL" if cur_sl == sl else "BE" if cur_sl == entry else "TRAIL"
                    close_trade(i, cur_sl, outc, full_sl=(cur_sl == sl))
                elif hit_tp4:
                    close_trade(i, tp4, "TP4")
                elif sell_sig:
                    close_trade(i, c[i], "REV")
                else:  # no exit -> ratchet stop for FUTURE bars (conservative)
                    run_high = max(run_high, c[i] if cfg.trail_on_close else h[i])
                    n_hit = targets_reached("BUY", run_high, tps)
                    cur_sl = max(cur_sl, next_stop_sched("BUY", entry, sl, tps, n_hit, cfg.trail_schedule))
            else:  # scaled
                # book intermediate TPs in order (each 25%)
                while booked < 4 and h[i] >= tps[booked]:
                    r_acc += 0.25 * ((tps[booked] - entry) - cost_at_entry * 0.25) / risk
                    booked += 1
                    if booked == 1:
                        cur_sl = entry  # move to break-even after TP1
                if booked >= 4:
                    close_trade(i, tp4, "TP4x", extra_r=r_acc)
                elif l[i] <= cur_sl:
                    frac = (4 - booked) * 0.25
                    r_acc += frac * (((cur_sl - entry)) - cost_at_entry * frac) / risk
                    close_trade(i, cur_sl, "SL" if booked == 0 else "BE",
                                extra_r=r_acc, full_sl=(booked == 0 and cur_sl == sl))
                elif sell_sig:
                    frac = (4 - booked) * 0.25
                    r_acc += frac * (((c[i] - entry)) - cost_at_entry * frac) / risk
                    close_trade(i, c[i], "REV", extra_r=r_acc)

        elif active == "Short":
            hit_sl = h[i] >= cur_sl
            hit_tp4 = l[i] <= tp4
            if cfg.mode == "full":
                if hit_sl and hit_tp4 and not cfg.tp_first:
                    hit_tp4 = False
                if hit_sl:
                    close_trade(i, cur_sl, "SL", full_sl=(cur_sl == sl))
                elif hit_tp4:
                    close_trade(i, tp4, "TP4")
                elif buy_sig:
                    close_trade(i, c[i], "REV")
            elif cfg.mode == "trail":
                if hit_sl and hit_tp4 and not cfg.tp_first:
                    hit_tp4 = False
                if hit_sl:
                    outc = "SL" if cur_sl == sl else "BE" if cur_sl == entry else "TRAIL"
                    close_trade(i, cur_sl, outc, full_sl=(cur_sl == sl))
                elif hit_tp4:
                    close_trade(i, tp4, "TP4")
                elif buy_sig:
                    close_trade(i, c[i], "REV")
                else:  # no exit -> ratchet stop for FUTURE bars (conservative)
                    run_low = min(run_low, c[i] if cfg.trail_on_close else l[i])
                    n_hit = targets_reached("SELL", run_low, tps)
                    cur_sl = min(cur_sl, next_stop_sched("SELL", entry, sl, tps, n_hit, cfg.trail_schedule))
            else:
                while booked < 4 and l[i] <= tps[booked]:
                    r_acc += 0.25 * ((entry - tps[booked]) - cost_at_entry * 0.25) / risk
                    booked += 1
                    if booked == 1:
                        cur_sl = entry
                if booked >= 4:
                    close_trade(i, tp4, "TP4x", extra_r=r_acc)
                elif h[i] >= cur_sl:
                    frac = (4 - booked) * 0.25
                    r_acc += frac * (((entry - cur_sl)) - cost_at_entry * frac) / risk
                    close_trade(i, cur_sl, "SL" if booked == 0 else "BE",
                                extra_r=r_acc, full_sl=(booked == 0 and cur_sl == sl))
                elif buy_sig:
                    frac = (4 - booked) * 0.25
                    r_acc += frac * (((entry - c[i])) - cost_at_entry * frac) / risk
                    close_trade(i, c[i], "REV", extra_r=r_acc)

        # ---------------- ENTRIES (after exit, same bar; Long then Short) ----
        if buy_sig and active != "Long":
            open_trade(i, "Long")
        elif sell_sig and active != "Short":
            open_trade(i, "Short")

    # close any runner at last bar
    if active != "None" and cur is not None:
        i = n - 1
        if cfg.mode != "scaled":
            close_trade(i, c[i], "EOD", full_sl=False)
        else:
            frac = (4 - booked) * 0.25
            gross = (c[i] - entry) if active == "Long" else (entry - c[i])
            r_acc += frac * (gross - cost_at_entry * frac) / risk
            close_trade(i, c[i], "EOD", extra_r=r_acc)
    return trades


# ------------------------------ reporting -------------------------------

def metrics(trades: list[Trade]):
    if not trades:
        return dict(n=0)
    r = np.array([x.r for x in trades])
    wins = r[r > 0]; losses = r[r <= 0]
    n = len(r)
    return dict(n=n, win=len(wins) / n, avg_r=r.mean(), tot_r=r.sum(),
                pf=(wins.sum() / -losses.sum()) if losses.sum() < 0 else np.inf,
                avg_w=wins.mean() if len(wins) else 0.0,
                avg_l=losses.mean() if len(losses) else 0.0,
                best=r.max(), worst=r.min())


def equity(trades, cfg: Cfg):
    eq = cfg.start_equity; curve = [eq]; peak = eq; dd = 0.0
    for x in trades:
        eq += x.r * cfg.risk_pct * eq
        curve.append(eq); peak = max(peak, eq); dd = max(dd, (peak - eq) / peak)
    return curve, dd


def split(trades, frac=0.6):
    trades = sorted(trades, key=lambda x: x.entry_time)
    k = int(len(trades) * frac)
    return trades[:k], trades[k:]


def fmt(m):
    if m.get("n", 0) == 0:
        return "no trades"
    return (f"n={m['n']:4d}  win={m['win']*100:4.1f}%  avgR={m['avg_r']:+.3f}  "
            f"totR={m['tot_r']:+7.1f}  PF={m['pf']:.2f}  "
            f"avgW={m['avg_w']:+.2f} avgL={m['avg_l']:+.2f}  worst={m['worst']:+.1f}")


def main():
    df = pd.read_csv(f"{DATA_DIR}/xauusd_m5.csv", parse_dates=["time"])
    if "spread" not in df:
        df["spread"] = 30
    d = build_indicators(df)
    span_days = (d["time"].max() - d["time"].min()).days
    print(f"XAUUSD M5 : {len(d)} bars  {d['time'].min()} -> {d['time'].max()}  (~{span_days} days)")
    print(f"raw_buy fires={int(d['raw_buy'].sum())}  raw_sell fires={int(d['raw_sell'].sum())}  "
          f"filt-pass bars={int(d['filt'].sum())}  median ATR=${d['atr'].median():.2f}\n")

    desc = {
        "full": "single position, exit SL/TP4/opposite",
        "scaled": "partials 25% each TP, BE after TP1",
        "trail": "full size, stop ratchets BE->TP1->TP2 on TP1/2/3, exit trailed-stop/TP4/opposite",
    }
    for mode in ("full", "scaled", "trail"):
        print("=" * 100)
        print(f"MODE = {mode.upper()}   (enter@close, SL=2ATR=1R, TP4=+2R; {desc[mode]})")
        print("=" * 100)
        cfg = Cfg(mode=mode)
        tr = simulate(d, cfg)
        m = metrics(tr); is_, oos = split(tr); cv, dd = equity(tr, cfg)
        print(f"  ALL      {fmt(m)}")
        print(f"  IS(60%)  {fmt(metrics(is_))}")
        print(f"  OOS(40%) {fmt(metrics(oos))}")
        print(f"  $curve @1% risk: ${cv[0]:.0f} -> ${cv[-1]:.0f}   maxDD={dd*100:.1f}%")
        # cost sensitivity
        print("  cost sensitivity (extra spread pts on top of feed):")
        for extra in (0, 20, 40):
            c2 = Cfg(mode=mode, extra_spread_pts=extra)
            print(f"     +{extra:2d}pts  {fmt(metrics(simulate(d, c2)))}")
        print(f"     realistic retail (fixed $0.30 rt, slip 3pts): "
              f"{fmt(metrics(simulate(d, Cfg(mode=mode, use_data_spread=False, fixed_spread_pts=24, slip_pts=3))))}")
        print(f"     optimistic (no cost, TP-first ties): "
              f"{fmt(metrics(simulate(d, Cfg(mode=mode, use_data_spread=False, fixed_spread_pts=0, slip_pts=0, tp_first=True))))}")
        print()

    # save faithful (full) blotter + equity for inspection
    cfg = Cfg(mode="full")
    tr = simulate(d, cfg)
    blot = pd.DataFrame([x.__dict__ for x in tr])
    blot.to_csv(f"{DATA_DIR}/gcp_trades_full.csv", index=False)
    cv, _ = equity(tr, cfg)
    pd.DataFrame({"equity": cv}).to_csv(f"{DATA_DIR}/gcp_equity_full.csv", index=False)
    print(f"Saved {len(tr)} full-mode trades -> gcp_trades_full.csv, gcp_equity_full.csv")

    # ---- verification: setup mix + first sample trades ----
    if len(blot):
        print("\nSAMPLE TRADES (full mode, first 8):")
        print(blot[["dir", "entry_time", "entry", "sl", "exit_time", "exit", "outcome", "r"]]
              .head(8).to_string(index=False))
        wins = (blot["r"] > 0).mean()
        print(f"\navg trades/month = {len(blot)/ (span_days/30.0):.1f} ; win% = {wins*100:.1f}")


if __name__ == "__main__":
    main()
