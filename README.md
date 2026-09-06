# Tradelancer — GCP Trend Sniper research & demo bot

Backtest + MT5 **demo** automation for the "GrowthClubPK Trend Sniper" signal on
**XAUUSD M5**. Research-grade and honest: this repo measures whether the signal
actually has an edge, rather than assuming it does.

> **Status: demo / research only.** No real-money trading. The live bot is
> hard-locked to DEMO accounts and runs in `--dry-run` by default.

## Signal engine (faithful port)
`gcp_engine.py` is a no-repaint, no-look-ahead Python port of the indicator's
tradeable logic (EMA ribbon, MACD, StochRSI, Bollinger, ATR/ADX filters, setups
A/B/C). It is the single source of truth shared by the backtester and the bot.

## Files
| File | Purpose |
|------|---------|
| `gcp_engine.py` | shared signal engine (indicators + setups) |
| `gcp_trailing.py` | shared trailing-exit ladder (levels / targets / stop schedule) |
| `gcp_backtest.py` | backtest with `full` / `scaled` / `trail` exit modes + costs |
| `gcp_live.py` | MT5 **demo** session bot (dry-run capable) |
| `verify_signals.py`, `diag.py`, `backtest.py` | inspection / earlier iterations |
| `test_*.py` | unit tests (signal rules + trailing math) |
| `*_m5.csv` | XAUUSD / EURUSD / GBPUSD M5 history |

## Key findings (backtested on broker M5, ~17 months)
- **XAUUSD single-TP** exit: ~**+0.074R/trade** after realistic costs — a thin but
  positive edge. Trailing variants do **not** beat it (they cap the few 2R winners).
- The edge is **XAUUSD-specific**: on EURUSD and GBPUSD the same signal is
  **negative even at zero cost** — no edge there. Do not multi-pair this signal.
- Treat the gold edge as thin and unproven until a real demo forward-test confirms it.

## Run
```bash
python gcp_backtest.py           # backtest all exit modes + cost sensitivity
python test_gcp_trailing.py      # unit tests
python gcp_live.py --dry-run     # observe live signals on the MT5 demo (no orders)
```
