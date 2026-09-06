"""
GCP Trend Sniper -- live SESSION BOT for XAUUSD on MT5 DEMO.

Recomputes the indicator with the shared engine (gcp_engine.build_indicators) on
CLOSED M5 bars and trades the demo account per the approved rules:

  Session (IST wall-clock):  new entries only 17:30-21:30; a trade already open
                             is left to run and force-closed if still open past
                             23:30 (or any time outside 17:30-23:30, e.g. after
                             a restart) -- never held overnight.
  Order:                     market, 0.01 lot, ONE position at a time.
  Stop:                      SL = 2*ATR.
  Target:                    fixed TP = 4*ATR = 2R (the indicator's TP4; no ADX/ratio pick).
  Extra exits:               opposite signal -> close ; 23:30 cutoff -> close.
  Daily (reset each IST day): max 3 entries ; two CONSECUTIVE losses -> stop for
                             the day (a win resets the streak).

Safety: refuses to run on anything but a DEMO account. --dry-run computes and
logs the exact order it WOULD send without sending it. Every cycle appends a
decision row to gcp_live_log.csv. State persists in gcp_live_state.json so a
mid-session restart neither double-enters nor loses its daily counters.

Usage:
  python gcp_live.py --dry-run     # observe: log intended orders, send nothing
  python gcp_live.py --dry-run --once
  python gcp_live.py               # live on the demo account
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time as _time
from dataclasses import dataclass, asdict
from datetime import datetime, time, timedelta, timezone

import pandas as pd

from gcp_engine import build_indicators

try:
    import MetaTrader5 as mt5
except Exception:                # allow importing this module for tests without the package
    mt5 = None

# ------------------------------- config ---------------------------------

SYMBOL = "XAUUSD"
LOT = 0.01
MAGIC = 920250906
DEVIATION = 20                   # max slippage, points

IST = timezone(timedelta(hours=5, minutes=30))
ENTRY_START = time(17, 30)       # first time a new entry may open (IST)
ENTRY_END = time(21, 30)         # last time a new entry may open (IST)
RUN_UNTIL = time(23, 30)         # a trade may stay open until here; then force-close (IST)

MAX_TRADES_PER_DAY = 3
MAX_CONSEC_LOSSES = 2
COOLDOWN_BARS = 8
SL_MULT = 2.0
TP_MULT = 4.0                    # fixed target = 4*ATR = 2R (indicator's TP4)

WARMUP_BARS = 400                # M5 bars pulled each cycle (covers all lookbacks)
POLL_SECONDS = 10

STATE_FILE = "gcp_live_state.json"
LOG_FILE = "gcp_live_log.csv"

# ------------------------- pure decision logic --------------------------
# These functions contain the trading RULES and take no MT5 dependency, so
# they can be unit-tested directly (see test_gcp_live.py).


def in_entry_window(now_ist: datetime) -> bool:
    """True only during 17:30-21:30 IST -- when a NEW entry may be opened."""
    return ENTRY_START <= now_ist.timetz().replace(tzinfo=None) <= ENTRY_END


def should_be_flat(now_ist: datetime) -> bool:
    """True when NO position should be open: outside the 17:30-23:30 IST run window.

    Covers both the 23:30 hard cutoff and the overnight/pre-session period (so a
    position surviving a restart gets closed rather than held)."""
    t = now_ist.timetz().replace(tzinfo=None)
    return not (ENTRY_START <= t < RUN_UNTIL)


def daily_gate_open(trades_today: int, consec_losses: int) -> bool:
    """True if the daily rules still permit a new entry."""
    return trades_today < MAX_TRADES_PER_DAY and consec_losses < MAX_CONSEC_LOSSES


def setup_label(row, is_buy: bool) -> str:
    if is_buy:
        return "PULLBACK" if row["B_buy"] else "SQUEEZE" if row["C_buy"] else "TREND"
    return "PULLBACK" if row["B_sell"] else "SQUEEZE" if row["C_sell"] else "TREND"


@dataclass
class Signal:
    side: str | None          # "BUY" / "SELL" / None
    setup: str
    adx: float
    atr: float
    close: float
    bar_time: object
    cooldown_ok: bool


def evaluate_last_closed(d: pd.DataFrame, last_entry_time) -> Signal:
    """Read the indicator signal off the LAST row of an already-indicatored frame.

    `d` must end at the last CLOSED bar (caller drops the forming bar). Cooldown
    is measured in bars since the last entry, matching the Pine engine."""
    row = d.iloc[-1]
    bar_time = row["time"]
    if last_entry_time is None:
        cooldown_ok = True
    else:
        bars_since = int((d["time"] > last_entry_time).sum())   # closed bars after entry
        cooldown_ok = bars_since > COOLDOWN_BARS

    valid = pd.notna(row["atr"]) and pd.notna(row["vol_ok"])
    buy = bool(valid and row["raw_buy"] and row["filt"] and row["confirm_bull"] and cooldown_ok)
    sell = bool(valid and row["raw_sell"] and row["filt"] and row["confirm_bear"] and cooldown_ok)

    side = "BUY" if buy else "SELL" if sell else None
    setup = "-" if side is None else setup_label(row, side == "BUY")
    return Signal(side=side, setup=setup, adx=float(row["adx"]), atr=float(row["atr"]),
                  close=float(row["close"]), bar_time=bar_time, cooldown_ok=cooldown_ok)


def order_prices(side: str, ref_price: float, atr: float, digits: int):
    """Return (sl, tp): SL = 2*ATR, TP = 4*ATR (= 2R), matching the indicator's TP4."""
    if side == "BUY":
        sl = ref_price - SL_MULT * atr
        tp = ref_price + TP_MULT * atr
    else:
        sl = ref_price + SL_MULT * atr
        tp = ref_price - TP_MULT * atr
    return round(sl, digits), round(tp, digits)


