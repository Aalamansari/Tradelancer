# GCP Trend Sniper — Live Session Bot (MT5 Demo) — Design Spec

**Date:** 2026-09-06
**Status:** Approved design, pre-implementation
**Location:** `C:\Users\aalam\Develop\MoneyMaker`

---

## 1. Purpose

Run the "GrowthClubPK Trend Sniper" indicator's signals automatically on an
**MT5 demo account**, trading **XAUUSD on M5**, inside a fixed evening session
(17:30–21:30 IST), with position/risk rules of the user's choosing. The goal is a
**true forward test** on demo — the decisive out-of-sample step that our earlier
backtest (`gcp_backtest.py`) flagged as missing. This is **not** a money-making
claim: the indicator's measured edge is thin, not statistically significant, and
in-sample. Demo results are data, nothing more.

No TradingView is in the execution loop. The indicator is a formula; we already
have a faithful Python port of it (`build_indicators()` in `gcp_backtest.py`).
The bot recomputes the same signals from MT5 candles and places demo orders.

## 2. Goals / Non-goals

**Goals**
- One self-contained Python bot, launched manually, that trades the demo session unattended.
- Signal logic = the existing faithful port, shared as a single source of truth.
- Enforce the exact session, entry, exit, target, and daily-limit rules in §5.
- Refuse to run on anything but a demo account.
- Be auditable: log every per-bar decision; provide a `--dry-run` mode.
- Provide a verification step proving the Python signals match the TradingView chart.

**Non-goals**
- No live/real-money trading. No real-account code paths at all.
- No TradingView webhooks, tunnels, or cloud hosting.
- No reoptimization of the indicator's parameters. We trade it as-is.
- No portfolio/multi-symbol logic. XAUUSD only.
- No GUI. Console + CSV logs.

## 3. Architecture

Four Python files in `MoneyMaker/`, plus two runtime data files.

| File | Role |
|------|------|
| `gcp_engine.py` | **Shared signal engine.** `build_indicators(df)` extracted verbatim from `gcp_backtest.py` (EMA ribbon, MACD, StochRSI, Bollinger, ATR, manual ADX, filters, setups A/B/C, `raw_buy`/`raw_sell`/`filt`/`confirm_*`). Single source of truth. |
| `gcp_backtest.py` | **Unchanged behavior.** Refactored only to `import` the engine from `gcp_engine.py` instead of defining it inline. Its numbers must not move. |
| `gcp_live.py` | **The live session bot.** Connects to MT5, loops per closed M5 bar, applies §5 rules, sends/manages demo orders. |
| `verify_signals.py` | **Port-vs-TradingView check.** Prints exact BUY/SELL timestamps over recent M5 history for eyeball comparison against the chart. |
| `gcp_live_state.json` | Runtime state: current IST trade-day, trades-taken-today, consecutive-loss count, our open position ticket. Survives a mid-session restart. |
| `gcp_live_log.csv` | One row per evaluated bar: time, ADX, which setup fired, filter states, gate results, action taken. |

**Data flow (per closed M5 bar):**

```
MT5 copy_rates (last ~400 M5 bars)
  -> gcp_engine.build_indicators(df)
  -> inspect the LAST CLOSED bar for buy_signal / sell_signal
  -> apply gates: session window, one-position, cooldown, daily caps
  -> place / manage demo order via mt5.order_send
  -> append decision to gcp_live_log.csv ; persist gcp_live_state.json
  -> sleep until next M5 close
```

## 4. Signal semantics (from the port, unchanged)

A **BUY** is true on a closed bar when `raw_buy AND filt AND confirm_bull AND cooldown_ok`:
- `raw_buy` = setup A (trend) OR B (pullback) OR C (squeeze), exactly as in the Pine.
- `filt` = `trend_ok` (|e1−e10|/ATR > 0.5) AND `vol_ok` (ATR > SMA(ATR,50)) AND `adx_ok` (ADX > 18).
- `confirm_bull` = close>open AND close>prev close.
- `cooldown_ok` = ≥ 8 bars since our last entry.

**SELL** is the mirror. Evaluated **only on closed bars** (matches
`alert.freq_once_per_bar_close`; no repaint, no look-ahead). Because `adx_ok`
requires ADX > 18, ADX at any entry is always > 18.

## 5. Trading rules (the contract)

**Session (all times IST, from system wall-clock → UTC+5:30; never from broker bar timestamps):**
- New entries allowed **only 17:30–21:30**.
- A position opened before 21:30 is **left to run**; **force-closed at 23:30** if still open (overnight-gap guard).

**Entry:** market order, **0.01 lot**, **at most one open position at any time**.

**Stop loss:** `SL = 2 · ATR` from entry (ATR of the signal bar), placed broker-side on the order.

**Take profit (chosen by ADX at the signal bar), placed broker-side:**
- ADX **> 25** → **1:3** → `TP = entry ± 6 · ATR`.
- ADX **18–25** → **1:2** → `TP = entry ± 4 · ATR`.

