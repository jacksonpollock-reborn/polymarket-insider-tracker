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

GAMMA_API   = "https://gamma-api.polymarket.com"
DATA_API    = "https://data-api.polymarket.com"
POLYGONSCAN = "https://api.polygonscan.com/api"
ARKHAM_API  = "https://api.arkhamintelligence.com"

POLYGONSCAN_KEY = os.environ.get("POLYGONSCAN_API_KEY", "")
ARKHAM_KEY      = os.environ.get("ARKHAM_API_KEY", "")

KNOWN_MIXER_PREFIXES = {
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
}


def _get(url, params=None, headers=None, retries=3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


# ── Markets ────────────────────────────────────────────────────────────────────

def fetch_active_markets(limit=150):
    markets, offset = [], 0
    while len(markets) < limit:
        batch = _get(f"{GAMMA_API}/markets", params={
            "active": "true", "closed": "false",
            "limit": 50, "offset": offset,
            "order": "volume24hr", "ascending": "false",
        })
        if not batch:
            break
        markets.extend(batch)
        if len(batch) < 50:
            break
        offset += 50
    log.info(f"[Polymarket] {len(markets)} active markets fetched")
    return markets[:limit]


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

    # Filter out redemptions: price >= 0.99 on a SELL = resolved market payout
    filtered = []
    for t in result:
        price = float(t.get("price") or 0)
        side  = (t.get("side") or "").upper()
        if side in ("SELL", "NO") and price >= 0.99:
            continue   # skip redemption
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
    exec_resp = SESSION.post(
        f"{DUNE_API}/query/{query_id}/execute",
        json={"performance": "medium"},
        headers=headers,
        timeout=20,
    )
    if exec_resp.status_code != 200:
        log.warning(f"[Dune] Execute query {query_id} failed: {exec_resp.status_code} {exec_resp.text[:150]}")
        return []

    execution_id = exec_resp.json().get("execution_id")
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
