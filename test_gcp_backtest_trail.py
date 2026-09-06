"""
Tests for mode='trail' in gcp_backtest.simulate (rule A: BE, then one target behind).
Synthetic OHLC paths, zero cost, so the R outcome is exact. No MT5, no CSV.
Run:  python test_gcp_backtest_trail.py   (or pytest)
"""
import pandas as pd
import gcp_backtest as bt

NOCOST = dict(use_data_spread=False, fixed_spread_pts=0.0, slip_pts=0.0)


def _df(specs, atr=2.0):
    """Build an indicatored-shape frame from a list of per-bar dicts."""
    base = dict(raw_buy=False, raw_sell=False, filt=True, confirm_bull=False,
                confirm_bear=False, vol_ok=True, spread=0.0)
    rows = []
    for i, s in enumerate(specs):
        r = dict(base)
        r.update(s)
        for short, full in (("o", "open"), ("h", "high"), ("l", "low"), ("c", "close")):
            r[full] = r.pop(short)
        r["atr"] = atr
        r["time"] = pd.Timestamp("2026-09-06 10:00") + pd.Timedelta(minutes=5 * i)
        rows.append(r)
    return pd.DataFrame(rows)


# entry long @100, atr 2 -> sl0=96, tp1..4 = 102/104/106/108, risk=4
def test_trail_locks_profit_on_reversal():
    df = _df([
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100, "raw_buy": True, "confirm_bull": True},  # ENTRY
        {"o": 100, "h": 105, "l": 100, "c": 104},   # reaches TP2 -> stop ratchets to TP1 (102)
        {"o": 103, "h": 103, "l": 101, "c": 101},   # dips to 101 <= 102 -> TRAIL exit @102
    ])
    tr = bt.simulate(df, bt.Cfg(mode="trail", **NOCOST))
    assert len(tr) == 1
    assert tr[0].dir == "Long"
    assert tr[0].outcome == "TRAIL"
    assert tr[0].exit == 102.0
    assert abs(tr[0].r - 0.5) < 1e-9        # +0.5R locked


def test_trail_full_loss_before_any_target():
    df = _df([
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100, "raw_buy": True, "confirm_bull": True},  # ENTRY
        {"o": 100, "h": 100, "l": 95, "c": 97},     # low 95 <= 96 (orig SL) -> full loss
    ])
    tr = bt.simulate(df, bt.Cfg(mode="trail", **NOCOST))
    assert len(tr) == 1
    assert tr[0].outcome == "SL"
    assert tr[0].full_sl                    # original-SL loss
    assert tr[0].exit == 96.0
    assert abs(tr[0].r + 1.0) < 1e-9        # -1R


def test_trail_breakeven_after_tp1():
    df = _df([
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100, "raw_buy": True, "confirm_bull": True},  # ENTRY
        {"o": 100, "h": 102, "l": 100, "c": 101},   # tags TP1 -> stop to breakeven (100)
        {"o": 100, "h": 100, "l": 99, "c": 99},     # dips to 99 <= 100 -> BE exit @100
    ])
    tr = bt.simulate(df, bt.Cfg(mode="trail", **NOCOST))
    assert len(tr) == 1
    assert tr[0].outcome == "BE"
    assert tr[0].exit == 100.0
    assert abs(tr[0].r - 0.0) < 1e-9        # scratch


def test_trail_tp4_full_target():
    df = _df([
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100, "raw_buy": True, "confirm_bull": True},  # ENTRY
        {"o": 100, "h": 109, "l": 100, "c": 108},   # high 109 >= TP4 (108) -> close +2R
    ])
    tr = bt.simulate(df, bt.Cfg(mode="trail", **NOCOST))
    assert len(tr) == 1
    assert tr[0].outcome == "TP4"
    assert tr[0].exit == 108.0
    assert abs(tr[0].r - 2.0) < 1e-9


# entry short @100 -> sl0=104, tp1..4 = 98/96/94/92
def test_trail_short_locks_profit_on_reversal():
    df = _df([
        {"o": 100, "h": 100, "l": 100, "c": 100},
        {"o": 100, "h": 100, "l": 100, "c": 100, "raw_sell": True, "confirm_bear": True},  # ENTRY short
        {"o": 100, "h": 100, "l": 95, "c": 96},     # reaches TP2 (96) -> stop ratchets to TP1 (98)
        {"o": 97, "h": 99, "l": 97, "c": 99},       # rises to 99 >= 98 -> TRAIL exit @98
    ])
    tr = bt.simulate(df, bt.Cfg(mode="trail", **NOCOST))
    assert len(tr) == 1
    assert tr[0].dir == "Short"
    assert tr[0].outcome == "TRAIL"
    assert tr[0].exit == 98.0
    assert abs(tr[0].r - 0.5) < 1e-9


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
