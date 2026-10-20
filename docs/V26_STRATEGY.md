# V26 strategy (pine/V26_webhook.pine)

**Status: written and tested statically; not deployed.** V25 is unchanged and remains what the running bot accepts.

* The production engine's default `ALLOWED_STRATEGY_VERSIONS=V25-webhook.1` rejects every V26 alert with an explicit reason.
* V26 is meant to be tested first in the separate staging stack (`docker-compose.staging.yml`, ports 8001/8081). See [Staging](#5-staging-test-without-touching-v25).

> **Needs your confirmation before finalising: the stop anchor.** See [section 2](#2-needs-your-confirmation).

---

## 1. Rules implemented

### Why V26 exists (what V25 did differently)

| Topic | V25 | V26 |
|---|---|---|
| HTF timeframes | 15m and 1h only | 5, 10, 15, 30, 45, 60, 120, 240 min (each can be switched off) |
| Zones tracked | newest zone per side only | every unmitigated zone, including overlapping and cross-timeframe ones |
| Opposing 1m FVG | first one formed after the tap | **most recent** uninverted one, formed before, during or after the tap |
| Extra confirmations | always on (1h EMA, sweep, ATR body, 3-bar structure, target availability, formation window) | **Core**: none. **Filtered**: each one is a separate switch |
| Stop | candle-3 low − 2 ticks | from the inverted gap's three stored candles (two anchors, see section 2) |
| Target | fixed R multiple | nearest confirmed, untapped swing high/low with R ≥ 2. If none, no trade |
| Size | 3–7 contracts in "V22 Baseline", which the engine rejects (max 2) | same formula and limits as the engine: max 2 contracts, $200 including costs. If 1 contract doesn't fit, no trade |

### HTF FVG zones

* **Bullish FVG:** three completed HTF candles where candle-3 low > candle-1 high. The zone is [candle-1 high, candle-3 low]. Bearish mirrors it.
* **Minimum size:** 4 ticks (input).
* **No repainting and no lookahead.** `request.security(..., lookahead_on)` reads only offsets `[1]` and `[3]`, i.e. HTF candles that have already closed.
  * A zone becomes known on the first 1-minute bar of the following HTF candle and never changes after that.
  * Each HTF candle is evaluated once (deduplicated by its time).
* **Overlap:** zones are independent records. A 15m zone inside a 4H zone is tracked twice. When one bar taps several zones, the setup records the **highest** timeframe tapped. All tapped zones are removed (mitigated).
* **Lifecycle.** A zone leaves the list when it is:
  * **tapped:** a confirmed 1m bar trades into it. This arms a setup if the context allows it; otherwise the tap is logged as `not_armed` with the reason. Either way, the zone is now mitigated.
  * **invalidated:** a 1m bar closes beyond the zone's far edge (below a bullish zone, above a bearish one). This takes priority over a touch on the same bar.
  * **expired:** older than 96 h (input; 0 = never).
  * **evicted:** more than 300 zones stored (oldest first).

### Setup → opposing FVG → inversion

1. **Tap.** A tap arms the setup for its direction (long on a bullish zone). Arming requires:
   * a 1m chart;
   * time inside 08:30–14:30 NY and before the flatten time;
   * flat, with no previous trade still active;
   * fewer than 2 trades today;
   * the prop budget not halted (prop mode only).

   A new tap re-arms and resets the setup.
2. **Disarm.** The setup disarms when the tapped zone is closed through, 60 bars after the tap (input; 0 = at session end), or at session end.
3. **Opposing 1m FVG.**
   * **For a long:** a bearish three-candle gap, candle-3 high < candle-1 low, at least 2 ticks.
   * **Selection:** the **most recent** one that has not yet been inverted, provided it formed no earlier than 60 bars before the tap (input; 0 = no limit).
   * **Re-selection:** if a newer opposing gap forms, it replaces the selection.
4. **Inversion.** A confirmed 1m close above that gap's top (long). Shorts mirror this: bullish gap, close below its bottom.
5. **Timing.** A gap is added to the list only after the entry decision of the bar that completes it, so it can never be "inverted" on its own bar.

### Variants

* **Core:** steps 1–4 only. No other confirmation.
* **Filtered:** Core plus each enabled filter. Defaults are all on:
  * 1h EMA(50) trend
  * a liquidity sweep within 30 bars
  * inversion body ≥ 0.35 × ATR(14)
  * a close beyond the last 3 bars
  * the candle closing in the trade direction

### Stop-loss

* **Source.** The stop comes from the specific inverted gap's three stored candles (times, highs and lows of candles 1, 2 and 3 are kept explicitly).
* **Rounding.** It is rounded to the instrument tick (0.25 MNQ).
* **Never moved.** It is set once at entry: `tradeStop` is only assigned at entry or cleared after the trade. A test enforces this.
* **Buffer.** Optional extra ticks, default 0. They only move the stop **away** from price.
* **Sanity skip.** Skip the trade if the stop distance is over 10 × ATR (input; 0 = off).

### Take-profit

* **Swing levels.**
  * Confirmed swing highs and lows (5 bars each side, input) are stored with their time.
  * A level is removed the first time price trades at or through it.
  * A wick through that closes back is also recorded as a **sweep**.
* **Choice.** The target is the **nearest** stored level beyond the estimated fill with 2 ≤ R ≤ 12 (inputs; the minimum cannot be set below 2).
* **No target, no trade.** If no level qualifies, there is no trade, and the reason is recorded with the nearest level's R. No fixed-R fallback exists.
* **Stop/target ambiguity.**
  * Both TradingView's emulator and the engine's paper broker assume the **stop fills first** when one bar touches both.
  * TradingView decides using its own intrabar path assumptions.

### Size (identical to the engine)

```
est. fill          = close ± RISK_ENTRY_SLIPPAGE_TICKS (2) × tick
risk per contract  = |est. fill − stop| × point value ($2) + COST_RESERVE_PER_CONTRACT_USD ($8)
qty                = min(MAX_CONTRACTS (2), floor(MAX_TRADE_RISK_USD ($200) / risk per contract))
qty < 1            → no trade ("size: 1 contract risks $X > $200 cap")
```

The four engine values are inputs in section 7 of the script.

* **Recorded in the alert.** Every entry alert sends the values it used as `caps`.
* **Must match.** The engine rejects the alert unless `caps` equals its own settings.
* **No silent resizing.** In V26, neither side ever shrinks or raises a size silently.
* **Prop budget (optional).** "Engine limits + prop budget" can only lower the size.

---

## 2. Needs your confirmation

### Stop anchor (your rule is ambiguous; please choose)

Your wording: *"bottom boundary of the inverted bearish 1-minute FVG, corresponding to the lower extreme of its three-candle structure … Do not confuse the FVG's boundary with the third candle's full wick extreme."*

Worked example (a long; the bearish gap being inverted):

| | High | Low |
|---|---|---|
| Candle 1 | 20010.00 | **20005.00** ← gap top |
| Candle 2 | 20006.00 | 19996.00 |
| Candle 3 | **20000.00** ← gap bottom | 19994.00 |

| Input "Stop anchor" | Long stop in the example | Meaning |
|---|---|---|
| **FVG boundary** | **20000.00** | the gap's bottom edge = candle-3 **high** |
| **Three-candle extreme** (current default) | **19994.00** | lowest low of candles 1–3. In a down-move this is usually candle 3's low, i.e. "the third candle's full wick extreme" |

* **Why it's ambiguous.** The first half of your sentence ("lower extreme of its three-candle structure") describes 19994. The warning ("do not confuse the FVG's boundary with the third candle's full wick extreme") suggests 20000. In most setups the two differ by several points, which changes size, R:R and which targets qualify.
* **Both are implemented.**
  * The alert declares the anchor used (`stop_anchor: boundary | structure`).
  * The engine verifies the stop against exactly that rule: for a long, the lowest low of all three candles (not just candle 3), or the gap bottom.
* **Default for now.** "Three-candle extreme" is a placeholder until you answer. **Tell me which one is correct** (or a third definition) and I will make it the only option.

### Other choices I made that you may want to change

| Item | Current value | Note |
|---|---|---|
| Opposing gap may precede the tap by | 60 bars | 0 = any age |
| Setup lifetime after the tap | 60 bars | 0 = until session end |
| Tap outside the entry window | zone is mitigated, no setup | logged as `not_armed` |
| Several zones tapped by one bar | highest timeframe recorded | all are mitigated |
| Long and short inverted on the same bar | no trade | logged as `conflict` |
| Swing confirmation | 5 bars each side on 1m | larger = fewer, stronger levels |
| Maximum target R | 12 | 0 = no cap |
| Skip when stop > N × ATR | 10 | 0 = off |
| Minimum HTF / 1m gap | 4 / 2 ticks | |
| Filtered variant | all five filters on | each can be switched off |

---

## 3. Webhook payload and engine checks

V26 alerts use schema `v26.webhook.v1`, `strategy: "V26"`, `strategy_version: "V26-webhook.1"` and `bot_id: "V26-MNQ-1"`. The event shapes are the same as V25:

* **Entry events** also carry `caps` and `setup`. `setup` holds the zone, the tap, the three FVG candles, the stop anchor, the target time and R, and the 5 nearest candidate levels.
* **Bar events** also carry `diag`.

The engine (`v25bot/v26_integrity.py`) rejects a V26 entry with an exact reason unless **all** of these hold:

* `caps` equals the engine's MAX_CONTRACTS, MAX_TRADE_RISK_USD, COST_RESERVE_PER_CONTRACT_USD and RISK_ENTRY_SLIPPAGE_TICKS.
* The FVG candles are in order (c1 < c2 < c3) and closed before the entry bar.
* The gap boundaries equal the candles' highs and lows, and the gap is at least the minimum size.
* The entry close inverted the gap.
* The stop equals the declared anchor ± buffer, on the tick grid.
* `est_fill` and `risk_per_contract` match the engine's formula.
* The target swing was confirmed before the entry bar and offers R ≥ max(2, `MIN_REWARD_RISK`), and the reported R matches.
* The target is the nearest eligible listed candidate. If all 5 listed candidates are ineligible, it must lie beyond all of them.
* Then the normal risk engine applies, unchanged:
  * session, flat, max trades, quote freshness, tick grid;
  * size ≤ 2 contracts and ≤ $200 including costs;
  * daily loss and drawdown, all from broker state.

V25 payloads are validated exactly as before. A V25 payload carrying V26 fields is rejected, and the reverse too.

---

## 4. Diagnostics (dashboard → **Signals**)

Each 1m bar alert includes up to 20 diagnostic items.

**From the Pine script:**

* `htf_fvg`, `htf_invalid`, `tap`
* `armed`, `not_armed`, `disarm`
* `opp_selected`, `inversion`, `entry`
* `blocked` (with the exact reason), `sweep`

The script also sends the current state: zones per timeframe, and each side's armed setup with the selected opposing gap.

**From the engine, for every V26 entry alert:**

* `alert_received`
* `order_accepted` or `order_rejected` (with the engine's exact reason text)

**Storage and display:**

* Stored in a new `diagnostics` table in the engine's database. It is additive: existing tables and history are untouched. Rows are deduplicated per bar and pruned after `DIAGNOSTICS_RETENTION_DAYS` (14).
* Served read-only at `GET /admin/diagnostics`.
* The Signals page shows the funnel, "why entries did not happen", the current setup state and a filterable timeline. On an engine without this endpoint, the page says so and the rest of the dashboard works as before.
* Diagnostics are stored only for allowlisted versions and bot ids. A stray V26 alert hitting the V25 engine stores nothing.

---

## 5. Staging test without touching V25

Run these in **Command Prompt** in the bot folder on the Windows PC. Nothing below stops, rebuilds or reconfigures the running V25 containers.

* **Separate stack.** `-p v25-staging` plus `name: v25-staging` keep staging in its own compose project, with its own containers, image, network and database volume.
* **Production keeps running its current image** until you explicitly rebuild it. Do **not** run `docker compose up -d --build` without `-f docker-compose.staging.yml` until you decide to cut over.

```bat
cd /d C:\path\to\futures_paper_bot
git fetch origin claude/eloquent-einstein-fq3gvf
git status
git pull origin claude/eloquent-einstein-fq3gvf

docker compose -p v25-staging -f docker-compose.staging.yml up -d --build
docker compose -p v25-staging -f docker-compose.staging.yml ps
curl.exe -s http://127.0.0.1:8001/health
curl.exe -s http://127.0.0.1:8000/health

start "cloudflared-staging" /min cloudflared tunnel --url http://localhost:8001
```

* **`git status`:** it should show nothing modified. `.env` and `secrets\` are not tracked.
* **The two health checks:** 8001 is staging and should show `"paper_only":true`. 8000 is production and should be unchanged and still answering.
* **The tunnel window** prints a new `https://<words>.trycloudflare.com` address. That is the **staging** URL. The V25 tunnel keeps its own address.

Staging dashboard: http://127.0.0.1:8081 (same password). Check what staging accepted:

```bat
set /p ADMIN=<secrets\admin_token
curl.exe -s -H "Authorization: Bearer %ADMIN%" "http://127.0.0.1:8001/admin/diagnostics?limit=20"
curl.exe -s -H "Authorization: Bearer %ADMIN%" "http://127.0.0.1:8001/admin/events?limit=20"
docker compose -p v25-staging -f docker-compose.staging.yml logs --tail 50 engine
```

Remove staging completely (production unaffected):

```bat
docker compose -p v25-staging -f docker-compose.staging.yml down -v
```

---

## 6. TradingView

### Add the script (keeps V25 untouched)

1. Open a **1-minute** `CME_MINI:MNQ1!` chart. Use a separate chart tab or layout, so the V25 chart and its alert stay as they are.
2. **Pine Editor → Open → New strategy**. Select everything, delete it, and paste the full contents of `pine/V26_webhook.pine`.
3. **Save as** `ICT FVG V26 webhook` (a *new* script; do not overwrite your V25 script), then **Add to chart**.
4. Compilation must succeed with no errors. If TradingView reports an error, send me the exact message and line number.
5. Open the strategy's **Settings → Inputs** and check:
   * "Strategy variant" (Core or Filtered)
   * "Stop anchor" (see section 2)
   * section 7 = 2 / 200 / 8 / 2, which must match the bot's `.env`
   * "Bot id" = `V26-MNQ-1`

### Check it in the Strategy Tester (historical simulation only)

* **Chart markers:**
  * triangles = setup armed by an HTF tap
  * purple/orange line = the selected opposing FVG's inversion level
  * label = entry with stop/target/R
  * red/green lines = stop/target while in a trade
* **Diagnostics table** (top right): zones, taps, selections, inversions, entries, and blocks by reason (context/filter/stop/target/size).
* **Spot-check about 10 trades** against your own reading of the rules, especially:
  * the stop against your preferred anchor;
  * that the target is the nearest untouched swing beyond 2R.

### V25 vs V26 comparison (historical; nothing was backtested in this repository)

1. On the same 1m MNQ chart and date range, add V25 and V26 one at a time.
2. For each, export **Strategy Tester → List of trades → Export**.
3. Then run:

   ```bat
   python tools\compare_tv_trades.py V25_trades.csv V26_core_trades.csv V26_filtered_trades.csv
   ```

The output is labelled *historical simulation*. TradingView's 1m history depth limits the range. Past results do not predict future performance.

### Create the webhook alert (staging)

1. **Alerts (clock icon) → Create alert**:
   * Condition: **ICT FVG V26 webhook** → **alert() function calls only**
   * Expiration: open-ended, if your plan allows
   * Alert name: `V26 STAGING`
   * Notifications: tick **Webhook URL** and enter `https://<staging-words>.trycloudflare.com/webhook/tradingview/<contents of secrets\webhook_token>`
   * The message box is ignored; the script writes the JSON itself.
2. Create. Within about 1 minute (market hours) the staging dashboard's TradingView pill turns green, and **Signals** shows the current state.
3. **Any time you change an input or the script, delete and recreate this alert.** TradingView alerts keep a snapshot of the script and inputs from when they were created.

---

## 7. Migration plan (do not perform until we have verified V26 together)

**Now:** V25 production (engine :8000, its tunnel, its alert) is untouched. The V26 alert goes to staging only.

### Verification checklist (staging, several sessions)

- [ ] Script compiles; Strategy Tester trades follow the rules on manual review.
- [ ] You have confirmed the stop anchor (section 2).
- [ ] Staging receives bars every minute; **Signals** shows taps, selections and inversions that match the chart.
- [ ] Every entry alert appears as `alert_received`, then `order_accepted`, or `order_rejected` with an understood reason.
- [ ] No `does not match engine` or `V26 rule check` rejections. These mean the script and engine disagree and must be fixed, not ignored.
- [ ] Staging's paper stop/target equal the Pine stop/target lines; exits on the dashboard match TradingView.

### Cutover (only on your decision; outside 08:30–14:30 NY, with V25 flat)

1. **TradingView:** delete the **V25** webhook alert. Delete the **V26 STAGING** alert.
2. **`.env`:** set `ALLOWED_STRATEGY_VERSIONS=V26-webhook.1` and `ALLOWED_BOT_IDS=V26-MNQ-1`. Leave every risk value unchanged.
3. **Rebuild production** with the new code. The database volume and trade history are kept, and the diagnostics table is added automatically:
   `docker compose up -d --build engine dashboard`
4. **Check production:** `curl.exe -s http://127.0.0.1:8000/health`.
5. **TradingView:** create a new V26 alert (as in section 6) with the **production** tunnel URL. Name it `V26 PRODUCTION`.
6. **Stop staging:** `docker compose -p v25-staging -f docker-compose.staging.yml down`. Add `-v` only if you no longer need its history. Close the staging tunnel window.

### Rollback

1. Delete the V26 alert.
2. Restore `ALLOWED_STRATEGY_VERSIONS=V25-webhook.1` and `ALLOWED_BOT_IDS=V25-MNQ-1`.
3. Run `docker compose up -d engine dashboard`. The new code handles V25 alerts exactly as before.
4. Recreate the V25 alert from `pine/V25_webhook.pine`.

Never allow both versions in one engine: they would share one position, one trade counter and one daily loss limit.

---

## 8. What was and was not verified here

| Verified | How |
|---|---|
| Pine syntax | parsed by `pynescript` 0.3.0 (`PINE_PARSE=1 pytest tests/test_pine_v26.py`). It is not TradingView's compiler: type and runtime errors only show in TradingView. |
| Pine structure | static tests: the 8 timeframes; closed-bar-only HTF reads; explicit candle indexing; filters only in Filtered; stop/target set once; engine-limit defaults equal engine settings; every JSON key matches the engine schema; indentation rules |
| Engine pipeline | tests through the real FastAPI app and paper broker, using **synthetic** payloads:<br>• accepted long/short with both anchors<br>• caps mismatch<br>• bullish/bearish stop rules<br>• candle indexing<br>• multiple candidates<br>• insufficient R:R<br>• 1-contract risk over the cap<br>• duplicates<br>• intrabar stop/target ambiguity (stop first)<br>• diagnostics storage, dedupe and pruning<br>• production rejection of V26 |
| Dashboard | API tests, plus a headless-Chromium render of the Signals page at desktop and phone width |

**Not verified:**

* The script has **not** been run in TradingView.
* **No historical or live market data** was available in the build environment, so **no backtest or V25/V26 comparison was run**. No profitability claim is made.
* All engine test prices are synthetic.
