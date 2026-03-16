#!/opt/polybot/venv/bin/python3
"""
sniper.py — 90¢+ Contract Sniper Bot
Buys contracts at $0.90+ in the final seconds of 5m/1h crypto markets.
Profit: ~$0.01-0.10/share per trade, near-zero fees, ~95%+ win rate.
Risk: rare reversal in final seconds = full stake loss.

Strategy:
  - Scan all 5m/1h markets every 3 seconds
  - Find any side (YES/NO) priced at >= min_odds (default 0.90)
  - Only buy in the last N seconds before expiry (default 2-15s)
  - Quick fill timeout (8s) — if not filled, cancel and move on
  - Wait for resolution, redeem winning shares
"""

import json
import logging
import sys
import time
import signal as _signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ── Paths ──────────────────────────────────────────────────────────────────
BOT_DIR  = Path("/opt/sniper")
DATA_DIR = BOT_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        RotatingFileHandler(DATA_DIR / "sniper.log", maxBytes=2_000_000, backupCount=3),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("sniper")
logging.getLogger("httpx").setLevel(logging.WARNING)

# ── Local imports ──────────────────────────────────────────────────────────
sys.path.insert(0, str(BOT_DIR))
from client import find_snipeable_markets, get_market_result, place_buy, redeem_positions

# ── Config / State ─────────────────────────────────────────────────────────
CONFIG_FILE = BOT_DIR / "config.json"
STATE_FILE  = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.jsonl"
WALLET_FILE = Path("/opt/polybot/data/.wallet.json")  # shared wallet
ENV_FILE    = Path("/opt/polybot/data/.env")

LOOP_SECONDS = 3  # scan every 3 seconds for speed

STOP_EVENT = threading.Event()
RUNNING = True


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        return json.loads(CONFIG_FILE.read_text())
    return {}


def _load_wallet() -> str:
    return json.loads(WALLET_FILE.read_text())["private_key"]


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {
        "bankroll_usdc": 0.0,
        "total_deposited": 0.0,
        "pnl_usdc": 0.0,
        "total_bets": 0,
        "total_wins": 0,
        "total_losses": 0,
        "open_positions": [],
        "pending_redemptions": [],
        "total_misses": 0,
    }


def _save_state(state: dict):
    tmp = STATE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n")
    tmp.rename(STATE_FILE)


