"""
Shared trailing-exit logic for the GCP 4-target ladder (rule A).

Pure functions, no I/O and no MT5, imported by BOTH gcp_backtest.py and
gcp_live.py so the historical simulation and the live bot manage a position
identically (same single-source-of-truth pattern as gcp_engine.py).

Levels (indicator-exact):  SL = entry -/+ atr_mult*ATR ; TP1..TP4 = entry +/- 1/2/3/4*ATR.
Rule A trailing:           TP1 reached -> stop to breakeven ; TP2 -> TP1 ; TP3 -> TP2 ;
                           TP4 reached -> caller closes the whole position (+2R).
"""
from __future__ import annotations


def compute_levels(side: str, entry: float, atr: float, atr_mult: float = 2.0):
    """Return (sl, [tp1, tp2, tp3, tp4]) for a BUY/SELL entry."""
    if side == "BUY":
        sl = entry - atr_mult * atr
        tps = [entry + atr * k for k in (1, 2, 3, 4)]
    else:
        sl = entry + atr_mult * atr
        tps = [entry - atr * k for k in (1, 2, 3, 4)]
    return sl, tps


def targets_reached(side: str, extreme: float, tps) -> int:
    """How many of TP1..TP4 have been reached at price `extreme`.

    Pass the favourable running extreme: HIGH for a BUY, LOW for a SELL."""
    if side == "BUY":
        return sum(1 for tp in tps if extreme >= tp)
    return sum(1 for tp in tps if extreme <= tp)


# A trail SCHEDULE maps "number of targets reached" -> stop level name.
# Level names: "BE" (breakeven/entry), "TP1", "TP2", "TP3". Below the lowest
# key the stop stays at the original SL. Rule A = BE after TP1, then one behind.
RULE_A = {1: "BE", 2: "TP1", 3: "TP2"}


def next_stop_sched(side: str, entry: float, sl0: float, tps, n_hit: int, schedule) -> float:
    """Stop price for `n_hit` targets reached, under an arbitrary trail schedule.

    Sticky and monotonic: uses the level for the largest schedule key <= n_hit,
    falling back to the original SL below the lowest key."""
    level = "SL0"
    for k in sorted(schedule):
        if n_hit >= k:
            level = schedule[k]
    price = {"SL0": sl0, "BE": entry, "TP1": tps[0], "TP2": tps[1], "TP3": tps[2]}
    return price[level]


def next_stop(side: str, entry: float, sl0: float, tps, n_hit: int) -> float:
    """Rule A stop for `n_hit` targets reached (0->SL, 1->BE, 2->TP1, 3->TP2)."""
    return next_stop_sched(side, entry, sl0, tps, n_hit, RULE_A)
