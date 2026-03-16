"""
client.py — Stripped-down Polymarket CLOB client for the sniper bot.
Finds 5m/1h markets, fetches live odds, places FAK batch buys, checks resolution, redeems.
"""

import requests
import logging
import json
import time
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Direct session (no proxy) for market discovery — Gamma/CLOB read APIs don't need VPN
_direct = requests.Session()
_direct.trust_env = False  # ignore HTTPS_PROXY env var

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API  = "https://clob.polymarket.com"
RPC_URL   = "https://polygon-bor-rpc.publicnode.com"
CTF       = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
USDCE     = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
WINDOWS = {
    "5m":  300,    # 5 minutes
    "1h":  3600,   # 1 hour
}

# Hourly markets use human-readable slugs with full asset names
_HOURLY_ASSET_NAMES = {
    "btc": "bitcoin", "eth": "ethereum", "sol": "solana",
    "xrp": "xrp", "doge": "dogecoin", "bnb": "bnb", "hype": "hype",
}
_MONTHS = ["january", "february", "march", "april", "may", "june",
           "july", "august", "september", "october", "november", "december"]


def _fmt_et_hour(h: int) -> str:
    """Format hour as '12am', '1pm', etc."""
    if h == 0: return "12am"
    if h == 12: return "12pm"
    if h < 12: return f"{h}am"
    return f"{h - 12}pm"


def _hourly_slugs(asset: str) -> list[str]:
    """Generate hourly market slugs for current and next 2 hours."""
    name = _HOURLY_ASSET_NAMES.get(asset.lower())
    if not name:
        return []
    now = datetime.now(timezone.utc)
    slugs = []
    for offset in range(0, 3):
        try:
            from zoneinfo import ZoneInfo
            et = (now + timedelta(hours=offset)).astimezone(ZoneInfo("America/New_York"))
        except ImportError:
            # Fallback: EDT = UTC-4 (most of the year). EST = UTC-5 (Nov-Mar).
            et = now + timedelta(hours=offset) - timedelta(hours=4)
        month = _MONTHS[et.month - 1]
        slug = f"{name}-up-or-down-{month}-{et.day}-{et.year}-{_fmt_et_hour(et.hour)}-et"
        slugs.append(slug)
    return slugs


@dataclass
class Market:
    condition_id:   str
    question:       str
    up_token_id:    str
    down_token_id:  str
    up_price:       float
    down_price:     float
    end_date_iso:   str
    asset:          str = "btc"
    timeframe:      str = "5m"

    @property
    def yes_token_id(self): return self.up_token_id
    @property
    def no_token_id(self): return self.down_token_id
    @property
    def yes_price(self): return self.up_price
    @property
    def no_price(self): return self.down_price


@dataclass
class BuyResult:
    success:     bool
    order_id:    Optional[str]
    side:        str
    price:       float
    amount_usdc: float
    shares:      float
    error:       Optional[str] = None
    fill_price:  Optional[float] = None