def _log_trade(entry: dict):
    with open(TRADES_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def _get_onchain_balance() -> float | None:
    try:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider("https://polygon-bor-rpc.publicnode.com"))
        wallet = json.loads(WALLET_FILE.read_text())
        addr = Web3.to_checksum_address(wallet["address"])
        abi = [{"inputs":[{"name":"account","type":"address"}],"name":"balanceOf",
                "outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        usdce = w3.eth.contract(
            address=Web3.to_checksum_address("0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"), abi=abi)
        raw = usdce.functions.balanceOf(addr).call()
        return round(raw / 1e6, 4)
    except Exception:
        return None


# ── Telegram ───────────────────────────────────────────────────────────────

_tg_token = None
_tg_chat_ids = []

def _load_telegram():
    global _tg_token, _tg_chat_ids
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("TELEGRAM_TOKEN="):
                _tg_token = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_CHAT_IDS="):
                _tg_chat_ids = [c.strip() for c in line.split("=", 1)[1].split(",") if c.strip()]

def _notify(msg: str):
    if not _tg_token or not _tg_chat_ids:
        return
    import requests
    for chat_id in _tg_chat_ids:
        try:
            requests.post(
                f"https://api.telegram.org/bot{_tg_token}/sendMessage",
                json={"chat_id": chat_id, "text": f"🎯 <b>[SNIPER]</b> {msg}",
                      "parse_mode": "HTML", "disable_web_page_preview": True},
                timeout=10,
            )
        except Exception:
            pass


# ── Shutdown ───────────────────────────────────────────────────────────────

def _shutdown(sig, frame):
    global RUNNING
    log.info("Shutdown signal received")
    RUNNING = False
    STOP_EVENT.set()

_signal.signal(_signal.SIGTERM, _shutdown)
_signal.signal(_signal.SIGINT, _shutdown)


# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    cfg = _load_config()
    privkey = _load_wallet()
    state = _load_state()
    _load_telegram()

    # Pre-warm CLOB client so first parallel batch doesn't wait for auth
    try:
        from client import _get_client
        _get_client(privkey)
    except Exception as e:
        log.warning("CLOB client pre-warm failed: %s", e)

    # Bankroll is trade-based only: total_deposited + pnl_usdc.
    # No on-chain sync — wallet is shared with polybot.
    if state["bankroll_usdc"] == 0 and state.get("total_deposited", 0) == 0:
        log.warning("Sniper bankroll is $0. Set total_deposited in state.json to allocate funds.")

    _loop_count = 0
    _cached_onchain = None
    log.info("=== Sniper Bot Starting ===")
    _tfs = cfg.get("timeframes", ["5m", "1h"])
    _assets = cfg.get("assets", ["btc", "eth", "sol", "xrp", "doge", "bnb", "hype"])
    _assets_h = cfg.get("assets_hourly", _assets)
    log.info("Assets: %s | Hourly: %s | Timeframes: %s | Min odds: %.2f | Window: %d-%ds",
             _assets, _assets_h, _tfs,
             cfg.get("min_odds", 0.97),
             cfg.get("min_seconds_left", 2),
             cfg.get("max_seconds_left", 15))

    _notify(
        f"Sniper Bot Started\n"
        f"Assets: {', '.join(a.upper() for a in _assets)}\n"
        f"Hourly: {', '.join(a.upper() for a in _assets_h)}\n"
        f"Timeframes: {', '.join(_tfs)}\n"
        f"Min odds: {cfg.get('min_odds', 0.97):.2f} | Stake: ${cfg.get('stake_per_bet', 5.0):.0f}\n"
        f"Window: last {cfg.get('max_seconds_left', 15)}s before expiry"
    )

    while RUNNING:
        try:
            cfg = _load_config()
            state = _load_state()

            if not cfg.get("enabled", True):
                STOP_EVENT.wait(LOOP_SECONDS)
                continue

            # ── 1. Resolve open positions ─────────────────────────────────
            still_open = []
            for pos in state.get("open_positions", []):
                result = get_market_result(pos["market_id"])
                if result:
                    won = (result == pos["side"])
                    stake = pos["stake_usdc"]
                    shares = pos.get("shares", stake / pos["odds"])
                    pnl = round(shares - stake, 4) if won else -stake

                    state["bankroll_usdc"] = round(state["bankroll_usdc"] + pnl, 4)
                    state["pnl_usdc"] = round(state["pnl_usdc"] + pnl, 4)
                    if won:
                        state["total_wins"] += 1
                        if not redeem_positions(privkey, pos["market_id"]):
                            log.warning("Redeem failed for %s — queuing for retry", pos["trade_id"])
                            state.setdefault("pending_redemptions", []).append(pos["market_id"])
                    else:
                        state["total_losses"] += 1

                    _log_trade({
                        "type": "OUTCOME",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "trade_id": pos["trade_id"],
                        "asset": pos["asset"],
                        "side": pos["side"],
                        "odds": pos["odds"],
                        "result": result,
                        "pnl_usdc": pnl,
                        "shares": shares,
                        "timeframe": pos.get("timeframe", "5m"),
                    })

                    tag = "WIN" if won else "LOSS"
                    log.info("[%s] %s %s %s @ %.2f | PnL=%+.4f | Bankroll=%.2f",
                             pos["asset"].upper(), tag, pos["side"], result,
                             pos["odds"], pnl, state["bankroll_usdc"])

                    if not won:
                        _notify(
                            f"❌ <b>LOSS</b> [{pos['asset'].upper()}]\n"
                            f"{pos['side']} @ {pos['odds']:.2f} | PnL: -${abs(pnl):.2f}\n"
                            f"Bankroll: ${state['bankroll_usdc']:.2f}"
                        )
                    elif state["total_wins"] % 10 == 0 and state["total_wins"] > 0:
                        _notify(
                            f"✅ <b>{state['total_wins']} wins!</b>\n"
                            f"PnL: {'+' if state['pnl_usdc'] >= 0 else ''}"
                            f"${state['pnl_usdc']:.2f} | "
                            f"WR: {state['total_wins']/(state['total_wins']+state['total_losses'])*100:.1f}% | "
                            f"Bankroll: ${state['bankroll_usdc']:.2f}"
                        )
                else:
                    # Check for stale (>5 min past expiry)
                    try:
                        end = datetime.fromisoformat(pos["end_date_iso"].replace("Z", "+00:00"))
                        age = (datetime.now(timezone.utc) - end).total_seconds()
                        # Stale timeout: 10 min past expiry (handles all timeframes including 1h)
                        if age > 600:
                            log.warning("Stale position %s (age=%ds) — force-closing as loss",
                                        pos["trade_id"], int(age))
                            stake = pos["stake_usdc"]
                            state["bankroll_usdc"] = round(state["bankroll_usdc"] - stake, 4)
                            state["pnl_usdc"] = round(state["pnl_usdc"] - stake, 4)
                            state["total_losses"] += 1
                            _log_trade({
                                "type": "OUTCOME", "trade_id": pos["trade_id"],
                                "timestamp": datetime.now(timezone.utc).isoformat(),
                                "asset": pos.get("asset", "?"),
                                "side": pos.get("side", "?"),
                                "odds": pos.get("odds", 0),
                                "shares": pos.get("shares", 0),
                                "result": "STALE", "pnl_usdc": -stake,
                            })
                            continue
                    except Exception:
                        pass
                    still_open.append(pos)

            state["open_positions"] = still_open
            _save_state(state)

            # ── 2. Retry pending redemptions ──────────────────────────────
            pending = state.get("pending_redemptions", [])
            if pending:
                still_pending = []
                for mkt_id in pending:
                    if redeem_positions(privkey, mkt_id):
                        log.info("Pending redeem OK: %s", mkt_id[:20])
                    else:
                        still_pending.append(mkt_id)
                state["pending_redemptions"] = still_pending
                _save_state(state)

            # ── 3. Check capacity ─────────────────────────────────────────
            max_open = cfg.get("max_open", 7)
            if len(still_open) >= max_open:
                STOP_EVENT.wait(LOOP_SECONDS)
                continue

            stake = cfg.get("stake_per_bet", 5.0)
            locked = sum(p.get("stake_usdc", 0) for p in still_open)
            available = state["bankroll_usdc"] - locked
            if available < stake:
                STOP_EVENT.wait(LOOP_SECONDS)
                continue

            # On-chain wallet check (shared wallet — both bots draw from same USDC.e)
            # Throttled: only check every 10 loops (~30s) to avoid RPC latency on every scan
            _loop_count += 1
            if _loop_count % 10 == 1 or _cached_onchain is None:
                _cached_onchain = _get_onchain_balance()
            onchain = _cached_onchain
            if onchain is not None and onchain < stake:
                log.info("On-chain balance $%.2f too low for stake $%.2f — waiting", onchain, stake)
                STOP_EVENT.wait(LOOP_SECONDS)
                continue

            # ── 4. Scan for snipeable markets ─────────────────────────────
            assets = cfg.get("assets", ["btc", "eth", "sol", "xrp", "doge", "bnb", "hype"])
            min_odds = cfg.get("min_odds", 0.97)
            max_secs = cfg.get("max_seconds_left", 15)
            min_secs = cfg.get("min_seconds_left", 2)
            fill_timeout = cfg.get("fill_timeout_seconds", 8)

            timeframes = cfg.get("timeframes", ["5m", "1h"])
            assets_hourly = cfg.get("assets_hourly", assets)
            markets = find_snipeable_markets(assets, min_odds=min_odds,
                                             timeframes=timeframes,
                                             assets_hourly=assets_hourly,
                                             max_secs=max_secs)
            now = datetime.now(timezone.utc)

            if markets:
                for _m in markets:
                    try:
                        _end = datetime.fromisoformat(_m.end_date_iso.replace("Z", "+00:00"))
                        _sl = int((_end - now).total_seconds())
                        _best = max(_m.yes_price, _m.no_price)
                        _side = "YES" if _m.yes_price >= _m.no_price else "NO"
                        log.info("[%s] CANDIDATE: %s@%.2f | %ds left | %s",
                                 _m.asset.upper(), _side, _best, _sl, _m.question[:40])
                    except Exception:
                        pass
            # Track (asset, end_date) pairs — allows cross-timeframe bets
            # (DOGE 5m + DOGE 1h OK) but blocks same-market duplicates
            open_keys = {(p.get("asset", "").lower(), p.get("end_date_iso", ""))
                         for p in still_open}

            # Build list of snipeable targets (filter first, buy in parallel)
            now = datetime.now(timezone.utc)
            _max_pct = cfg.get("max_stake_pct", 0.30)
            max_stake = state["bankroll_usdc"] * _max_pct
            targets = []  # (mkt, side, odds, token_id, stake, secs_left)

            for mkt in markets:
                if (mkt.asset, mkt.end_date_iso) in open_keys:
                    continue

                try:
                    end = datetime.fromisoformat(mkt.end_date_iso.replace("Z", "+00:00"))
                    secs_left = (end - now).total_seconds()
                except Exception:
                    continue

                if secs_left > max_secs or secs_left < min_secs:
                    continue

                # Find the side at >= min_odds and < max_odds
                max_odds = cfg.get("max_odds", 0.99)
                candidates = []
                if min_odds <= mkt.yes_price <= max_odds:
                    candidates.append(("YES", mkt.yes_price, mkt.yes_token_id))
                if min_odds <= mkt.no_price <= max_odds:
                    candidates.append(("NO", mkt.no_price, mkt.no_token_id))

                if not candidates:
                    continue

                candidates.sort(key=lambda x: x[1], reverse=True)
                side, odds, token_id = candidates[0]

                actual_stake = min(stake, max_stake)
                if actual_stake < 5.0:
                    continue

                targets.append((mkt, side, odds, token_id, actual_stake, secs_left))
                open_keys.add((mkt.asset, mkt.end_date_iso))  # prevent same-market dups, allow cross-timeframe

            # Sort by odds ascending — lower odds = higher profit per trade = priority
            targets.sort(key=lambda t: t[2])

            if not targets:
                STOP_EVENT.wait(LOOP_SECONDS)
                continue

            # Respect max_open AND available balance — don't fire more orders
            # than we can actually afford (parallel orders all draw from same balance)
            slots_available = max_open - len(still_open)
            affordable = int(available / stake) if stake > 0 else 0
            max_targets = min(slots_available, affordable)
            if max_targets <= 0:
                STOP_EVENT.wait(LOOP_SECONDS)
                continue
            targets = targets[:max_targets]

            # Fire all orders in parallel
            from concurrent.futures import ThreadPoolExecutor, as_completed

            def _do_snipe(args):
                mkt, side, odds, token_id, actual_stake, secs_left = args
                log.info("[%s] SNIPE: %s @ %.2f | stake=$%.2f | %ds left | %s",
                         mkt.asset.upper(), side, odds, actual_stake,
                         int(secs_left), mkt.question[:50])
                result = place_buy(
                    private_key=privkey,
                    token_id=token_id,
                    side=side,
                    amount_usdc=actual_stake,
                    price=odds,
                    fill_timeout=fill_timeout,
                )
                return (mkt, side, odds, secs_left, result)

            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                futures = [pool.submit(_do_snipe, t) for t in targets]
                for fut in as_completed(futures):
                    try:
                        mkt, side, odds, secs_left, result = fut.result()
                    except Exception as e:
                        log.warning("Parallel snipe error: %s", e)
                        continue

                    if result.success:
                        trade_id = f"snipe_{int(time.time()*1000)}_{threading.get_ident() % 10000}"
                        pos_entry = {
                            "trade_id": trade_id,
                            "market_id": mkt.condition_id,
                            "asset": mkt.asset,
                            "side": side,
                            "odds": odds,
                            "fill_price": result.fill_price or odds,
                            "stake_usdc": result.amount_usdc,
                            "shares": result.shares,
                            "end_date_iso": mkt.end_date_iso,
                            "timeframe": mkt.timeframe,
                        }
                        state["open_positions"].append(pos_entry)
                        state["total_bets"] += 1

                        _log_trade({
                            "type": "BET",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "trade_id": trade_id,
                            "asset": mkt.asset,
                            "side": side,
                            "odds": odds,
                            "fill_price": result.fill_price or odds,
                            "stake_usdc": result.amount_usdc,
                            "shares": result.shares,
                            "order_id": result.order_id,
                            "question": mkt.question,
                            "secs_left": int(secs_left),
                            "timeframe": mkt.timeframe,
                        })

                        log.info("[%s] SNIPED: %s @ %.2f | $%.2f | shares=%.2f | order=%s",
                                 mkt.asset.upper(), side, odds, result.amount_usdc,
                                 result.shares, result.order_id)
                    else:
                        log.info("[%s] MISS: %s @ %.2f | %s",
                                 mkt.asset.upper(), side, odds, result.error or "unknown")
                        state["total_misses"] = state.get("total_misses", 0) + 1
                        _log_trade({
                            "type": "MISS",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "asset": mkt.asset,
                            "side": side,
                            "odds": odds,
                            "error": result.error or "unknown",
                            "timeframe": mkt.timeframe,
                        })

            # Save state once after all parallel orders complete
            _save_state(state)

        except Exception as e:
            log.exception("Error in main loop: %s", e)

        STOP_EVENT.wait(LOOP_SECONDS)

    log.info("=== Sniper Bot Stopped ===")


if __name__ == "__main__":
    main()
