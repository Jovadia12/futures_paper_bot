# V25 Dashboard

A read-only monitoring website for the V25 paper bot.

| | |
|---|---|
| **On the PC** | http://127.0.0.1:8080 |
| **On your iPhone / MacBook (via Tailscale)** | `https://<pc-name>.<tailnet>.ts.net` (shown by `tailscale serve status`) |
| **Login** | the password in `secrets\dashboard_password` |

Pages: **Overview** (balance, equity, P&L, every performance metric, current position), **Activity** (live feed of signals, fills, stops, targets, rejections, halts, errors, in Eastern Time), **Trades** (searchable, filterable history, CSV export), **Signals** (V26 only: strategy diagnostics funnel from HTF FVG detected → tap → opposing FVG selected → inversion → entry → alert received → order accepted/rejected, the exact block/rejection reasons, current setup state per timeframe, and a timeline. An engine without V26 diagnostics shows a notice), **Analytics** (equity, cumulative P&L, daily P&L, win/loss distribution, drawdown, by weekday, by symbol; Today/7D/30D/All), **Positions** (open positions and working orders, refreshed every 4 s), **Health** (engine, Docker, TradingView feed, market data, reconciliation, mode, risk, halts, errors).

---

## How it is built (and why it can't affect trading)

```
Browser ──► dashboard container (127.0.0.1:8080)
              │  GET only, fixed allowlist:
              │  /health /status /admin/fills /admin/orders /admin/events /admin/audit
              │  /admin/diagnostics
              ▼
            engine container (unchanged)  ◄── TradingView via Cloudflare (port 8000)
```

* **Separate service.** Its own container, published on `127.0.0.1:8080` only. The engine's service definition is byte-for-byte unchanged, and Docker keeps the engine running when the dashboard is added.
* **Read-only by construction.** The dashboard's engine client can only issue GET requests to the seven paths above. Kill, resume, flatten, reconcile and quote endpoints are refused before a request is built. The dashboard's only non-GET routes are its own login and logout.
* **No database access.** It never opens the engine's SQLite file. All figures come from the engine's existing read endpoints, which serve the persisted records, so history survives restarts of either container.
* **No fabricated numbers.** Balance, equity, open and today's P&L come straight from the broker account. Trades are rebuilt from recorded broker fills. Anything that can't be computed (for example win rate before the first closed trade) shows "—". If the engine is unreachable, figures are hidden, not shown stale.
* **Token handling.** The dashboard holds the engine's admin token (the engine requires it for `/status`). The client code can only use it for the GET paths above. The webhook token is never given to the dashboard.
* **Hardened container.** Read-only filesystem, all Linux capabilities dropped, no-new-privileges, no Docker socket, no volumes.

### Security

* Password login (minimum 12 characters), with server-side sessions in HttpOnly, SameSite=Strict cookies. Sessions are Secure when served over HTTPS (Tailscale).
* Five failed logins lock sign-in for 5 minutes.
* Host allowlist (`127.0.0.1`, `localhost`, `*.ts.net`) blocks DNS-rebinding attacks on the local port. Cross-origin login attempts are refused.
* Strict Content-Security-Policy: no inline scripts, no `eval`, no third-party origins. All JavaScript libraries are served from the container (Preact 10.29.8, htm 3.1.1, Chart.js 4.5.1; see `v25bot/dashboard/static/vendor/LICENSES.md`).
* Sessions reset when the dashboard container restarts, so you sign in again.

### What "healthy" means

| Indicator | Shows green only when |
|---|---|
| Engine | `/health` answers, the monitor loop is running, the broker is connected and reconciled, and trading isn't halted |
| TradingView | a bar or heartbeat arrived **within the last 150 s**. A past timestamp alone is never shown as live. Outside CME hours it shows "idle · market closed" in grey instead of a warning. |
| Docker | the engine container answers over the compose network. The dashboard has no Docker API access by design. |
| Engine uptime | how long this dashboard has continuously seen the engine online (resets when the dashboard restarts). The engine doesn't publish its own start time. |

The Cloudflare tunnel isn't monitored directly. TradingView traffic arriving is the evidence that it works.

---

## Monitor without keeping Command Prompt open

1. **Docker Desktop → Settings → General → "Start Docker Desktop when you sign in to your computer"**: on. Both containers use `restart: unless-stopped`, so they come back by themselves after a reboot.
2. Bookmark **http://127.0.0.1:8080** (on iPhone: Share → *Add to Home Screen* for an app-like icon).
3. Container status and logs are also visible in **Docker Desktop → Containers**.
4. The Cloudflare quick tunnel still needs its own window. Start it minimized so it stays out of the way:
   `start "cloudflared" /min cloudflared tunnel --url http://localhost:8000`
   A quick tunnel gets a new address each time it starts, and TradingView's alert must use the current one.

