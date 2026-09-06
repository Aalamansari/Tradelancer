"""
Unit tests for the PURE trailing-exit math in gcp_trailing.py (rule A ladder).
No MT5, no data. Run:  python test_gcp_trailing.py   (or: pytest test_gcp_trailing.py)
"""
import gcp_trailing as tr


def test_compute_levels_buy():
    sl, tps = tr.compute_levels("BUY", 100.0, 2.0)
    assert sl == 96.0
    assert tps == [102.0, 104.0, 106.0, 108.0]


def test_compute_levels_sell():
    sl, tps = tr.compute_levels("SELL", 100.0, 2.0)
    assert sl == 104.0
    assert tps == [98.0, 96.0, 94.0, 92.0]


def test_targets_reached_buy():
    _, tps = tr.compute_levels("BUY", 100.0, 2.0)
    assert tr.targets_reached("BUY", 101.0, tps) == 0    # below TP1
    assert tr.targets_reached("BUY", 102.0, tps) == 1    # exactly TP1
    assert tr.targets_reached("BUY", 105.0, tps) == 2    # past TP2
    assert tr.targets_reached("BUY", 108.0, tps) == 4    # TP4


def test_targets_reached_sell():
    _, tps = tr.compute_levels("SELL", 100.0, 2.0)
    assert tr.targets_reached("SELL", 99.0, tps) == 0
    assert tr.targets_reached("SELL", 98.0, tps) == 1
    assert tr.targets_reached("SELL", 95.0, tps) == 2
    assert tr.targets_reached("SELL", 92.0, tps) == 4


def test_next_stop_buy_rule_a():
    sl0, tps = tr.compute_levels("BUY", 100.0, 2.0)
    assert tr.next_stop("BUY", 100.0, sl0, tps, 0) == 96.0    # nothing hit -> original SL
    assert tr.next_stop("BUY", 100.0, sl0, tps, 1) == 100.0   # TP1 hit -> breakeven
    assert tr.next_stop("BUY", 100.0, sl0, tps, 2) == 102.0   # TP2 hit -> TP1
    assert tr.next_stop("BUY", 100.0, sl0, tps, 3) == 104.0   # TP3 hit -> TP2


def test_next_stop_sell_rule_a():
    sl0, tps = tr.compute_levels("SELL", 100.0, 2.0)
    assert tr.next_stop("SELL", 100.0, sl0, tps, 0) == 104.0
    assert tr.next_stop("SELL", 100.0, sl0, tps, 1) == 100.0
    assert tr.next_stop("SELL", 100.0, sl0, tps, 2) == 98.0
    assert tr.next_stop("SELL", 100.0, sl0, tps, 3) == 96.0


def test_next_stop_sched_breakeven_only():
    # {1: BE} -> move to breakeven after TP1, then hold (no further trailing)
    sl0, tps = tr.compute_levels("BUY", 100.0, 2.0)
    sched = {1: "BE"}
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 0, sched) == 96.0    # original SL
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 1, sched) == 100.0   # breakeven
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 2, sched) == 100.0   # stays BE
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 3, sched) == 100.0   # stays BE


def test_next_stop_sched_trail_late():
    # {2: BE, 3: TP1} -> nothing until TP2, then BE, then TP1 after TP3
    sl0, tps = tr.compute_levels("BUY", 100.0, 2.0)
    sched = {2: "BE", 3: "TP1"}
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 1, sched) == 96.0    # still original
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 2, sched) == 100.0   # BE after TP2
    assert tr.next_stop_sched("BUY", 100.0, sl0, tps, 3, sched) == 102.0   # TP1 after TP3


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    main()
