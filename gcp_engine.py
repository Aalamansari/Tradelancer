"""
Shared signal engine for the "GrowthClubPK Trend Sniper" Pine v6 indicator.

Faithful Python port of the indicator's tradeable logic, evaluated on CLOSED
bars (no repaint, no look-ahead). This is the SINGLE SOURCE OF TRUTH imported by
both the backtester (gcp_backtest.py) and the live session bot (gcp_live.py), so
the two can never drift apart.

Pure functions of a price DataFrame -- computes indicators/signals only. Places
no orders and does no I/O.

FAITHFUL SIGNAL ENGINE (exact translation of the Pine source)
  EMA ribbon  : lengths 20,24,28,...,56 (start 20, step 4) -> e1..e10
  ribbon_bull : e1>e2>e3>e4>e5>e10      (Pine skips e6..e9, so do we)
  MACD(12,26,9) hist sign
  StochRSI(14,3,3) %K / smooth crosses
  Bollinger(20,2) breakouts
  ATR(14); ema_strength=|e1-e10|/atr>0.5 ; vol_ok=atr>sma(atr,50)
  manual ADX(14)>18 ; confirm candle
  setups A(trend) / B(pullback) / C(squeeze) OR'd -> raw_buy / raw_sell
  (the 8-bar cooldown is applied by the consumer's trade loop, not here)
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ----------------------------- indicators -------------------------------

def ema(s: pd.Series, n: int) -> pd.Series:
    # Pine ta.ema: recursive EMA seeded with first value -> ewm(adjust=False)
    return s.ewm(alpha=2.0 / (n + 1.0), adjust=False).mean()


def rma(s: pd.Series, n: int) -> pd.Series:
    # Pine ta.rma / Wilder smoothing
    return s.ewm(alpha=1.0 / n, adjust=False).mean()


def rsi(s: pd.Series, n: int) -> pd.Series:
    d = s.diff()
    up = d.clip(lower=0.0)
    dn = (-d).clip(lower=0.0)
    rs = rma(up, n) / rma(dn, n).replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    return out.fillna(100.0)


def crossover(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a > b) & (a.shift(1) <= b.shift(1))


def crossunder(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a < b) & (a.shift(1) >= b.shift(1))


def build_indicators(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    src = c

    # --- EMA ribbon (start 20, step 4) ---
    lens = [20 + 4 * k for k in range(10)]            # 20,24,...,56
    e = {i + 1: ema(src, lens[i]) for i in range(10)}
    d["e1"], d["e3"], d["e5"], d["e10"] = e[1], e[3], e[5], e[10]
    d["ribbon_bull"] = (e[1] > e[2]) & (e[2] > e[3]) & (e[3] > e[4]) & (e[4] > e[5]) & (e[5] > e[10])
    d["ribbon_bear"] = (e[1] < e[2]) & (e[2] < e[3]) & (e[3] < e[4]) & (e[4] < e[5]) & (e[5] < e[10])

    # --- MACD(12,26,9) ---
    macd = ema(src, 12) - ema(src, 26)
    hist = macd - ema(macd, 9)
    d["macd_bull"] = hist > 0
    d["macd_bear"] = hist < 0

    # --- Stoch RSI(14,3,3) ---
    r = rsi(src, 14)
    lo = r.rolling(14).min()
    hi = r.rolling(14).max()
    stoch = 100.0 * (r - lo) / (hi - lo).clip(lower=1.0)
    k = stoch.rolling(3).mean()
    smooth = k.rolling(3).mean()
    d["stoch_k"] = k
    d["stoch_bull_cross"] = crossover(k, smooth)
    d["stoch_bear_cross"] = crossunder(k, smooth)

    # --- Bollinger(20,2) ---
    basis = src.rolling(20).mean()
    dev = 2.0 * src.rolling(20).std(ddof=0)
    d["bb_bull"] = crossover(src, basis + dev)
    d["bb_bear"] = crossunder(src, basis - dev)

    # --- ATR(14) + strength / volatility filters ---
    pc = c.shift(1)
    tr = pd.concat([(h - l), (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = rma(tr, 14)
    d["atr"] = atr
    d["trend_ok"] = (e[1] - e[10]).abs() / atr > 0.5
    d["vol_ok"] = atr > atr.rolling(50).mean()

    # --- manual ADX(14) (exact translation) ---
    up_move = h - h.shift(1)
    dn_move = l.shift(1) - l
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    trur = rma(tr, 14)
    plus_di = 100.0 * rma(pd.Series(plus_dm, index=d.index), 14) / trur
    minus_di = 100.0 * rma(pd.Series(minus_dm, index=d.index), 14) / trur
    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).clip(lower=1.0)
    d["adx"] = rma(dx, 14)
    d["adx_ok"] = d["adx"] > 18.0

    # --- confirmation candles ---
    d["confirm_bull"] = (c > o) & (c > c.shift(1))
    d["confirm_bear"] = (c < o) & (c < c.shift(1))

    # --- ribbon price cross ---
    d["cross_up"] = crossover(src, e[1])
    d["cross_dn"] = crossunder(src, e[1])

    # --- setups A/B/C ---
    d["A_buy"] = d["ribbon_bull"] & d["macd_bull"] & d["cross_up"]
    d["A_sell"] = d["ribbon_bear"] & d["macd_bear"] & d["cross_dn"]
    d["B_buy"] = d["ribbon_bull"] & (l <= e[5]) & (c > e[3]) & d["stoch_bull_cross"] & (k < 40)
    d["B_sell"] = d["ribbon_bear"] & (h >= e[5]) & (c < e[3]) & d["stoch_bear_cross"] & (k > 60)
    d["C_buy"] = d["bb_bull"] & (c > e[1]) & d["macd_bull"]
    d["C_sell"] = d["bb_bear"] & (c < e[1]) & d["macd_bear"]

    d["raw_buy"] = d["A_buy"] | d["B_buy"] | d["C_buy"]
    d["raw_sell"] = d["A_sell"] | d["B_sell"] | d["C_sell"]

    # filter mask shared by both sides (cooldown applied in the loop)
    d["filt"] = d["trend_ok"] & d["vol_ok"] & d["adx_ok"]
    return d
