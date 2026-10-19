# V25 paper-only gateway (prototype)

**No real brokerage adapter is included. This cannot place live orders.**

## Run locally

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
export WEBHOOK_TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Use `http://127.0.0.1:8000/docs` to inspect endpoints. Do not expose this prototype directly to the internet. Deploy only behind HTTPS and a controlled ingress, after adding replay protection, request signing, and monitoring.

### Example test signal

Use an actual current Unix timestamp; example entry and bracket values are illustrative, not market data. Entries only accepted 08:30–14:30 America/New_York on weekdays. Qty max 2; estimated risk max $200.

```bash
curl -X POST "http://127.0.0.1:8000/webhook/$WEBHOOK_TOKEN" \
  -H 'content-type: application/json' \
  -d "{\"event_id\":\"test-$(date +%s)-12345\",\"symbol\":\"MNQ\",\"action\":\"buy\",\"qty\":1,\"entry\":20000,\"stop\":19975,\"target\":20075,\"timestamp\":$(date +%s)}"
```

This is an **event ingestion and simplified paper accounting prototype**, not a complete broker simulator. `POST /paper/mark/{token}?price=...` requires an external price source. No price feed, automatic marking, tick-by-tick fill model, OCO engine, market calendar, or session-end flatten scheduler is bundled. Do not interpret its reported P&L as a valid forward test until those are implemented.

## Production checklist
- Patch V25 to emit authenticated machine-readable entry intent with original third-candle stop and liquidity target. Existing strategy-fill alerts alone omit those prices.
- Use unique per-order IDs and durable queue, idempotency, timeout and replay protection.
- Add exchange-approved real-time data feed, tradable front-month MNQ symbol mapping, and contract rollover.
- Implement a realistic paper broker with bid/ask, commissions, latency, partial fills, gap/slippage, stop/target OCO, and flattening.
- Use actual Lucid account-state API and authoritative drawdown calculations after connection confirmed.
- Broker-side bracket protection, kill switch, reconciliation, disconnection fail-closed behavior.
- No live trading until broker connectivity, permission, risk, and acceptance tests pass.
