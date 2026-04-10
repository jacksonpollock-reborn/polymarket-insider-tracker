"""
fetchers.py — All external API calls in one place.
Verified working endpoints as of 2026-03.
"""

import os
import time
import logging
import requests

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "Accept": "application/json",
    "User-Agent": "polymarket-insider-tracker/1.0",
})

REQUEST_HEALTH = {
    "successful_calls": 0,
    "attempt_failures": 0,
    "failed_calls": 0,
    "last_error": None,
}

GAMMA_API   = "https://gamma-api.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
CLOB_API    = "https://clob.polymarket.com"
POLYGONSCAN = "https://api.polygonscan.com/api"
ARKHAM_API  = "https://api.arkhamintelligence.com"

POLYGONSCAN_KEY = os.environ.get("POLYGONSCAN_API_KEY", "")
ARKHAM_KEY      = os.environ.get("ARKHAM_API_KEY", "")

KNOWN_MIXER_PREFIXES = {
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
}


def reset_request_health():
    REQUEST_HEALTH.update({
        "successful_calls": 0,
        "attempt_failures": 0,
        "failed_calls": 0,
        "last_error": None,
    })


def get_request_health():
    return dict(REQUEST_HEALTH)


def _get(url, params=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            REQUEST_HEALTH["successful_calls"] += 1
            return r.json()
        except Exception as e:
            REQUEST_HEALTH["attempt_failures"] += 1
            REQUEST_HEALTH["last_error"] = str(e)
            log.warning(f"GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    REQUEST_HEALTH["failed_calls"] += 1
    return None


def _post(url, json=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.post(url, json=json, headers=headers, timeout=20)
            r.raise_for_status()
            REQUEST_HEALTH["successful_calls"] += 1
            return r.json()
        except Exception as e:
            REQUEST_HEALTH["attempt_failures"] += 1
            REQUEST_HEALTH["last_error"] = str(e)
            log.warning(f"POST {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    REQUEST_HEALTH["failed_calls"] += 1
    return None


# ── Markets ────────────────────────────────────────────────────────────────────

def fetch_active_markets(limit=150, tag_slug: str = None):
    """
    Fetch active Polymarket markets sorted by 24h volume.
    Pass tag_slug to restrict to a specific category (e.g. 'politics', 'crypto').
    """
    markets, offset = [], 0
    while len(markets) < limit:
        params = {
            "active": "true", "closed": "false",
            "limit": 50, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        }
        if tag_slug:
            params["tag_slug"] = tag_slug
        batch = _get(f"{GAMMA_API}/markets", params=params)
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    log.info(f"[Polymarket] {len(markets)} active markets fetched" + (f" (tag={tag_slug})" if tag_slug else ""))
    return markets[:limit]


def fetch_markets_by_tags(tag_slugs: list[str], per_tag_limit: int = 50) -> list[dict]:
    """
    Fetch markets for each tag slug and return a deduplicated combined list,
    sorted by 24h volume descending. Used to prioritise non-sports categories.
    """
    seen, combined = set(), []
    for tag in tag_slugs:
        batch = fetch_active_markets(limit=per_tag_limit, tag_slug=tag)
        for m in batch:
            mid = m.get("conditionId") or m.get("condition_id") or m.get("id")
            if mid and mid not in seen:
                seen.add(mid)
                combined.append(m)
    # Sort combined by 24h volume desc so the best markets surface first
    combined.sort(key=lambda m: float(m.get("volume24hr") or 0), reverse=True)
    return combined


# ── Trades per market ──────────────────────────────────────────────────────────

def fetch_market_trades(condition_id, limit=100):
    """
    Fetch recent trades for a market via data-api /trades.
    Returns list of trade dicts with keys:
      proxyWallet, side, size, price, outcome, timestamp, title, conditionId
    Note: size is share quantity. USDC value = size * price.

    Redemption filter: when a market resolves, winners redeem shares at price=1.00.
    These show up as SELL at price 1.00 in the API but are NOT real sell trades —
    they are just prize collection. We filter them out to avoid false signals.
    """
    result = _get(f"{DATA_API}/trades", params={"market": condition_id, "limit": limit})
    time.sleep(0.15)

    if isinstance(result, dict):
        result = result.get("data") or result.get("results") or []
    if not isinstance(result, list):
        return []

    # Filter out redemptions: price >= 0.99 = resolved market payout
    # This applies to BOTH sides:
    #   SELL at 1.00 = winning side collecting payout
    #   BUY  at 1.00 = also seen in some redemption flows
    # Real trades never happen at price >= 0.99 (no rational buyer pays $1 for a $1 max payout)
    filtered = []
    for t in result:
        price = float(t.get("price") or 0)
        if price >= 0.99:
            continue   # skip all redemptions regardless of side
        filtered.append(t)

    return filtered


# ── Wallet data ────────────────────────────────────────────────────────────────

def fetch_wallet_activity(address):
    result = _get(f"{DATA_API}/activity", params={"user": address, "limit": 500})
    time.sleep(0.15)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data") or result.get("results") or []
    return []


def fetch_wallet_positions(address):
    result = _get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": "0"})
    time.sleep(0.15)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data") or result.get("positions") or []
    return []


# ── Dune Analytics ────────────────────────────────────────────────────────────
# Queries pre-created on Dune (free tier can execute existing queries via API)
DUNE_API        = "https://api.dune.com/api/v1"
DUNE_KEY        = os.environ.get("DUNE_API_KEY", "")
DUNE_QUERY_WHALE_WALLETS     = "6807889"
DUNE_QUERY_NEW_LARGE_BETTORS = "6807896"


def _dune_execute_and_fetch(query_id: str) -> list[dict]:
    """Execute a pre-existing Dune query and return rows."""
    if not DUNE_KEY:
        log.warning("[Dune] No API key — skipping")
        return []

    headers = {"X-Dune-API-Key": DUNE_KEY}

    # Trigger execution
    exec_resp = _post(
        f"{DUNE_API}/query/{query_id}/execute",
        json={"performance": "medium"},
        headers=headers,
    )
    if not exec_resp:
        log.warning(f"[Dune] Execute query {query_id} failed before execution_id was returned")
        return []

    execution_id = exec_resp.get("execution_id")
    if not execution_id:
        return []

    # Poll for completion (max 90s)
    for _ in range(18):
        time.sleep(5)
        result = _get(
            f"{DUNE_API}/execution/{execution_id}/results",
            headers=headers,
        )
        if not result:
            continue
        state = result.get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            rows = result.get("result", {}).get("rows", [])
            log.info(f"[Dune] Query {query_id} returned {len(rows)} rows")
            return rows
        if "FAILED" in state or "CANCELLED" in state:
            log.warning(f"[Dune] Query {query_id} state: {state}")
            return []

    log.warning(f"[Dune] Query {query_id} timed out")
    return []


def fetch_dune_volume_spikes() -> list[dict]:
    return []


def fetch_dune_whale_wallets() -> list[str]:
    rows = _dune_execute_and_fetch(DUNE_QUERY_WHALE_WALLETS)
    return [r.get("wallet") for r in rows if r.get("wallet")]


def fetch_dune_new_large_bettors() -> list[str]:
    rows = _dune_execute_and_fetch(DUNE_QUERY_NEW_LARGE_BETTORS)
    return [r.get("wallet") for r in rows if r.get("wallet")]


# ── Polygonscan ────────────────────────────────────────────────────────────────

def fetch_polygon_tx_history(address):
    empty = {"tx_count": 0, "first_tx_timestamp": None, "usdc_inflows": [], "funding_flags": []}
    if not POLYGONSCAN_KEY:
        log.warning("[Polygonscan] No API key — skipping")
        return empty

    USDC_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"
    transfers = _get(POLYGONSCAN, params={
        "module": "account", "action": "tokentx",
        "contractaddress": USDC_CONTRACT,
        "address": address, "sort": "asc",
        "apikey": POLYGONSCAN_KEY,
    })
    if not transfers or transfers.get("status") != "1":
        return empty

    txs     = transfers.get("result", [])
    inflows = [t for t in txs if t.get("to", "").lower() == address.lower()]
    result  = {
        "tx_count":           len(txs),
        "first_tx_timestamp": int(inflows[0]["timeStamp"]) if inflows else None,
        "usdc_inflows":       [],
        "funding_flags":      [],
    }
    for tx in inflows:
        from_addr  = tx.get("from", "").lower()
        value_usdc = int(tx.get("value", 0)) / 1e6
        ts         = int(tx.get("timeStamp", 0))
        result["usdc_inflows"].append({"from": from_addr, "value_usdc": value_usdc, "timestamp": ts})
        if any(from_addr.startswith(m) for m in KNOWN_MIXER_PREFIXES):
            result["funding_flags"].append(f"⚠️ Funded by known mixer: {from_addr[:12]}…")
        bridge_prefixes = ["0x7ceb", "0x2c0", "0x8484", "0x40ec"]
        if any(from_addr.startswith(p) for p in bridge_prefixes):
            result["funding_flags"].append(f"🌉 Bridged USDC from external chain (${value_usdc:,.0f})")
    return result


# ── Arkham ─────────────────────────────────────────────────────────────────────

def fetch_arkham_entity(address):
    if not ARKHAM_KEY:
        log.warning("[Arkham] No API key — skipping")
        return {}
    headers = {"API-Key": ARKHAM_KEY}
    data    = _get(f"{ARKHAM_API}/intelligence/address/{address}", headers=headers)
    time.sleep(0.25)
    if not data:
        return {}
    entity = data.get("arkhamEntity") or {}
    return {
        "label":             entity.get("name") or "Unknown",
        "type":              entity.get("type") or "unknown",
        "website":           entity.get("website"),
        "twitter":           entity.get("twitter"),
        "cluster_size":      len(data.get("cluster", [])),
        "related_addresses": [
            c.get("address") for c in data.get("cluster", [])[:5] if c.get("address")
        ],
    }


# ── Arbitrage Scanner ──────────────────────────────────────────────────────────
# Polymarket CLOB: YES ask + NO ask < 1.0 → guaranteed profit at resolution.
# With maker (limit) orders, Polymarket charges ZERO fees. With taker (market)
# orders, the fee formula is: shares × feeRate × p × (1-p), where feeRate varies
# by category (sports=0.03, crypto=0.072, politics=0.04, geopolitics=0).
# Since arb execution should use limit orders for zero fees, the threshold is
# set just below 1.0 to account for rounding and execution slippage only.
ARB_THRESHOLD = float(os.environ.get("ARB_THRESHOLD", "0.995"))

# Category-specific taker fee rates (only used if executing as taker)
TAKER_FEE_RATES = {
    "Sports": 0.03,
    "Crypto": 0.072,
    "Politics": 0.04,
    "Finance": 0.04,
    "Other": 0.05,
}


def estimate_taker_fee(shares: float, price: float, category: str = "Other") -> float:
    """Estimate Polymarket taker fee: shares × feeRate × p × (1-p)."""
    rate = TAKER_FEE_RATES.get(category, 0.05)
    return shares * rate * price * (1 - price)

def fetch_clob_book(token_id: str) -> dict:
    """
    Fetch the live order book for a single token from Polymarket's CLOB.
    Returns dict with 'bids' and 'asks' lists, each item {"price": str, "size": str}.
    Asks are sorted ascending (best ask = asks[0]).
    """
    result = _get(f"{CLOB_API}/book", params={"token_id": token_id})
    time.sleep(0.12)   # 8 req/s safe rate
    return result or {}


def scan_market_for_arb(market: dict) -> dict | None:
    """
    Check a single market for direct arbitrage.

    Fetches live best-ask prices for YES and NO tokens from the CLOB.
    If best_ask_yes + best_ask_no < ARB_THRESHOLD (0.97), a risk-free
    profit exists: buy both sides for < $0.97, collect $1.00 at resolution.

    Returns an arb dict on opportunity, None otherwise.
    """
    tokens = market.get("tokens") or []
    if len(tokens) < 2:
        return None

    yes_token = next((t for t in tokens if str(t.get("outcome", "")).upper() == "YES"), None)
    no_token  = next((t for t in tokens if str(t.get("outcome", "")).upper() == "NO"),  None)

    if not yes_token or not no_token:
        return None

    yes_id = yes_token.get("token_id") or yes_token.get("tokenId")
    no_id  = no_token.get("token_id")  or no_token.get("tokenId")
    if not yes_id or not no_id:
        return None

    yes_book = fetch_clob_book(yes_id)
    no_book  = fetch_clob_book(no_id)

    yes_asks = yes_book.get("asks", [])
    no_asks  = no_book.get("asks",  [])
    if not yes_asks or not no_asks:
        return None

    try:
        yes_ask  = float(yes_asks[0]["price"])
        no_ask   = float(no_asks[0]["price"])
        yes_size = float(yes_asks[0].get("size", 0))
        no_size  = float(no_asks[0].get("size",  0))
    except (KeyError, ValueError, TypeError):
        return None

    combined = yes_ask + no_ask
    if combined >= ARB_THRESHOLD:
        return None

    # Max fillable at the best ask levels (limited by smaller side)
    max_fill_shares = min(yes_size, no_size)
    gross_profit_per_share = 1.0 - combined
    category = market.get("_detected_category", "Other")

    # With limit orders (maker), fees are zero. Show both maker and taker P&L.
    maker_profit_per_share = gross_profit_per_share
    taker_fee_yes = estimate_taker_fee(1, yes_ask, category)
    taker_fee_no = estimate_taker_fee(1, no_ask, category)
    taker_profit_per_share = gross_profit_per_share - taker_fee_yes - taker_fee_no

    max_profit_maker = round(maker_profit_per_share * max_fill_shares, 2)
    max_profit_taker = round(taker_profit_per_share * max_fill_shares, 2)

    return {
        "market":            market.get("question") or market.get("title", ""),
        "market_id":         market.get("conditionId") or market.get("condition_id"),
        "yes_ask":           round(yes_ask, 4),
        "no_ask":            round(no_ask, 4),
        "combined":          round(combined, 4),
        "arb_pct":           round(gross_profit_per_share * 100, 2),
        "net_arb_pct":       round(maker_profit_per_share * 100, 2),
        "taker_arb_pct":     round(taker_profit_per_share * 100, 2),
        "max_fill_shares":   round(max_fill_shares, 0),
        "max_profit_usdc":   max_profit_maker,
        "max_profit_taker":  max_profit_taker,
        "yes_token_id":      yes_id,
        "no_token_id":       no_id,
        "liquidity":         float(market.get("liquidity") or 0),
        "category":          category,
        "days_to_end":       market.get("_days_to_end", 0),
    }


def batch_scan_arb(markets: list[dict], limit: int = 80) -> list[dict]:
    """
    Scan up to `limit` markets (sorted by 24h volume) for arb opportunities.
    Returns list of arb dicts sorted by net_arb_pct descending.
    """
    candidates = sorted(
        markets,
        key=lambda m: float(m.get("volume24hr") or 0),
        reverse=True,
    )[:limit]

    found = []
    for m in candidates:
        arb = scan_market_for_arb(m)
        if arb:
            found.append(arb)
            log.info(
                f"[ARB] {arb['market'][:55]} | "
                f"combined={arb['combined']} | +{arb['net_arb_pct']}% net | "
                f"max ~${arb['max_profit_usdc']:,.0f}"
            )

    found.sort(key=lambda a: a["net_arb_pct"], reverse=True)
    log.info(f"[ARB] Scanned {len(candidates)} markets → {len(found)} arb opportunities found")
    return found
