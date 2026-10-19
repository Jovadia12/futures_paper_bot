# V25 Execution Engine (paper trading)

Automated execution for the **ICT FVG V25** TradingView strategy on MNQ futures:

```
TradingView V25 (alert() JSON) ─HTTPS─► Caddy (TLS) ─► FastAPI engine ─► Broker adapter
                                                         ├ webhook gate (token, IP allowlist, schema, replay)
                                                         ├ contract resolver (MNQ1! → MNQZ6)
                                                         ├ independent risk engine (broker state only)
                                                         ├ protection guard (stop required, else flatten + halt)
                                                         └ monitor (reconcile, connectivity, 14:30 cutoff, kill switch)
```

> **Status: PAPER ONLY. Not live ready.** `PAPER_ONLY=true` is the default, and while it is set the engine refuses any broker except the built-in paper simulator. No order has ever been sent to a real broker by this code. Live use would require a verified end-to-end test against a broker this account is actually permitted to automate, and Lucid evaluation accounts are **not** eligible for Tradovate API access (see [docs/TRADOVATE_LUCID.md](docs/TRADOVATE_LUCID.md)).
>
> A backtested profit factor is not a forecast. Paper results here also use 1-minute bar prices, which are coarser than a real tick feed.

---

## Contents

1. [Repository layout](#repository-layout)
2. [Local development](#local-development)
3. [Connect your TradingView strategy (step by step)](#connect-your-tradingview-strategy)
4. [Docker deployment with HTTPS](#docker-deployment-with-https)
5. [Operations: kill switch, flatten, resume, alerts](#operations)
6. [How it works](#how-it-works)
7. [Configuration reference](#configuration-reference)
8. [Webhook API reference](#webhook-api-reference)
9. [Testing](#testing)
10. [Before anything live](#before-anything-live)

---

## Repository layout

| Path | Purpose |
|---|---|
| `pine/V25_original.pine` | Your V25 script, verbatim (reference). |
| `pine/V25_webhook.pine` | V25 + machine-readable alerts. **Strategy logic unchanged**; see [docs/PINE_CHANGES.md](docs/PINE_CHANGES.md). |
| `v25bot/` | Execution engine (FastAPI). |
| `v25bot/brokers/paper.py` | Paper broker simulator. |
| `v25bot/brokers/tradovate.py` | Tradovate adapter: documented API only, **disabled**, unverified against real servers. |
| `tests/` | 183 automated tests, including an end-to-end HTTP drill. |
| `v25_paper_bot/` | The original prototype from `v25_paper_bot.zip`, kept unchanged for reference. Its routes and payload still work through the new engine. |
| `docs/` | Implementation plan, Pine change log, Tradovate/Lucid findings. |

---

## Local development

Requires Python 3.11+.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt

export WEBHOOK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ADMIN_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m v25bot                     # http://127.0.0.1:8000 (docs at /docs in paper mode)
```

In a second shell (same exported tokens):

```bash
python -m v25bot.admin status
# Simulated entry (must be inside 08:30–14:30 NY on a weekday or it is rejected, by design):
python -m v25bot.admin test-entry --side buy --price 20000 --stop 19980 --target 20060 --contract MNQZ2026
# Push a price (or a bar) to drive the simulator:
python -m v25bot.admin quote --last 20061
python -m v25bot.admin test-bar --open 20000 --high 20061 --low 19995 --close 20050
python -m v25bot.admin flatten
python -m v25bot.admin kill --reason "drill"
python -m v25bot.admin resume
```

Run the tests:

```bash
python -m pytest -q
```

---

## Connect your TradingView strategy

TradingView requirements (check your plan's current terms): webhook alerts need a **paid plan** and **two-factor authentication** enabled on the account. Webhooks are sent only to ports **80/443**, must be answered within about **3 seconds**, and come from these IPs: `52.89.214.238`, `34.212.75.30`, `54.218.53.128`, `52.32.178.7`.

**1. Deploy the engine on a public HTTPS domain.** Follow [Docker deployment](#docker-deployment-with-https). Confirm from your own machine:
```bash
curl https://YOUR_DOMAIN/health        # {"status":"ok","mode":"PAPER_ONLY",...}
```

**2. Add the webhook script.** In TradingView open **Pine Editor → New → Strategy**, paste the whole of `pine/V25_webhook.pine`, **Save**, then **Add to chart**.

**3. Use the right chart.** Symbol **`CME_MINI:MNQ1!`** (or an explicit contract such as `MNQZ2026`), timeframe **1 minute**. The engine rejects entries from any other timeframe or instrument (it checks tick size $0.25 and point value $2).

**4. Verify the strategy is unchanged.** Add `pine/V25_original.pine` to the same chart with identical inputs and compare the Strategy Tester results. They must be identical. If they differ, stop.

**5. Set inputs** (strategy ⚙ → Inputs):
* **5. Position Sizing → Sizing mode:** `Prop Conservative` (V22 Baseline needs ≥ 5 contracts, which the default engine limits reject).
* **Maximum contracts** ≤ engine `MAX_CONTRACTS` (default 2).
* **Prop maximum planned risk USD** ≤ engine `MAX_TRADE_RISK_USD` (default 200).
* **9. Webhook Automation → Bot id:** must equal `ALLOWED_BOT_IDS` (default `V25-MNQ-1`).
* **Optional body passphrase:** blank, unless you set `WEBHOOK_PASSPHRASE` on the server.
* Leave **Emit webhook alert() JSON**, **Emit 1m bar prices**, and **Emit TradingView exit / flatten events** on.

**6. Create the alert.** Alerts (⏰) → **Create alert**:
* **Condition:** `ICT FVG V25 | V22 Core + Sweep Test + Prop Mode` → **`alert() function calls only`**
* **Expiration:** open-ended
* **Message:** leave as is. It is ignored, because the script writes the JSON.
* **Notifications → Webhook URL:**
  ```
  https://YOUR_DOMAIN/webhook/tradingview/YOUR_WEBHOOK_TOKEN
  ```
  (`YOUR_WEBHOOK_TOKEN` = contents of `secrets/webhook_token`). Treat this URL as a password.
* **Create.**

> TradingView alerts snapshot the script and inputs at creation time. **Whenever you change inputs or the script, delete and recreate the alert.**

**7. Confirm traffic.** Within a minute during market hours, bar events should arrive:
```bash
docker compose exec engine python -m v25bot.admin status   # "tv_last_seen" updates every minute
docker compose logs -f engine                              # JSON logs; tokens are redacted
```
Entries are accepted only 08:30–14:30 New York time on weekdays, and positions are flattened at 14:30.

**8. Let it paper trade.** Review `/admin/fills`, `/admin/events`, and alerts daily. Compare fills and trade count with the TradingView Strategy Tester for the same days.

---

## Docker deployment with HTTPS

Requires Docker with Compose, a host with ports 80/443 open, and a DNS `A` record for your domain pointing at it.

```bash
cp .env.example .env                  # edit DOMAIN, limits, ALERT_WEBHOOK_URL
mkdir -p secrets && chmod 700 secrets
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/webhook_token
python3 -c 'import secrets; print(secrets.token_urlsafe(32))' > secrets/admin_token
chmod 600 secrets/*
docker compose up -d --build
docker compose logs -f
```

* **Caddy** obtains and renews the TLS certificate automatically, exposes only `/webhook/*` and `/health`, and removes URIs (which contain the token) from its access log.
* The admin API is **not** exposed publicly. Use `docker compose exec engine python -m v25bot.admin …`.
* Compose **forces** `PAPER_ONLY=true`, `BROKER=paper`, `TRADOVATE_ENABLED=false`, `LIVE_TRADING_ENABLED=false`, overriding `.env`.
* State (SQLite: events, orders, fills, halts, paper account) lives in the `engine-data` volume and survives restarts.

The Docker image and compose stack were written but **not built or run in the development sandbox** (no Docker daemon was available there). Run `docker compose config` and a first `docker compose up` yourself and check `/health`.

---

## Operations

| Action | How |
|---|---|
| Status | `python -m v25bot.admin status` (`GET /status`) |
| **Kill switch** (flatten everything, block trading) | `python -m v25bot.admin kill --reason "…"`, or create the file `KILL_SWITCH_FILE` (`docker compose exec engine touch /data/KILL_SWITCH`) |
| Emergency flatten (no halt) | `python -m v25bot.admin flatten` |
| Resume after a halt | `python -m v25bot.admin resume`. It refuses while the kill-switch file exists, the broker is disconnected, reconciliation fails, or a position lacks a stop. |
| Force reconciliation | `python -m v25bot.admin reconcile` |
| Paper fault drills | `POST /paper/simulate` `{"action":"disconnect"\|"reconnect"\|"reject_next","role":"entry\|stop_loss\|take_profit\|flatten"}` |

**Halts.** `day` halts (daily loss limit) clear automatically on the next trading day. `account` (drawdown, failed flatten), `protection` (unprotected fill), `reconcile` (state mismatch), `manual`, and `kill_switch` halts persist across restarts and need an explicit resume.

**Alerts** go to the JSON log, the `audit` table, and optionally `ALERT_WEBHOOK_URL` (Slack, Discord, or generic JSON). Critical alerts include: unprotected position, flatten failed (manual intervention required), kill switch, and drawdown halt. Warnings include: rejected entries, broker disconnect/reconnect, stale prices with an open position, TradingView silence (`TV_HEARTBEAT_MAX_AGE_SECONDS`), and contract-roll notices.

---

## How it works

### Webhook gate (`v25bot/api.py`)
1. **Token** in the URL path, compared in constant time. TradingView can't send custom headers or sign requests, so the path token is the primary credential. The optional `WEBHOOK_PASSPHRASE` adds a second factor in the body.
2. **IP allowlist** (`ENFORCE_IP_ALLOWLIST=true`): TradingView's four published IPs. `X-Forwarded-For` is honoured only from `TRUSTED_PROXIES`.
3. **Size limit** 8 KB, JSON only (both `application/json` and `text/plain` content types are accepted).
4. **Strict schema** `v25.webhook.v1`: unknown fields are rejected, prices must be finite, and the bracket must be on the correct sides.
5. **Replay protection:** `sent_at` must be within `MAX_SIGNAL_AGE_SECONDS` (90 s) and no more than 15 s in the future. Every trade event ID is stored in SQLite **before** processing. A repeat returns the original result (HTTP 200, `duplicate: true`), and reusing an ID with a different payload returns HTTP 409. Rejected events stay rejected on replay.
6. **Version pinning:** `strategy_version` must be in `ALLOWED_STRATEGY_VERSIONS` and `bot_id` in `ALLOWED_BOT_IDS`.

### Contract resolution (`v25bot/contracts.py`)
`CME_MINI:MNQ1!` → front quarterly month (H/M/U/Z, expiry on the 3rd Friday at 09:30 ET, roll 8 days before). The Pine script sends `syminfo.current_contract`, and the engine trades **that** contract, because the stop and target prices came from its data. During roll week without that field, entries are refused, since chart prices might belong to the other month. Contracts within `MIN_DAYS_TO_EXPIRY` of expiry are refused.

### Risk engine (`v25bot/risk.py`): broker state only
Every entry needs a fresh, verified broker account snapshot, positions, and open orders. If any of them can't be read, the order is rejected. Checks, in order:
halts/kill switch → broker connected & reconciled → snapshot verified and < 10 s old → weekday, not a holiday, inside 08:30–14:30 NY → fresh price → flat with no working orders (V25 runs `pyramiding=0`) → trades today < `MAX_TRADES_PER_DAY` → stop/target on the tick grid and on the correct side of the **live market** → market within `MAX_ENTRY_DEVIATION_TICKS` of the signal → qty ≤ `MAX_CONTRACTS` → risk ≤ `MAX_TRADE_RISK_USD` → worst-case loss keeps day P&L above `-DAILY_LOSS_LIMIT_USD` → worst-case equity stays above the drawdown floor + buffer.

Risk per contract = |estimated fill − stop| × $2 + `COST_RESERVE_PER_CONTRACT_USD`, where the estimated fill is the **broker-side market price** plus `RISK_ENTRY_SLIPPAGE_TICKS`. The alert's `planned_risk` and TradingView's equity are never used. Day P&L is the worse of broker-reported realised P&L and P&L rebuilt from the recorded fills. The trailing EOD drawdown floor is `min(start, peak EOD balance − MAX_DRAWDOWN_USD)`; static and intraday-trailing modes are also available.

### Protection guard (`v25bot/engine.py`)
Every entry is an OSO bracket: a market entry plus an OCO stop-loss and take-profit at the broker. After the entry fills, the engine checks that working stop orders cover the full position. If not (stop rejected, cancelled, or partially covering), it **immediately flattens, sets a persistent `protection` halt, and raises a critical alert**. The monitor repeats this check every second for every open position.

### Monitor (`v25bot/monitor.py`)
Each cycle: kill-switch file → broker connectivity (reconnect, then reconcile before any entry) → reconciliation every 5 s (broker positions vs fills, unknown orders, orphaned exits) → daily-loss/drawdown breach (halt + flatten) → **flatten anything held outside 08:30–14:30 NY** → stale-price alerts → TradingView silence → protection check.

### Paper broker (`v25bot/brokers/paper.py`)
Market, limit, and stop orders; OSO brackets whose exits grow with partial entry fills; OCO with reduce-only exits; book-depth partial fills at progressively worse prices; adverse slippage (default 2 ticks, matching the V25 backtest); stops gapped through fill at the worse price; limits need a trade-through; $1.50/contract/side commission (matching the backtest); rejections; client disconnects, during which server-side stops keep working; persisted state.

**Price source.** By default, the 1-minute `bar` alerts from TradingView. A bar is expanded conservatively (for a long position O→L→H→C), so when a stop and target are both inside one bar, **the stop fills first**. You can also push quotes from any feed you are licensed to use via `POST /paper/quote`.

---

## Configuration reference

All settings are environment variables. See [`.env.example`](.env.example) for the full annotated list. Secrets accept `NAME_FILE=/path`.

| Variable | Default | Meaning |
|---|---|---|
| `PAPER_ONLY` | `true` | Only the paper broker may run. |
| `WEBHOOK_TOKEN` / `ADMIN_TOKEN` | – | Required, ≥ 24 chars. |
| `ENFORCE_IP_ALLOWLIST` | `false` (compose example `true`) | Restrict webhooks to TradingView IPs. |
| `MAX_CONTRACTS` | 2 | Per order and position. |
| `MAX_TRADE_RISK_USD` | 200 | Worst-case loss per trade incl. cost reserve. |
| `RISK_SIZING_MODE` | `reject` | `reduce` shrinks qty instead (changes sizing vs backtest). |
| `DAILY_LOSS_LIMIT_USD` | 450 | Per CME trading day (rolls 17:00 NY). |
| `MAX_DRAWDOWN_USD` / `DRAWDOWN_SAFETY_BUFFER_USD` | 2000 / 400 | Drawdown floor and buffer. |
| `DRAWDOWN_MODE` | `trailing_eod` | Or `intraday_trailing`, `static`. |
| `MAX_TRADES_PER_DAY` | 2 | Matches V25 default. |
| `ENTRY_WINDOW_START/END`, `FLATTEN_TIME` | 08:30 / 14:30 / 14:30 | New York time. |
| `HOLIDAYS` | – | `YYYY-MM-DD,…` dates with no trading. |
| `MAX_QUOTE_AGE_SECONDS` | 90 | Stale-price threshold. |
| `TV_EXIT_ACTION` | `flatten` | On a TradingView exit event while the broker still has a position. |
| `PAPER_SLIPPAGE_TICKS` / `PAPER_COMMISSION_PER_CONTRACT` / `PAPER_BOOK_DEPTH` | 2 / 1.50 / 5 | Simulator. |

The defaults deliberately mirror the original prototype's limits (2 contracts, $200 risk, −$450/day, $2,000 trailing drawdown with $400 buffer, 2 trades/day).

---

## Webhook API reference

`POST /webhook/tradingview/{token}` (`/webhook/{token}` is kept for the legacy prototype)

| Event | Required fields (beyond the common ones) | Effect |
|---|---|---|
| `entry` | `side`, `qty`, `order_type:"market"`, `price`, `est_fill`, `stop`, `target`, `tick_size`, `point_value`, `risk_per_contract`, `planned_risk`, `sizing_mode` | Risk-checked bracket order |
| `exit` | `exit_price` (+ ids, qty, profit) | TradingView's simulated trade closed; flatten if the broker still holds a position |
| `flatten` | `reason` (`session_flat`/`prop_risk_halt`/`manual`) | Flatten all |
| `bar` | `open`, `high`, `low`, `close` | Drives paper fills (not persisted as an event; must be newer than the last bar). Ignored as market data for non-paper brokers. |
| `heartbeat` | – | Liveness |

Common fields: `schema`, `strategy`, `strategy_version`, `bot_id`, `event`, `event_id`, `symbol`, `contract`, `timeframe`, `bar_time`, `sent_at` (ms), `passphrase`.

Responses: `200` accepted, processed, or duplicate · `403` bad token/IP/passphrase · `409` rejected by risk, session, contract, or broker, or `event_id` conflict · `413` too large · `422` invalid schema, stale, or wrong chart · `503` webhook disabled or broker error.

The legacy prototype payload `{event_id, symbol, action, qty, entry, stop, target, timestamp}` is still accepted when `ALLOW_LEGACY_SCHEMA=true` (the code default; the `.env.example` sets it to `false`). Other differences from the prototype: `/status` now requires the admin token, and `/paper/mark/{token}?price=` now marks the paper broker's quote.

---

## Testing

```bash
python -m pytest -q
```

| File | Covers |
|---|---|
| `test_webhook_api.py` | Auth, IP allowlist and proxies, size limits, schema violations, **duplicate webhooks**, ID conflicts, **replayed/stale signals**, passphrase, legacy routes, secret redaction |
| `test_paper_broker.py` | **Slippage**, commissions, **partial fills**, OCO, gaps, trade-through limits, **rejected orders**, **disconnects**, persistence |
| `test_risk.py` | Every limit, **session boundaries**, holidays, **stale prices**, unverifiable account state, **risk breaches** |
| `test_engine.py` | Unprotected-fill liquidation and halt, failed flatten, kill switch (API and file, persistent), **session cutoff**, reconnect/reconcile, mismatches, TradingView exit/flatten, roll-window entries, restart |
| `test_contracts.py` | Expiries, **contract rollover**, continuous/explicit symbols, expiry guards |
| `test_safety_and_tradovate.py` | PAPER_ONLY gates, prop-account refusal, live gates, secret files and redaction, Tradovate request shapes, CAPTCHA refusal |
| `test_pine.py` | Webhook script only adds lines; trading calls unchanged; emitted JSON keys match the schema |
| `test_e2e_http.py` | Real uvicorn server + monitor: entry → duplicate → target → stop gap → trade limit → 14:30 cutoff → kill/resume → outage/reconnect |

---

## Before anything live

This system is a **paper** execution engine. Do not route real orders until **all** of these are true:

1. A broker/account where automated API trading is **explicitly permitted** for your account type. Lucid evaluation accounts via the Tradovate API are not; see [docs/TRADOVATE_LUCID.md](docs/TRADOVATE_LUCID.md).
2. The adapter passes a recorded end-to-end test in that broker's demo/sim environment: entry, bracket, partial fills, stop-out, target, flatten, kill switch, disconnect and reconnect, reconciliation.
3. A real-time market-data feed you are licensed for (Tradovate's API add-on does not include market data).
4. Several weeks of paper results reviewed against the backtest.
5. Every gate set deliberately: `PAPER_ONLY=false`, `LIVE_TRADING_ENABLED=true`, `LIVE_TRADING_ACK` = the exact phrase in `v25bot/config.py`, `TRADOVATE_ENABLED=true`, `TRADOVATE_ACCOUNT_IS_PROP_OR_EVALUATION=false`, `ALLOW_LEGACY_SCHEMA=false`. The compose file also overrides these back to paper and must be changed on purpose.