def _fetch_market_by_slug(slug: str) -> Optional[dict]:
    try:
        r = _direct.get(f"{GAMMA_API}/markets", params={"slug": slug}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        log.debug("Gamma fetch failed for %s: %s", slug, e)
    return None


def _get_clob_price(token_id: str) -> float:
    """Get buy price for a token. Falls back to orderbook best ask if price endpoint
    returns 0 or >= 1.0 (common for hourly markets near expiry)."""
    try:
        r = _direct.get(f"{CLOB_API}/price",
                         params={"token_id": token_id, "side": "buy"}, timeout=5)
        if r.status_code == 200:
            p = float(r.json().get("price", 0))
            if 0 < p < 1.0:
                return p
    except Exception:
        pass
    # Fallback: orderbook best ask (what we'd actually pay to buy)
    try:
        r = _direct.get(f"{CLOB_API}/book",
                         params={"token_id": token_id}, timeout=5)
        if r.status_code == 200:
            asks = r.json().get("asks", [])
            if asks:
                # Best ask = lowest sell price = cheapest to buy
                best = min(float(a["price"]) for a in asks)
                return best
    except Exception:
        pass
    return 0.0


def _pj(val):
    """Parse JSON string or return list."""
    if isinstance(val, str):
        try: return json.loads(val)
        except Exception: return []
    return val if isinstance(val, list) else []


def find_snipeable_markets(assets: list, min_odds: float = 0.90,
                           timeframes: list = None,
                           assets_hourly: list = None,
                           max_secs: int = 20) -> list[Market]:
    """
    Find markets near expiry with at least one side >= min_odds.

    Optimized: only checks current window per timeframe (not future windows),
    skips CLOB price fetch if market isn't close to expiry.
    """
    if timeframes is None:
        timeframes = ["5m", "1h"]
    if assets_hourly is None:
        assets_hourly = assets
    now_ts = int(time.time())
    now_dt = datetime.now(timezone.utc)
    markets = []

    for tf in timeframes:
        window = WINDOWS.get(tf, 300)
        tf_assets = assets_hourly if tf == "1h" else assets
        for asset in tf_assets:
            # Only check current window (the one about to expire)
            if tf == "1h":
                slugs = _hourly_slugs(asset)[:1]  # only current hour
            else:
                current_start = (now_ts // window) * window
                slugs = [f"{asset.lower()}-updown-{tf}-{current_start}"]

            for slug in slugs:
                m = _fetch_market_by_slug(slug)
                if not m or m.get("closed") or m.get("resolved"):
                    continue
                try:
                    end_str = m.get("endDate", m.get("end_date_iso", ""))
                    if not end_str:
                        continue

                    # Quick time check BEFORE fetching CLOB prices
                    end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                    secs_left = (end_dt - now_dt).total_seconds()
                    if secs_left > max_secs or secs_left < 0:
                        continue  # too far out or already expired — skip price fetch

                    clob_ids = _pj(m.get("clobTokenIds", []))
                    outcomes = _pj(m.get("outcomes", []))
                    up_tid = down_tid = ""

                    if clob_ids and len(clob_ids) >= 2 and outcomes and len(outcomes) >= 2:
                        for i, o in enumerate(outcomes):
                            if str(o).lower() == "up": up_tid = clob_ids[i]
                            elif str(o).lower() == "down": down_tid = clob_ids[i]
                    elif clob_ids and len(clob_ids) >= 2:
                        up_tid, down_tid = clob_ids[0], clob_ids[1]

                    if not up_tid or not down_tid:
                        continue

                    # Only fetch prices for markets near expiry
                    up_price = _get_clob_price(up_tid)
                    down_price = _get_clob_price(down_tid)

                    if up_price >= min_odds or down_price >= min_odds:
                        markets.append(Market(
                            condition_id=m.get("conditionId", m.get("condition_id", "")),
                            question=m.get("question", ""),
                            up_token_id=up_tid, down_token_id=down_tid,
                            up_price=up_price, down_price=down_price,
                            end_date_iso=end_str,
                            asset=asset.lower(),
                            timeframe=tf,
                        ))
                except Exception:
                    pass

    markets.sort(key=lambda x: x.end_date_iso)
    return markets


def get_market_result(condition_id: str) -> Optional[str]:
    """Check if market resolved. Returns 'YES', 'NO', or None."""
    try:
        r = _direct.get(f"{GAMMA_API}/markets",
                         params={"condition_id": condition_id}, timeout=10)
        if r.status_code == 200:
            data = r.json()
            market = data[0] if isinstance(data, list) and data else data
            if isinstance(market, dict) and (market.get("resolved") or market.get("closed")):
                winner = market.get("winnerOutcome") or market.get("resolvedOutcome")
                if winner:
                    w = str(winner).lower()
                    if w == "up": return "YES"
                    elif w == "down": return "NO"
        # Fallback: CLOB API
        r2 = _direct.get(f"{CLOB_API}/markets/{condition_id}", timeout=10)
        if r2.status_code == 200:
            m2 = r2.json()
            if m2.get("resolved") or m2.get("closed"):
                for t in m2.get("tokens", []):
                    if isinstance(t, dict) and t.get("winner") is True:
                        o = t.get("outcome", "").lower()
                        if o == "up": return "YES"
                        elif o == "down": return "NO"
    except Exception as e:
        log.error("Result check failed: %s", e)
    return None


# ── CLOB client (cached, thread-safe) ─────────────────────────────────────

import threading
_cached_client = None
_cached_client_key = None
_client_lock = threading.Lock()
_order_lock = threading.Lock()  # serialize CLOB API calls — py_clob_client is not thread-safe

def _get_client(private_key: str):
    global _cached_client, _cached_client_key
    if _cached_client is not None and _cached_client_key == private_key:
        return _cached_client
    with _client_lock:
        # Double-check after acquiring lock
        if _cached_client is not None and _cached_client_key == private_key:
            return _cached_client
        from py_clob_client.client import ClobClient
        client = ClobClient("https://clob.polymarket.com", key=private_key, chain_id=137)
        client.set_api_creds(client.create_or_derive_api_creds())
        _cached_client = client
        _cached_client_key = private_key
        log.info("CLOB client created")
        return client


def place_buy_batch(
    private_key: str,
    orders: list[dict],
) -> list[BuyResult]:
    """Place multiple FAK orders in a single batch API call.
    Each order dict: {token_id, side, amount_usdc, price}.
    Returns list of BuyResult in same order as input."""
    try:
        from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
        from py_clob_client.order_builder.constants import BUY

        client = _get_client(private_key)
        batch = []
        order_meta = []  # track per-order details for results

        for o in orders:
            limit_price = min(o["price"] + 0.02, 0.99)
            shares = round(o["amount_usdc"] / limit_price, 4)
            if shares < 5:
                shares = 5.0
            amount_usdc = round(shares * limit_price, 2)

            args = OrderArgs(token_id=o["token_id"], price=limit_price, size=shares, side=BUY)
            with _order_lock:
                signed = client.create_order(args)
            batch.append(PostOrdersArgs(order=signed, orderType=OrderType.FAK))
            order_meta.append({"side": o["side"], "price": o["price"],
                               "limit_price": limit_price, "shares": shares,
                               "amount_usdc": amount_usdc})

        # Single batch API call for all orders
        with _order_lock:
            try:
                resp_list = client.post_orders(batch)
            except Exception as e:
                err_str = str(e).lower()
                if "401" in err_str or "403" in err_str or "unauthorized" in err_str:
                    global _cached_client
                    _cached_client = None
                    client = _get_client(private_key)
                    # Re-sign all orders
                    batch = []
                    for i, o in enumerate(orders):
                        meta = order_meta[i]
                        args = OrderArgs(token_id=o["token_id"], price=meta["limit_price"],
                                         size=meta["shares"], side=BUY)
                        signed = client.create_order(args)
                        batch.append(PostOrdersArgs(order=signed, orderType=OrderType.FAK))
                    resp_list = client.post_orders(batch)
                else:
                    raise

        if not isinstance(resp_list, list):
            resp_list = [resp_list]

        # Collect order IDs
        order_ids = []
        for resp in resp_list:
            oid = ""
            if isinstance(resp, dict):
                oid = resp.get("orderID") or resp.get("id") or ""
            order_ids.append(oid)

        # Brief poll to confirm FAK fill status
        time.sleep(1)
        results = []
        for i, oid in enumerate(order_ids):
            meta = order_meta[i]
            if not oid:
                results.append(BuyResult(False, None, meta["side"], meta["price"],
                                         meta["amount_usdc"], 0, error="No order ID returned"))
                continue
            try:
                with _order_lock:
                    order = client.get_order(oid)
                matched = float(order.get("size_matched", 0)) if isinstance(order, dict) else 0
                if matched > 0:
                    actual_cost = round(matched * meta["limit_price"], 4)
                    results.append(BuyResult(True, oid, meta["side"], meta["price"],
                                             actual_cost, matched,
                                             fill_price=meta["limit_price"]))
                else:
                    results.append(BuyResult(False, oid, meta["side"], meta["price"],
                                             meta["amount_usdc"], 0,
                                             error="FAK not filled — no liquidity"))
            except Exception as e:
                results.append(BuyResult(False, oid, meta["side"], meta["price"],
                                         meta["amount_usdc"], 0, error=str(e)))

        return results

    except Exception as e:
        log.error("Batch buy failed: %s", e)
        return [BuyResult(False, None, o.get("side", "?"), o.get("price", 0),
                          o.get("amount_usdc", 0), 0, error=str(e)) for o in orders]


_REDEEM_LOCK_FILE = "/tmp/polymarket_redeem.lock"

def redeem_positions(private_key: str, condition_id: str) -> bool:
    """Redeem winning tokens for USDC.e. Uses file lock to avoid nonce conflicts with polybot."""
    import fcntl
    lock_fd = None
    try:
        lock_fd = open(_REDEEM_LOCK_FILE, "w")
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (IOError, OSError):
        if lock_fd:
            lock_fd.close()
        log.info("Redeem lock held by another process — deferring %s", condition_id[:20])
        return False
    try:
        from web3 import Web3
        from eth_account import Account

        w3 = Web3(Web3.HTTPProvider(RPC_URL))
        acct = Account.from_key(private_key)
        sender = acct.address

        cid_bare = condition_id[2:] if condition_id.startswith("0x") else condition_id
        sel = Web3.keccak(text="payoutDenominator(bytes32)")[:4].hex()
        data = "0x" + sel + cid_bare.zfill(64)
        r = _direct.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "eth_call",
            "params": [{"to": CTF, "data": data}, "latest"]
        }, timeout=20)
        denom = int(r.json().get("result", "0x0"), 16)
        if denom == 0:
            return False

        erc20_abi = [{"inputs":[{"name":"account","type":"address"}],
                      "name":"balanceOf","outputs":[{"name":"","type":"uint256"}],
                      "stateMutability":"view","type":"function"}]
        usdce = w3.eth.contract(address=Web3.to_checksum_address(USDCE), abi=erc20_abi)
        bal_before = usdce.functions.balanceOf(sender).call()

        redeem_sel = Web3.keccak(
            text="redeemPositions(address,bytes32,bytes32,uint256[])"
        )[:4].hex()
        parent = "0" * 64
        cid_hex = cid_bare.zfill(64)
        offset = hex(128)[2:].zfill(64)
        arr_len = hex(1)[2:].zfill(64)

        for index_set in [1, 2]:
            arr_val = hex(index_set)[2:].zfill(64)
            tx_data = ("0x" + redeem_sel + USDCE[2:].lower().zfill(64)
                        + parent + cid_hex + offset + arr_len + arr_val)
            for attempt in range(2):
                try:
                    nonce = w3.eth.get_transaction_count(sender)
                    gas_price = w3.eth.gas_price
                    tx = {"to": Web3.to_checksum_address(CTF), "data": tx_data,
                          "gas": 200000, "gasPrice": int(gas_price * 1.2),
                          "nonce": nonce, "chainId": 137}
                    signed = w3.eth.account.sign_transaction(tx, private_key)
                    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
                    w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(3)
            time.sleep(2)

        bal_after = usdce.functions.balanceOf(sender).call()
        received = (bal_after - bal_before) / 1e6
        return received > 0
    except Exception as e:
        log.error("Redeem failed: %s", e)
        return False
    finally:
        try:
            import fcntl
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
        except Exception:
            pass
