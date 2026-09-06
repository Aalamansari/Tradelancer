"""
Unit tests for the PURE decision logic in gcp_live.py (the trading rules).
No MT5 needed. Run:  python test_gcp_live.py   (or: pytest test_gcp_live.py)
"""
from datetime import datetime, timedelta, timezone

import pandas as pd

import gcp_live as g

IST = timezone(timedelta(hours=5, minutes=30))


def _ist(h, m):
    return datetime(2026, 9, 6, h, m, tzinfo=IST)


def test_entry_window():
    assert not g.in_entry_window(_ist(17, 29))
    assert g.in_entry_window(_ist(17, 30))      # inclusive start
    assert g.in_entry_window(_ist(19, 0))
    assert g.in_entry_window(_ist(21, 30))      # inclusive end
    assert not g.in_entry_window(_ist(21, 31))
    assert not g.in_entry_window(_ist(23, 0))
    assert not g.in_entry_window(_ist(12, 0))


def test_should_be_flat():
    # open allowed only within [17:30, 23:30)
    assert g.should_be_flat(_ist(17, 29))
    assert not g.should_be_flat(_ist(17, 30))
    assert not g.should_be_flat(_ist(21, 30))
    assert not g.should_be_flat(_ist(23, 25))
    assert g.should_be_flat(_ist(23, 30))       # hard cutoff -> flat
    assert g.should_be_flat(_ist(2, 0))         # overnight -> flat


def test_daily_gate():
    assert g.daily_gate_open(0, 0)
    assert g.daily_gate_open(2, 0)
    assert not g.daily_gate_open(3, 0)          # max 3 trades
    assert not g.daily_gate_open(0, 2)          # two consecutive losses
    assert g.daily_gate_open(2, 1)
    assert not g.daily_gate_open(1, 2)


def test_target_selection():
    assert g.tp_multiplier(25.0) == 4.0         # boundary: not > 25 -> 1:2
    assert g.tp_multiplier(25.1) == 6.0
    assert g.tp_multiplier(30.0) == 6.0
    assert g.tp_multiplier(18.0) == 4.0
    assert g.target_ratio(30.0) == "1:3"
    assert g.target_ratio(20.0) == "1:2"


def test_order_prices():
    # BUY, strong trend (1:3): sl 2*ATR below, tp 6*ATR above
    sl, tp = g.order_prices("BUY", 100.0, 2.0, 30.0, 2)
    assert sl == 96.0 and tp == 112.0
    # SELL, weak trend (1:2): sl 2*ATR above, tp 4*ATR below
    sl, tp = g.order_prices("SELL", 100.0, 2.0, 20.0, 2)
    assert sl == 104.0 and tp == 92.0


def _make_df(n=12, **last_overrides):
    base = dict(raw_buy=False, raw_sell=False, filt=True, confirm_bull=False,
                confirm_bear=False, atr=1.0, vol_ok=True, adx=20.0, close=100.0,
                B_buy=False, C_buy=False, B_sell=False, C_sell=False)
    rows = [dict(base, time=pd.Timestamp("2026-09-06 10:00") + pd.Timedelta(minutes=5 * i))
            for i in range(n)]
    rows[-1].update(last_overrides)
    return pd.DataFrame(rows)


def test_signal_buy_no_cooldown():
    d = _make_df(raw_buy=True, confirm_bull=True, C_buy=True, adx=27.0)
    sig = g.evaluate_last_closed(d, last_entry_time=None)
    assert sig.side == "BUY"
    assert sig.setup == "SQUEEZE"
    assert sig.cooldown_ok
    assert g.target_ratio(sig.adx) == "1:3"


def test_signal_blocked_by_cooldown():
    d = _make_df(n=12, raw_buy=True, confirm_bull=True)
    # entry 5 bars back -> only 5 closed bars after -> not > 8 -> blocked
    entry_time = d["time"].iloc[-6]
    sig = g.evaluate_last_closed(d, last_entry_time=entry_time)
    assert not sig.cooldown_ok
    assert sig.side is None


def test_signal_cooldown_satisfied():
    d = _make_df(n=12, raw_buy=True, confirm_bull=True)
    entry_time = d["time"].iloc[0]              # 11 bars back -> > 8 -> ok
    sig = g.evaluate_last_closed(d, last_entry_time=entry_time)
    assert sig.cooldown_ok
    assert sig.side == "BUY"


def test_no_signal_when_filter_fails():
    d = _make_df(raw_buy=True, confirm_bull=True, filt=False)
    sig = g.evaluate_last_closed(d, last_entry_time=None)
    assert sig.side is None


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