# ------------------------------- state ----------------------------------

@dataclass
class State:
    day: str = ""                 # IST date (YYYY-MM-DD) the counters belong to
    trades_today: int = 0
    consec_losses: int = 0
    open_ticket: int | None = None
    last_entry_time: str | None = None   # broker bar time (ISO) of last entry, for cooldown


def load_state() -> State:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return State(**json.load(f))
    return State()


def save_state(s: State) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump(asdict(s), f, indent=2)


def rollover_day(s: State, now_ist: datetime) -> None:
    today = now_ist.date().isoformat()
    if s.day != today:
        s.day = today
        s.trades_today = 0
        s.consec_losses = 0


def log_row(**cols) -> None:
    new = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(list(cols.keys()))
        w.writerow(list(cols.values()))


# ------------------------------ MT5 I/O ---------------------------------

def ist_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(IST)


def mt5_connect_demo():
    if not mt5.initialize():
        raise SystemExit(f"MT5 initialize() failed: {mt5.last_error()}")
    info = mt5.account_info()
    if info is None:
        mt5.shutdown()
        raise SystemExit("MT5 account_info() returned None -- is a terminal logged in?")
    if info.trade_mode != mt5.ACCOUNT_TRADE_MODE_DEMO:
        mt5.shutdown()
        raise SystemExit(
            f"REFUSING TO RUN: account {info.login} is not a DEMO account "
            f"(trade_mode={info.trade_mode}). This bot is demo-only.")
    print(f"Connected DEMO account {info.login}  balance={info.balance} {info.currency}")
    if not mt5.symbol_select(SYMBOL, True):
        mt5.shutdown()
        raise SystemExit(f"cannot select symbol {SYMBOL}")
    return mt5.symbol_info(SYMBOL)


def pick_filling(si):
    fm = si.filling_mode
    if fm & mt5.SYMBOL_FILLING_IOC:
        return mt5.ORDER_FILLING_IOC
    if fm & mt5.SYMBOL_FILLING_FOK:
        return mt5.ORDER_FILLING_FOK
    return mt5.ORDER_FILLING_RETURN


def pull_closed_bars() -> pd.DataFrame:
    rates = mt5.copy_rates_from_pos(SYMBOL, mt5.TIMEFRAME_M5, 0, WARMUP_BARS)
    if rates is None or len(rates) < 60:
        raise RuntimeError(f"copy_rates returned too little: {mt5.last_error()}")
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")   # broker server time (naive)
    return df.iloc[:-1].reset_index(drop=True)          # drop the still-forming bar


def our_position(si):
    positions = mt5.positions_get(symbol=SYMBOL)
    if not positions:
        return None
    for p in positions:
        if p.magic == MAGIC:
            return p
    return None


def closed_pl(position_id: int) -> float:
    deals = mt5.history_deals_get(position=position_id)
    if not deals:
        return 0.0
    return float(sum(d.profit + d.commission + d.swap for d in deals))


def send_market(si, side: str, sl: float, tp: float, dry: bool) -> int | None:
    tick = mt5.symbol_info_tick(SYMBOL)
    price = tick.ask if side == "BUY" else tick.bid
    otype = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=LOT, type=otype,
               price=price, sl=sl, tp=tp, deviation=DEVIATION, magic=MAGIC,
               comment="gcp-live", type_time=mt5.ORDER_TIME_GTC,
               type_filling=pick_filling(si))
    if dry:
        print(f"[DRY-RUN] would {side} {LOT} {SYMBOL} @~{price} SL={sl} TP={tp}")
        return None
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"order_send FAILED: retcode={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
        return None
    pos = our_position(si)
    return pos.ticket if pos else None


