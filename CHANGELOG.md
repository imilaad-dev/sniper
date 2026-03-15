# Changelog

**Rule: Every code or config change must have a changelog entry below. No exceptions.**

---

## 2026-03-15 — Lower min_odds to 0.97

### Why
More opportunities — 0.97 contracts still have very high win probability near expiry.

### What changed
- **`config.json`**: `min_odds` 0.98 → 0.97

---

## 2026-03-15 — Config update + fix parallel snipe exceeding max_open

### What changed
- **`config.json`**: `min_odds` 0.97→0.98, `max_seconds_left` 20→15, `max_open` 20→7. All 7 assets enabled for all timeframes.
- **`sniper.py`**: **BUG FIX** — parallel snipe batch was firing ALL valid targets regardless of `max_open`. The capacity check at the top of the loop only skipped when already full, but the targeting phase had no cap. Added `slots_available` check to truncate targets list to remaining open slots before firing.
- **`INTRO.md`**: Updated all config references to match new values.

---

## 2026-03-15 — Parallel orders + isolation fixes + pre-warm CLOB client

### What changed
- **`sniper.py`**: Orders now fire in parallel via `ThreadPoolExecutor` — all candidates in one batch instead of sequential 8s per order. Removed dangerous `sys.path.insert(0, "/opt/polybot")` that could accidentally import polybot modules. Pre-warms CLOB client at startup.
- **`client.py`**: Thread-safe CLOB client cache with `threading.Lock()`. Added file lock (`/tmp/polymarket_redeem.lock`) on `redeem_positions()` to prevent nonce conflicts with polybot (shared wallet).

---

## 2026-03-15 — Fix audit bugs + optimize scanning + center dashboard

### What changed
- **`client.py`**: Fixed bare `except` → `except Exception` in `_pj()`. Moved `timedelta` import to module level. Used `_direct` session (no proxy) for all read-only API calls; `py_clob_client` still uses VPN proxy for order placement.
- **`sniper.py`**: **HIGH FIX** — `locked` balance now includes `spent_this_cycle` so second bet in same cycle can't overspend available balance. **MEDIUM FIX** — `now` refreshed inside candidate loop before each time check (was stale after 5-7s `place_buy()` calls). Fixed `max_stake_pct` default mismatch (was 0.10 in computation vs 0.15 in log message). Stale position timeout increased from 300s to 600s (handles 1h markets).
- **`dashboard.py`**: Added `max-width: 900px` centered container. Fixed bare `except` → `except Exception`. Used `deque(f, maxlen=20)` for efficient log tail reading. Fixed docstring port reference.
- **`config.json`**: `min_odds` → 0.97, `max_stake_pct` → 0.15, `max_seconds_left` → 20, `min_seconds_left` → 2. These were changed during debugging but not logged.
- **`INTRO.md`**: Updated all config values and strategy description to match current config.
- **`sniper.service`**: Added `HTTPS_PROXY` and `HTTP_PROXY` env vars + `microsocks-mullvad.service` dependency for CLOB order VPN routing.

---

## 2026-03-15 — Fix critical bugs + add hourly support

### What changed
- **`client.py`**: **CRITICAL FIX** — indentation bug meant only the last slug per asset was processed, silently skipping 2/3 of markets. Entire market parsing block now correctly inside `for slug in slugs:` loop.
- **`client.py`**: Fixed ET timezone to use `zoneinfo.ZoneInfo("America/New_York")` for proper DST handling (was hardcoded UTC-4 which breaks during EST Nov-Mar).
- **`client.py`**: Default `timeframes` now includes `"1h"`.
- **`sniper.py`**: Fixed default assets to include `xrp` (was `["btc","eth","sol"]`, now `["btc","eth","sol","xrp"]`).
- **`sniper.py`**: Fixed broken capacity check in candidate loop (was comparing `state["open_positions"]` against itself). Now uses `bets_this_cycle` counter.
- **`sniper.py`**: Added periodic win notification — every 10 wins sends a Telegram summary with WR, PnL, bankroll.
- **`sniper.py`**: Startup log now shows both `assets` and `assets_hourly` lists.
- **`dashboard.py`**: Removed dead code (`bet_map`, unused `CONFIG_FILE` variable).

---

## 2026-03-15 — Add 15m and hourly market support + extra assets

### What changed
- **`client.py`**: Added hourly market slug generation. Hourly slugs use human-readable format: `{full_name}-up-or-down-{month}-{day}-{year}-{hour}-et` (e.g. `bitcoin-up-or-down-march-15-2026-12pm-et`). 5m/15m keep unix timestamp format. Added `assets_hourly` param for extra hourly assets (DOGE, BNB, HYPE). Asset name mapping in `_HOURLY_ASSET_NAMES`.
- **`sniper.py`**: Reads `timeframes` and `assets_hourly` from config, passes to scanner. Startup log shows active timeframes.
- **`config.json`**: Added `timeframes: ["5m", "15m", "1h"]`, `assets_hourly` (7 assets), `xrp` to base assets.
- **`INTRO.md`**: Updated with multi-timeframe docs, slug patterns, hourly asset list.

---

## 2026-03-15 — Initial release

### Why
Research into popular Polymarket bots revealed a proven strategy: buying contracts already at $0.99 in the final seconds of 5-min markets. Near-zero fees at 0.99 (Polymarket fee formula: `fee_rate * p * (1-p)^2` → $0.0001 at p=0.99). The outcome is essentially decided — you're farming the settlement lag.

### What was built
- **`sniper.py`**: Main loop scanning every 3 seconds. Finds 5-min markets with any side at >= 0.99 odds, buys in the last 3-15 seconds before expiry, waits for resolution, redeems winning shares. Stale position detection at 5 min past expiry. Telegram notifications on losses (wins are too frequent to notify).
- **`client.py`**: Stripped-down Polymarket CLOB client. Slug-based market discovery, live price fetch via CLOB API, GTC limit buy with 5s fill timeout and cancel-on-miss, Gamma + CLOB result checking, on-chain CTF redemption with dual indexSet.
- **`dashboard.py`**: Single-page Flask dashboard on port 5001. Stat cards (bankroll, PnL, WR, open positions), recent trades table, live log tail. Auto-refreshes every 5s.
- **`config.json`**: 9 settings: min_odds (0.99), stake ($5), max_stake_pct (10%), time window (3-15s), fill timeout (5s), max_open (2), assets (BTC/ETH/SOL), enabled toggle.
- **`sniper.service`**: systemd service using polybot's venv for py_clob_client/web3.
- **`sniper-dash.service`**: systemd service using system Python for Flask.

### Architecture decisions
- **Shared wallet** with polybot — sniper locks capital for seconds (not minutes), minimal overlap
- **Shared Telegram** — messages prefixed `[SNIPER]` to distinguish from polybot
- **No signals/Kelly/learner** — strategy is purely mechanical, no analysis needed
- **3-second scan interval** — faster than polybot's 5s to catch narrow windows
- **Separate state/trades** — fully independent tracking, no cross-contamination
