# Tradovate API access and Lucid evaluation accounts

## Finding

**Lucid evaluation (and funded prop) accounts cannot use the Tradovate API.** The `TradovateBroker` adapter therefore stays disabled for your use case.

Basis (researched 2026-10-19; Tradovate's site could not be fetched directly from the build sandbox, so this comes from search results quoting Tradovate's own pages. Re-check before relying on it):

* Tradovate support article *"Tradovate API Access"*: API access requires an active **API Access add-on** subscription (reported as $25/month) on a **live, funded Tradovate brokerage account**. **Prop-firm and evaluation accounts are not eligible.** The add-on covers order placement and account data, **not** real-time market data.
* Tradovate support article *"Using Your Prop or Evaluation Account on Tradovate"*: prop/evaluation accounts are provisioned by the firm, not Tradovate. Tradovate support cannot enable features on the provider's behalf.
* Third-party guides report a minimum live-account balance (around $1,000) for API keys. This is unconfirmed on Tradovate's own pages.
* Tradovate community threads report that a personal API key does not reach prop-firm accounts.

Lucid's own rules reportedly **allow automated trading** on evaluation and funded accounts (with the trader responsible for errors, and HFT/microscalping prohibited). "Automation allowed" is not the same as "Tradovate API access granted". These are separate permissions, and the second is denied by Tradovate.

## What the code does about it

* `TRADOVATE_ACCOUNT_IS_PROP_OR_EVALUATION` defaults to `true`. While it is `true` the adapter refuses to construct and the app refuses to start with `BROKER=tradovate`.
* While `PAPER_ONLY=true`, only `BROKER=paper` is allowed at all.
* The adapter uses documented REST endpoints only, sends `isAutomated: true` on every order, never solves or bypasses CAPTCHA/"p-ticket" challenges, and never uses web-app session tokens or scraping.
* It has **not** been executed against Tradovate servers. Before any use on a personal funded account with the API add-on, it needs a verified end-to-end test on Tradovate's demo environment.

## Supported alternatives for Lucid accounts (verify each with Lucid in writing)

| Route | How it works | Notes |
|---|---|---|
| TradingView → TradingView's native Tradovate broker panel | Manual or semi-manual trading in TradingView connected to the Lucid/Tradovate login | Officially supported connection; TradingView strategies cannot auto-place broker orders from alerts without a bridge. |
| Lucid-approved third-party automation (e.g. TradersPost, PickMyTrade, CrossTrade for NinjaTrader) | TradingView alert → vendor → Tradovate/NinjaTrader via OAuth or the vendor's own integration | These use connections authorised by the platform. Confirm with Lucid that the vendor is permitted on your account type. This engine could forward to such a vendor's documented webhook as a future adapter. |
| NinjaTrader strategy (NinjaScript) | Re-implement V25 natively in NinjaTrader, which Lucid supports | Runs inside an approved platform; no third-party API needed. |
| Personal funded Tradovate account + API add-on | This engine's `TradovateBroker` | Only after a verified demo test. Never with prop/evaluation credentials. |

Nothing in this repository attempts to obtain API access the platform has not granted.
