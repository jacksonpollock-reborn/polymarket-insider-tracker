"""
fetchers.py — All external API calls in one place.
Each function returns None/empty on failure so the scorer degrades gracefully.

Endpoint status (verified 2026-03):
  ✅ gamma-api.polymarket.com/markets          — public, no auth
  ✅ gamma-api.polymarket.com/positions        — public, needs market_id (numeric)
  ✅ data-api.polymarket.com/activity          — public, needs ?user=ADDRESS
  ✅ data-api.polymarket.com/positions         — public, needs ?user=ADDRESS
  ✅ api.polygonscan.com                       — needs API key
  ✅ api.arkhamintelligence.com                — needs API key
  ❌ clob.polymarket.com/trades                — requires auth (401)
  ❌ dune.com query creation                   — paid plan only (403)
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


def fetch_market_top_holders(market_id, limit=50):
    """
    Top position holders for a market.
    market_id: numeric 'id' from markets API (NOT conditionId).
    """
    result = _get(f"{GAMMA_API}/positions", params={
        "market_id": market_id,
        "limit": limit,
        "sortBy": "currentValue",
        "order": "DESC",
    })
    time.sleep(0.15)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data") or result.get("positions") or []
    return []


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


# ── Dune — disabled (free tier cannot create queries) ─────────────────────────

def fetch_dune_volume_spikes():
    log.info("[Dune] Skipped — requires paid plan.")
    return []

def fetch_dune_whale_wallets():
    return []

def fetch_dune_new_large_bettors():
    return []


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
