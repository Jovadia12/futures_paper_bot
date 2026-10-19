# V25 Execution Engine — Implementation Plan

## 0. Repository inspection (2026-10-19)

| Item | Finding |
|---|---|
| Git history | Empty repository (no commits) before this work. |
| `v25_paper_bot.zip` | Extracted to `v25_paper_bot/`: `app.py` (FastAPI prototype), `README.md`, `requirements.txt`. Kept **unchanged** as the legacy reference. |
| Legacy behaviour (smoke-tested) | `GET /health`, `GET /status`, `POST /webhook/{token}` (path-token auth, 90 s timestamp window, event-id dedupe, 08:30–14:30 NY window, qty ≤ 2, risk ≤ $200, daily −$450, trailing floor 48 000 + $400 buffer, 2 trades/day), `POST /paper/mark/{token}?price=` (manual mark → stop/target exit). |
| Legacy gaps | No price feed, no OCO engine, no partial fills, no slippage/commission model besides a flat $8, no session-flatten, no reconciliation, `/status` unauthenticated, SQLite connections never closed, risk computed from signal price, not broker state. |
| Pine Script | **Not in the zip.** The complete V25 source was supplied in chat and saved verbatim to `pine/V25_original.pine`. It is structurally complete (sections 1–8, entry/exit logic, plots, diagnostics). It has not been compiled here — TradingView is the only Pine compiler. |

## 1. Architecture

```
TradingView V25 (alert() JSON) ──HTTPS──► Caddy (TLS) ──► FastAPI (v25bot.api)
                                                           │
           ┌───────────────────────────────────────────────┤
           ▼                                               ▼
   Webhook gate                                     Admin API (Bearer)
   path token · optional passphrase · IP allowlist   kill / resume / flatten /
   size limit · strict schema · timestamp window      reconcile / status
   persistent event_id (SQLite, replay-safe)
           │
           ▼
   ExecutionEngine ── ContractResolver (MNQ1! → MNQZ6)
           │       ── RiskEngine (broker-sourced account state only)
           │       ── Broker (PaperBroker | TradovateBroker[disabled])
           │       ── ProtectionGuard (stop must be working, else flatten + halt)
           ▼
   Monitor loop: reconciliation · connectivity · stale prices ·
                 session flatten · kill-switch file · alerts
```

## 2. Modules

| Module | Responsibility |
|---|---|
| `v25bot/config.py` | Env-var settings, `*_FILE` secret support, `SecretStr`, live-trading gates (`PAPER_ONLY=true` default). |
| `v25bot/logging_setup.py` | JSON logs + redaction filter. |
| `v25bot/store.py` | SQLite (WAL): events, orders, fills, audit, key/value state. |
| `v25bot/contracts.py` | MNQ month codes, 3rd-Friday expiry, roll schedule, TradingView/Tradovate symbol mapping. |
| `v25bot/market_data.py` | Quote cache, staleness, bar → price path. |
| `v25bot/brokers/base.py` | Broker ABC, order/fill/position/account models, error types. |
| `v25bot/brokers/paper.py` | Simulator: market/limit/stop, OSO brackets, OCO, partial fills via book depth, slippage, gaps, commissions, rejects, disconnects, persistence. |
| `v25bot/brokers/tradovate.py` | Documented Tradovate REST endpoints only; refuses to start unless explicitly enabled; prop/evaluation accounts documented as ineligible. |
| `v25bot/risk.py` | Independent pre-trade checks and halt conditions. |
| `v25bot/engine.py` | Signal handling, protection enforcement, flatten, kill switch, reconciliation. |
| `v25bot/monitor.py` | Background supervision loop. |
| `v25bot/alerts.py` | Log + optional webhook (Slack/Discord/generic) alerts with redaction. |
| `v25bot/api.py` | FastAPI app, TradingView webhook, legacy-compatible routes, admin routes. |

## 3. Safety invariants (enforced in code and covered by tests)

1. `PAPER_ONLY=true` by default. Live routing requires `PAPER_ONLY=false` **and** `LIVE_TRADING_ENABLED=true` **and** `LIVE_TRADING_ACK` set to an exact acknowledgment phrase **and** `TRADOVATE_ENABLED=true` **and** a non-prop account confirmation. Any missing gate → engine starts in paper mode or refuses to start.
2. No order is sent unless the broker returns a fresh, verified account snapshot and positions list.
3. Every entry is an OSO bracket with a broker-side stop. After an entry fill, the guard verifies a working stop covering the full position. If it is missing or rejected → immediate flatten + persistent halt + alert.
4. Risk is computed from broker fills, positions, and balances, never from TradingView equity.
5. Kill switch and halts persist across restarts and require an explicit authenticated resume.
6. Secrets never appear in logs, responses, or `repr()`.

## 4. Pine Script changes

Only additive alert/telemetry code. No signal, sizing, stop, or target logic changed. See `docs/PINE_CHANGES.md`.

## 5. Test plan

Duplicate/replayed webhooks, auth failures, schema violations, partial fills, rejected entries and rejected stops, slippage and gaps, OCO cancellation, commissions, disconnects, stale prices, session cutoff and auto-flatten, every risk limit, contract roll/expiry, kill switch persistence, reconciliation mismatches, secret redaction, live-mode gating.

## 6. Out of scope / not claimed

- Live readiness. No end-to-end execution against a real broker has been performed.
- A licensed real-time market-data feed. Paper fills use TradingView bar updates or prices you push. That's coarser than tick data and is documented as such.