---

## Remote access with Tailscale (iPhone and MacBook)

Tailscale creates a private, encrypted network ("tailnet") between your own devices. **Tailscale Serve** publishes the dashboard to *your tailnet only*. Nothing is opened on your router, nothing goes through Cloudflare, and the public internet can't reach it. (Tailscale's public variant, *Funnel*, is **not** used.)

The dashboard keeps listening on `127.0.0.1:8080`, so it keeps working on the PC even when Tailscale is disconnected.

### 1. Windows PC (where the bot runs)

1. Install Tailscale: https://tailscale.com/download/windows. Sign in (Google, Microsoft, GitHub or Apple account).
2. In the Tailscale admin console, under **DNS** (https://login.tailscale.com/admin/dns):
   * **MagicDNS**: enabled (the default for new tailnets).
   * **HTTPS Certificates**: click **Enable HTTPS**. Serve needs this for the `https://….ts.net` address.
3. Keep it connected while you're signed out: Tailscale tray icon → **Settings** (called **Preferences** in some versions) → enable **Run unattended**.
4. Open **Command Prompt** and run:
   ```bat
   tailscale serve --bg 8080
   tailscale serve status
   ```
   `status` prints your dashboard address, for example `https://gaming-pc.tail1a2b3c.ts.net`, proxying to `http://127.0.0.1:8080`. `--bg` keeps it running after you close the window, and across restarts.
   If Windows says access is denied, run the same commands from **Command Prompt (Admin)**.

### 2. MacBook

1. Install Tailscale from the Mac App Store or https://tailscale.com/download/mac.
2. Sign in with **the same account** as the PC.
3. Open the address from step 1.4 in Safari or Chrome, then sign in with the dashboard password.
4. Optional check from Terminal: `curl -sS https://<pc-name>.<tailnet>.ts.net/healthz` prints `{"ok":true}`.

### 3. iPhone

1. Install **Tailscale** from the App Store.
2. Sign in with **the same account**, and allow the VPN configuration when iOS asks.
3. Make sure the Tailscale toggle is **on** (Connected).
4. Open the address in Safari and sign in. Safari can save the password to Keychain.
5. Optional: Share → **Add to Home Screen**.

### Verify remote access without touching the bot

All of these are read-only checks. None restarts or reconfigures the engine.

| Check | Expected |
|---|---|
| On the PC: `tailscale serve status` | `https://<pc>.<tailnet>.ts.net` → `proxy http://127.0.0.1:8080` |
| iPhone with **Wi-Fi off** (cellular) and Tailscale on: open the address | Login page, then the dashboard after sign-in |
| iPhone with **Tailscale off**: open the address | Doesn't load. That's correct: it isn't on the public internet. |
| Any device: `https://<your>.trycloudflare.com/login` | 404 from the engine. The dashboard isn't behind the Cloudflare tunnel. |
| On the PC: `curl.exe -s http://127.0.0.1:8000/health` | Still `"paper_only":true,"live_enabled":false`. The bot is unaffected. |

### Turning remote access off

`tailscale serve reset` removes the Serve configuration. The dashboard stays available on the PC at `http://127.0.0.1:8080`.

### Notes

* Only devices signed into your tailnet can reach the address. If you ever share the PC with other Tailscale users, they could reach the login page too (still password protected).
* Use the `ts.net` name, not the `100.x.y.z` Tailscale IP. The HTTPS certificate is issued for the name, and the dashboard's host allowlist expects it. Custom names can be added with `DASHBOARD_ALLOWED_HOSTS`.

---

## Configuration

Set in `docker-compose.yml` (dashboard service) or `.env`:

| Variable | Default | Meaning |
|---|---|---|
| `DASHBOARD_PASSWORD_FILE` | `/run/secrets/dashboard_password` | Login password (≥ 12 chars), from `secrets/dashboard_password` |
| `DASHBOARD_SESSION_HOURS` | 12 | Session lifetime |
| `DASHBOARD_ALLOWED_HOSTS` | `127.0.0.1,localhost,*.ts.net` | Accepted `Host` headers |
| `DASHBOARD_REFRESH_SECONDS` | 4 | Live refresh interval (3–30) |
| `DASHBOARD_TV_LIVE_SECONDS` | 150 | Max age of TradingView data still called "receiving" |

To change the password: edit `secrets\dashboard_password`, then run `docker compose up -d --no-deps --force-recreate dashboard`.