def close_position(si, pos, dry: bool) -> bool:
    tick = mt5.symbol_info_tick(SYMBOL)
    if pos.type == mt5.POSITION_TYPE_BUY:
        otype, price = mt5.ORDER_TYPE_SELL, tick.bid
    else:
        otype, price = mt5.ORDER_TYPE_BUY, tick.ask
    req = dict(action=mt5.TRADE_ACTION_DEAL, symbol=SYMBOL, volume=pos.volume, type=otype,
               position=pos.ticket, price=price, deviation=DEVIATION, magic=MAGIC,
               comment="gcp-close", type_time=mt5.ORDER_TIME_GTC,
               type_filling=pick_filling(si))
    if dry:
        print(f"[DRY-RUN] would CLOSE ticket {pos.ticket} @~{price}")
        return False
    r = mt5.order_send(req)
    if r is None or r.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"close FAILED: retcode={getattr(r,'retcode',None)} {getattr(r,'comment','')}")
        return False
    return True


# ------------------------------- loop -----------------------------------

def run(dry: bool, once: bool):
    si = mt5_connect_demo()
    digits = si.digits
    state = load_state()
    last_processed = None
    print(f"gcp_live running  symbol={SYMBOL}  dry_run={dry}  "
          f"entries {ENTRY_START}-{ENTRY_END} IST, cutoff {RUN_UNTIL} IST")

    while True:
        now = ist_now()
        rollover_day(state, now)
        pos = our_position(si)

        # --- reconcile a position that closed since last cycle (SL/TP/manual) ---
        if state.open_ticket is not None and pos is None:
            pl = closed_pl(state.open_ticket)
            if pl < 0:
                state.consec_losses += 1
            else:
                state.consec_losses = 0
            log_row(ts_ist=now.isoformat(timespec="seconds"), event="CLOSED_DETECTED",
                    detail=f"ticket={state.open_ticket} pl={pl:.2f} "
                           f"consec_losses={state.consec_losses}")
            print(f"position {state.open_ticket} closed, pl={pl:.2f}, "
                  f"consec_losses={state.consec_losses}")
            state.open_ticket = None
            save_state(state)
        elif state.open_ticket is None and pos is not None:
            state.open_ticket = pos.ticket        # adopt (e.g. after restart)
            save_state(state)

        # --- time-based force-close (23:30 cutoff or outside run window) --------
        if pos is not None and should_be_flat(now):
            if close_position(si, pos, dry):
                state.open_ticket = None
            log_row(ts_ist=now.isoformat(timespec="seconds"), event="FORCE_CLOSE",
                    detail=f"ticket={pos.ticket} outside run window / cutoff")
            save_state(state)

        # --- signal work only on a NEW closed bar ------------------------------
        df = pull_closed_bars()
        d = build_indicators(df)
        bar_time = d["time"].iloc[-1]

        if bar_time != last_processed:
            last_processed = bar_time
            sig = evaluate_last_closed(d, _parse_ts(state.last_entry_time))
            pos = our_position(si)
            action, detail = "none", ""

            if pos is not None:
                # opposite-signal exit
                is_long = pos.type == mt5.POSITION_TYPE_BUY
                if (is_long and sig.side == "SELL") or (not is_long and sig.side == "BUY"):
                    if close_position(si, pos, dry):
                        state.open_ticket = None
                    action, detail = "REV_CLOSE", f"opposite {sig.side}"
            else:
                can_enter = (in_entry_window(now) and not should_be_flat(now)
                             and daily_gate_open(state.trades_today, state.consec_losses)
                             and sig.side is not None and sig.cooldown_ok)
                if can_enter:
                    sl, tp = order_prices(sig.side, sig.close, sig.atr, digits)
                    ticket = send_market(si, sig.side, sl, tp, dry)
                    if not dry and ticket is not None:
                        state.open_ticket = ticket
                        state.trades_today += 1
                        state.last_entry_time = pd.Timestamp(bar_time).isoformat()
                    action = "ENTRY_DRYRUN" if dry else "ENTRY"
                    detail = (f"{sig.side} {sig.setup} adx={sig.adx:.1f} "
                              f"target=2R sl={sl} tp={tp}")

            log_row(ts_ist=now.isoformat(timespec="seconds"), bar_time=str(bar_time),
                    in_pos=(pos is not None), sig=str(sig.side), setup=sig.setup,
                    adx=round(sig.adx, 1), atr=round(sig.atr, 2),
                    window=in_entry_window(now), flat_required=should_be_flat(now),
                    trades_today=state.trades_today, consec_losses=state.consec_losses,
                    cooldown_ok=sig.cooldown_ok, action=action, detail=detail)
            if action != "none":
                print(f"{now:%H:%M} IST  {action}  {detail}")
            save_state(state)

        if once:
            break
        _time.sleep(POLL_SECONDS)

    mt5.shutdown()


def _parse_ts(s):
    return None if not s else pd.Timestamp(s)


def main():
    ap = argparse.ArgumentParser(description="GCP Trend Sniper live session bot (MT5 demo).")
    ap.add_argument("--dry-run", action="store_true", help="log intended orders; send nothing")
    ap.add_argument("--once", action="store_true", help="run a single cycle then exit")
    args = ap.parse_args()
    if mt5 is None:
        raise SystemExit("MetaTrader5 package not importable in this environment.")
    run(dry=args.dry_run, once=args.once)


if __name__ == "__main__":
    main()