**Script-managed exits (in addition to broker SL/TP):**
- **Opposite signal:** if a `sell_signal` fires while long (or `buy_signal` while short), close at market. The 8-bar cooldown then prevents an immediate reverse entry — identical to the backtest.
- **23:30 hard cutoff:** force-close any still-open position.

**Daily limits (reset at the start of each IST trade-day):**
- **Max 3 entries per day.**
- **Two consecutive losing trades → no further entries that day.** A winning trade resets the consecutive-loss counter.
- **Loss** = the closed trade's realized account P/L `< 0`. Break-even/positive is not a loss.

## 6. Order execution details

- **Magic number:** every order tagged with a constant magic (e.g. `920250906`) so the bot only ever reads/manages its own positions.
- **Symbol:** default `"XAUUSD"`; a config constant allows an override if the broker names gold differently. Bot verifies the symbol exists and is selected (as `pull_data.py` does).
- **Filling mode / deviation:** derived from `symbol_info` (supported filling modes); market orders use a bounded `deviation` (slippage cap in points).
- **Price/volume rounding:** SL/TP rounded to `symbol_info.digits`; volume validated against `volume_min` / `volume_step`.
- **Result checking:** every `order_send` return code checked; failures logged and surfaced, never silently ignored.
- **Restart reconciliation:** on startup the bot reads open positions by magic number and its `gcp_live_state.json` to rebuild "do I already have a trade / how many today / current loss streak" — so a crash-and-relaunch mid-session neither double-enters nor loses its daily counters.

## 7. Safety rails

- **Demo assert:** on connect, require `account_info().trade_mode == 0` (demo). If not demo, log loudly and **exit without trading**. No real-account path exists.
- **`--dry-run`:** compute and log the exact intended order (side, lot, entry, SL, TP, reason) but **do not** call `order_send`. Used for the first live session.
- **Out-of-window inaction:** outside 17:30–21:30 the bot only monitors/manages existing trades and never opens new ones.
- **Decision log:** `gcp_live_log.csv` records why each bar did or didn't trade, for auditing against the chart and for post-session review.

## 8. Verification (`verify_signals.py`) — gate before trading

Run the shared engine over recent M5 history (reusing the existing
`xauusd_m5.csv` or a fresh pull) and print the exact timestamps where BUY/SELL
fire, with the triggering setup (A/B/C). The user opens the same dates on the
TradingView chart and confirms the arrows line up. If they match → the port is
proven faithful and the bot is cleared to trade. If any drift appears → fix
`gcp_engine.py` until it matches, then re-verify.

## 9. Testing

- **Engine parity test:** feed `gcp_engine.build_indicators()` a fixed slice of
  `xauusd_m5.csv` and assert the fired signal bars equal those implied by the
  saved backtest blotter (`gcp_trades_full.csv`). This guards the extract-to-module
  refactor — `gcp_backtest.py` output must not change.
- **Rule unit checks:** small synthetic sequences asserting (a) session-window
  gating in IST, (b) max-3/day, (c) two-consecutive-losses lockout with win-reset,
  (d) TP multiplier selection at the ADX=25 boundary, (e) 23:30 cutoff.
- **Dry-run session:** run `--dry-run` against live MT5 for one real session;
  confirm it detects closed bars, computes signals, respects the window and caps,
  and would place well-formed orders — without sending any.
- Only after all the above: **0.01-lot live demo.**

## 10. Rollout order

1. Extract `gcp_engine.py`; make `gcp_backtest.py` import it; confirm backtest numbers unchanged (parity test).
2. Build `verify_signals.py`; **user verifies Python signals vs TradingView chart.**
3. Build `gcp_live.py` with all §5 rules and §7 rails.
4. One `--dry-run` live session; review the log.
5. Flip to 0.01-lot demo; review session by session.

## 11. Risks & honest caveats

- **This is a different strategy than the backtest.** Session-bounded entries,
  1:2/1:3 ADX-chosen targets, and daily caps all diverge from the backtest's
  24h / TP4=+2R / exit-on-SL-TP4-opposite model. Do **not** expect the backtest's
  +0.074R. Read demo results as a fresh, standalone forward test.
- **Edge is unproven.** Prior work: thin, not significant (t≈1.89), cost-sensitive,
  in-sample. Demo forward-testing is exactly to see whether anything survives live
  fills/spread in this session. Real money is out of scope until it does.
- **Broker time vs IST** is the most likely bug source; the session gate uses
  wall-clock IST, and this is called out explicitly to avoid the trap.
- **Port fidelity** is assumed until §8 verification passes; that step is a hard gate.
- **Feed differences:** the demo broker's XAUUSD candles/spread differ from
  TradingView's data source, so a few marginal signals may legitimately differ even
  with a faithful port — §8 is about confirming the logic matches, not that two
  different data feeds are identical tick-for-tick.

## 12. Open items (none blocking)

- Whether to later wrap `gcp_live.py` as a Windows Scheduled Task for hands-off
  daily start — deferred; manual launch is the simple starting point.
- `MoneyMaker` is not its own git repo (the git root is the home directory), so
  this spec is not committed. Initializing a dedicated repo for the project is
  optional and can be decided separately.
