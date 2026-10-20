# V25 Pine Script changes

| File | What it is |
|---|---|
| `pine/V25_original.pine` | Your V25 source exactly as supplied in chat (reference, unmodified). |
| `pine/V25_webhook.pine` | V25 plus webhook output. Version string `V25-webhook.1`. |
| `pine/V26_webhook.pine` | V26: a separate script that **does** change the strategy logic (`V26-webhook.1`). Documented in [V26_STRATEGY.md](V26_STRATEGY.md). Both V25 files are unchanged, and a test pins their exact bytes. |

## Strategy logic: unchanged

`diff pine/V25_original.pine pine/V25_webhook.pine` shows **only insertions**, no deleted or modified lines. `tests/test_pine.py` enforces this:

* every original line appears, in order, in the webhook version;
* the list of `strategy.entry / exit / close_all / cancel_all` calls is identical;
* no added (non-comment) line calls an order function or assigns to `state`, `qty`, `tradeStop`, `tradeTarget` or `tradesToday`;
* every JSON key the script emits matches the server schema exactly.

Signals, HTF FVG detection, IFVG inversion, third-candle stops, liquidity targets, sizing, prop simulation, and session handling behave exactly as before. Backtest results in the Strategy Tester should be identical. If they are not, stop and report it, because that would mean the patch is wrong.

## Additions

1. **Input group "9. Webhook Automation"**
   * `Emit webhook alert() JSON` (default on)
   * `Bot id`: must match `ALLOWED_BOT_IDS` on the server
   * `Optional body passphrase`: leave blank unless `WEBHOOK_PASSPHRASE` is set
   * `Emit 1m bar prices`: OHLC per bar, used by the paper simulator to fill stops and targets
   * `Emit TradingView exit / flatten events`
2. **Helper functions** `f_num`, `f_eid`, `f_base`: JSON building only.
3. **Entry event**: emitted in the same branch that calls `strategy.entry`/`strategy.exit`. It carries side, qty, signal close, `estimatedFill`, the original structural stop (`tradeStop`) and liquidity target (`tradeTarget`), tick size, point value, risk per contract, planned risk, sizing mode, and `syminfo.current_contract`.
4. **Flatten events**: next to the two existing `strategy.close_all` calls (prop risk halt, NY session flat).
5. **Exit event**: when `strategy.closedtrades` increases (TradingView's simulated trade closed).
6. **Bar event**: once per confirmed bar.

All alerts use `alert.freq_once_per_bar_close`. Event IDs are deterministic (`V25-<botId>-<bar time>-<kind>`), so a re-delivered alert carries the same ID and is deduplicated by the server.

## Things to verify in TradingView (not possible here)

* **Compile.** The script has not been compiled. TradingView is the only Pine compiler. If `syminfo.current_contract` is reported as unknown, your Pine version predates it. Replace `syminfo.current_contract` in `f_base` with `""`. The server then falls back to its roll calendar and refuses entries during roll week (safe, but you'll miss roll-week trades).
* **Backtest parity.** Compare Strategy Tester results of the original and webhook versions on the same chart and settings. They must match.
* **Alert rate.** One `bar` alert per minute plus occasional trade alerts stays well under TradingView's rate limits. If TradingView ever stops the alert for triggering too often, disable `Emit 1m bar prices` and feed prices via `/paper/quote` instead.

## Observations (not changed, documented only)

* **Sizing mode vs engine caps.** In `V22 Baseline` mode, V25 requires at least 5 contracts (`minContracts`) and risks up to $1,500. The engine defaults (`MAX_CONTRACTS=2`, `MAX_TRADE_RISK_USD=200`) will **reject** those signals. For prop-style limits use `Prop Conservative` and set the V25 inputs `Maximum contracts` ≤ `MAX_CONTRACTS` and `Prop maximum planned risk USD` ≤ `MAX_TRADE_RISK_USD`. `RISK_SIZING_MODE=reduce` shrinks the quantity instead of rejecting, but that changes position sizing relative to the backtest.
* **Prop simulation is approximate.** V25's prop tracking uses `strategy.equity`, sampled per bar. The engine does not trust it. It computes daily loss and drawdown from broker fills and balances.
* **Backtest ≠ forward performance.** A backtested profit factor does not guarantee future profitability. Paper-trade long enough to compare live fills, slippage, and trade frequency with the backtest before considering anything else.
