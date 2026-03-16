# Sniper Bot — 90¢+ Contract Farmer

## What it does

Buys contracts trading at **$0.90+** on Polymarket **5-minute and hourly** crypto markets in the **final seconds** before expiry. At these odds the outcome is essentially decided — price just hasn't settled yet. Profit: ~$0.01-0.10 per share per trade, near-zero fees, ~95%+ win rate.

Runs 24/7 on the same VPS as the Poly Trading Bot at `/opt/sniper/`. Shares the same wallet, VPN, and Telegram bot.

**Philosophy: boring math, consistent results.** No analysis, no signals, no intuition. Just farming settled markets faster than the price updates.

**Multi-timeframe:** Scans 5m and 1h markets (configurable via `timeframes`). 15m disabled due to 100% miss rate (zero liquidity). Hourly markets have extra assets (DOGE, BNB, HYPE) and better liquidity. Slug patterns differ by timeframe:
- 5m/15m: `{asset}-updown-{tf}-{unix_timestamp}` (e.g. `btc-updown-5m-1773590400`)
- 1h: `{full_name}-up-or-down-{month}-{day}-{year}-{hour}-et` (e.g. `bitcoin-up-or-down-march-15-2026-12pm-et`)

---

## How it works

1. **Scan** (every 3 seconds): Query Gamma API for active 5m/1h markets across all 7 assets (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE)
2. **Filter**: Find any side (YES or NO) priced at >= `min_odds` (0.97)
3. **Time gate**: Only buy in the last `max_seconds_left` (15s) to `min_seconds_left` (2s) before market expiry
4. **Buy**: Place GTC limit order at market price + 0.01 buffer (capped at 0.99), wait up to 8s for fill (polling every 1s)
5. **Resolve**: Wait for market to settle, redeem winning shares for $1.00 each
6. **Repeat**

### Why it works

- At 0.97+ odds with <15 seconds to expiry, the crypto price has already moved decisively
- The market just hasn't fully priced it in yet (CLOB latency, thin liquidity at extremes)
- Polymarket taker fee at 0.98-0.99 is **< $0.001 per $5 stake** (essentially zero)
- You're not predicting anything — you're farming the settlement lag

### The risk

- Rare flash reversal in final seconds → full stake loss ($5)
- One loss erases ~100 wins at typical profit levels
- Thin liquidity at high odds means orders may not fill (NOFILL, not a loss)

---

## Files

| File | What it does |
|---|---|
| `sniper.py` | Main loop. Scans every 3s, finds 0.97+ contracts near expiry, buys, resolves, redeems. Telegram alerts on losses + every 10 wins |
| `client.py` | Stripped-down Polymarket CLOB client. Market discovery (slug-based), live price fetch, GTC limit buy, result check, on-chain redemption |
| `dashboard.py` | Single-page Flask dashboard on port 5001. Stats (bankroll, PnL, WR, fill rate, avg profit, pending redeems), recent trades table with TF column, live log tail. Auto-refreshes every 5s |
| `config.json` | Runtime config: min_odds, stake, time window, assets, max_open |
| `data/state.json` | Bankroll (`total_deposited + pnl_usdc`), PnL, open positions, win/loss counts |
| `data/trades.jsonl` | Full trade log (BET + OUTCOME records) |
| `data/sniper.log` | Rotating log file (2MB, 3 backups) |

---

## Shared resources with Polybot

| Resource | Shared? | Details |
|---|---|---|
| Wallet | Yes | Same private key (`/opt/polybot/data/.wallet.json`), same USDC.e balance. **Bankroll is tracked independently per-bot** (`total_deposited + pnl_usdc`), not synced from on-chain. On-chain balance is only checked before betting (shared wallet safety) |
| Telegram | Yes | Same token + chat IDs (`/opt/polybot/data/.env`), messages prefixed `[SNIPER]` |
| VPN | Yes | Same Mullvad SOCKS5 on 127.0.0.1:1080 |
| Python venv | Partial | Bot uses polybot's venv for py_clob_client/web3. Dashboard uses system Python for Flask |
| State | No | Separate `state.json`, `trades.jsonl`, `config.json` |
| Dashboard | No | Separate Flask app on port 5001 (polybot dashboard is on 5050 via nginx) |

---

## Config reference

| Key | Default | Purpose |
|-----|---------|---------|
| `min_odds` | 0.90 | Minimum odds to buy (0.90-0.99 = near-settled markets) |
| `max_odds` | 0.99 | Maximum odds to buy — above 0.99 there's no spread, guaranteed miss |
| `stake_per_bet` | 5.0 | Base stake per trade in USDC |
| `max_stake_pct` | 0.30 | Max stake as fraction of bankroll (30%) |
| `max_seconds_left` | 15 | Start buying this many seconds before expiry |
| `min_seconds_left` | 2 | Stop buying — too close to expiry, order won't fill |
| `fill_timeout_seconds` | 8 | Cancel order if not filled within this time |
| `max_open` | 7 | Max simultaneous open positions |
| `assets` | ["btc","eth","sol","xrp","doge","bnb","hype"] | Which assets to scan (all timeframes) |
| `assets_hourly` | ["btc","eth","sol","xrp","doge","bnb","hype"] | Which assets to scan (1h, same as base) |
| `timeframes` | ["5m","1h"] | Which market timeframes to scan (15m disabled — zero liquidity) |
| `enabled` | true | Master on/off switch |

---

## Capital allocation

$20 transferred from polybot on 2026-03-15 (polybot's original deposit). Bankroll = `total_deposited + pnl_usdc`. Both bots share the same on-chain wallet but track bankroll independently.

---

## Services

```
systemctl start|stop|restart|status sniper       # the bot
systemctl start|stop|restart|status sniper-dash   # the dashboard (port 5001)
```

---

## Debugging

**Check if running:**
```
systemctl status sniper
journalctl -u sniper -n 50
```

**Check dashboard:**
```
systemctl status sniper-dash
curl http://localhost:5001/api/stats
```

**View trades:**
```
tail -20 /opt/sniper/data/trades.jsonl | python3 -m json.tool
```

**Check state:**
```
cat /opt/sniper/data/state.json
```

---

## Math

At 0.99 odds buying 5.05 shares ($5 stake):
- **Win**: 5.05 shares × $1.00 = $5.05, profit = **$0.05**
- **Loss**: $5.00 lost
- **Fee**: ~$0.0001 (negligible)
- **Breakeven WR**: 99.0%
- **At 99.5% WR**: net +$0.025/trade average → 50 trades/day = +$1.25/day

The profit per trade is tiny. The edge is volume and consistency.
