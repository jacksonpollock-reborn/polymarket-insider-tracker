"""
fetchers.py — All external API calls in one place.
Each function returns None on failure so the scorer can degrade gracefully.
"""

import os
import time
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"Accept": "application/json", "User-Agent": "polymarket-insider-tracker/1.0"})

# ── API base URLs ──────────────────────────────────────────────────────────────
GAMMA_API    = "https://gamma-api.polymarket.com"
DATA_API     = "https://data-api.polymarket.com"
CLOB_API     = "https://clob.polymarket.com"
POLYGONSCAN  = "https://api.polygonscan.com/api"
ARKHAM_API   = "https://api.arkhamintelligence.com"
DUNE_API     = "https://api.dune.com/api/v1"

# ── API keys from environment ──────────────────────────────────────────────────
POLYGONSCAN_KEY = os.environ.get("POLYGONSCAN_API_KEY", "")
ARKHAM_KEY      = os.environ.get("ARKHAM_API_KEY", "")
DUNE_KEY        = os.environ.get("DUNE_API_KEY", "")

KNOWN_MIXER_PREFIXES = {
    "0x722122df12d4e14e13ac3b6895a86e84145b6967",  # Tornado Cash Router
    "0xd90e2f925da726b50c4ed8d0fb90ad053324f31b",
    "0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936",
}

# ── Generic HTTP GET with retry ────────────────────────────────────────────────
def _get(url: str, params: dict = None, headers: dict = None, retries: int = 3):
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, headers=headers, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            log.warning(f"GET {url} attempt {attempt+1} failed: {e}")
            time.sleep(2 ** attempt)
    return None


# ══════════════════════════════════════════════════════════════════════════════
# POLYMARKET
# ══════════════════════════════════════════════════════════════════════════════

def fetch_active_markets(limit: int = 150) -> list[dict]:
    """Fetch active markets ordered by 24h volume (all categories)."""
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


def fetch_market_trades(condition_id: str, limit: int = 100) -> list[dict]:
    """
    Fetch recent large trades for a market.
    Tries CLOB trades endpoint first, falls back to fetching top position holders.
    """
    # Try CLOB API trades endpoint (uses market conditionId)
    result = _get(f"{CLOB_API}/trades", params={"market": condition_id, "limit": limit})
    time.sleep(0.1)
    if isinstance(result, list) and result:
        return result
    if isinstance(result, dict):
        trades = result.get("data") or result.get("results") or []
        if trades:
            return trades

    # Fallback: fetch top liquidity positions in this market from gamma API
    positions = _get(f"{GAMMA_API}/positions", params={"conditionId": condition_id, "limit": limit})
    time.sleep(0.1)
    if isinstance(positions, list):
        return positions
    if isinstance(positions, dict):
        return positions.get("data", [])
    return []


def fetch_wallet_activity(address: str) -> list[dict]:
    """All trades a wallet has made on Polymarket."""
    result = _get(f"{DATA_API}/activity", params={"user": address, "limit": 500})
    time.sleep(0.15)
    return result if isinstance(result, list) else []


def fetch_wallet_positions(address: str) -> list[dict]:
    """Current + historical positions for a wallet."""
    result = _get(f"{DATA_API}/positions", params={"user": address, "sizeThreshold": "0"})
    time.sleep(0.15)
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data", [])
    return []


def fetch_wallet_pnl(address: str) -> dict | None:
    """Profit/loss summary for a wallet."""
    result = _get(f"{DATA_API}/value", params={"user": address})
    time.sleep(0.1)
    return result if isinstance(result, dict) else None


# ══════════════════════════════════════════════════════════════════════════════
# POLYGONSCAN
# ══════════════════════════════════════════════════════════════════════════════

def fetch_polygon_tx_history(address: str, days_back: int = 90) -> dict:
    """
    Returns:
      - tx_count: total polygon transactions
      - first_tx_timestamp: unix ts of earliest tx (wallet age proxy)
      - usdc_inflows: list of {from, value, timestamp} for USDC transfers in
      - funding_flags: list of human-readable warnings
    """
    if not POLYGONSCAN_KEY:
        log.warning("[Polygonscan] No API key — skipping on-chain funding analysis")
        return {}

    # USDC on Polygon
    USDC_CONTRACT = "0x2791bca1f2de4661ed88a30c99a7a9449aa84174"

    # Fetch ERC-20 USDC transfers TO this wallet
    transfers = _get(POLYGONSCAN, params={
        "module": "account", "action": "tokentx",
        "contractaddress": USDC_CONTRACT,
        "address": address, "sort": "asc",
        "apikey": POLYGONSCAN_KEY,
    })

    result = {
        "tx_count": 0,
        "first_tx_timestamp": None,
        "usdc_inflows": [],
        "funding_flags": [],
    }

    if not transfers or transfers.get("status") != "1":
        return result

    txs = transfers.get("result", [])
    result["tx_count"] = len(txs)

    inflows = [t for t in txs if t.get("to", "").lower() == address.lower()]
    if inflows:
        result["first_tx_timestamp"] = int(inflows[0]["timeStamp"])

    for tx in inflows:
        from_addr = tx.get("from", "").lower()
        value_usdc = int(tx.get("value", 0)) / 1e6
        ts = int(tx.get("timeStamp", 0))

        inflow = {"from": from_addr, "value_usdc": value_usdc, "timestamp": ts}
        result["usdc_inflows"].append(inflow)

        # Flag mixer funding
        if any(from_addr.startswith(m) for m in KNOWN_MIXER_PREFIXES):
            result["funding_flags"].append(f"⚠️ Funded by known mixer: {from_addr[:12]}…")

        # Flag bridge funding (Polygon bridges have recognisable hot wallets)
        bridge_keywords = ["bridge", "hop", "stargate", "across", "socket"]
        tx_hash_info = tx.get("tokenName", "").lower()
        if any(k in from_addr for k in ["0x7ceb", "0x2c0"]) or any(k in tx_hash_info for k in bridge_keywords):
            result["funding_flags"].append(f"🌉 Bridged USDC from external chain (${value_usdc:,.0f})")

    return result


# ══════════════════════════════════════════════════════════════════════════════
# DUNE ANALYTICS
# ══════════════════════════════════════════════════════════════════════════════

# ── Inline SQL queries (no pre-saved query IDs needed) ────────────────────────
DUNE_SQL_WHALE_WALLETS = """
SELECT
    trader_address AS wallet,
    SUM(usdc_amount) AS total_usdc
FROM polymarket.trades
WHERE block_time >= NOW() - INTERVAL '24' HOUR
  AND usdc_amount >= 5000
GROUP BY trader_address
HAVING SUM(usdc_amount) >= 10000
ORDER BY total_usdc DESC
LIMIT 50
"""

DUNE_SQL_NEW_LARGE_BETTORS = """
SELECT trader_address AS wallet
FROM polymarket.trades
WHERE block_time >= NOW() - INTERVAL '24' HOUR
  AND usdc_amount >= 5000
  AND trader_address NOT IN (
      SELECT DISTINCT trader_address
      FROM polymarket.trades
      WHERE block_time < NOW() - INTERVAL '30' DAY
  )
GROUP BY trader_address
LIMIT 50
"""

def _dune_run_sql(sql: str) -> list[dict] | None:
    """Execute an ad-hoc SQL query via Dune API and return rows."""
    if not DUNE_KEY:
        log.warning("[Dune] No API key — skipping Dune queries")
        return None

    headers = {"X-Dune-API-Key": DUNE_KEY, "Content-Type": "application/json"}

    # Create a one-off query
    create_resp = SESSION.post(
        f"{DUNE_API}/query",
        json={"name": "polymarket-insider-tracker-adhoc", "query_sql": sql},
        headers=headers, timeout=20
    )
    if create_resp.status_code not in (200, 201):
        log.warning(f"[Dune] Failed to create query: {create_resp.status_code} {create_resp.text[:200]}")
        return None

    query_id = create_resp.json().get("query_id")
    if not query_id:
        return None

    # Execute
    exec_resp = SESSION.post(
        f"{DUNE_API}/query/{query_id}/execute",
        json={}, headers=headers, timeout=20
    )
    if exec_resp.status_code != 200:
        log.warning(f"[Dune] Failed to execute query {query_id}: {exec_resp.text[:200]}")
        return None

    execution_id = exec_resp.json().get("execution_id")
    if not execution_id:
        return None

    # Poll for results (max 90s)
    for _ in range(18):
        time.sleep(5)
        result_resp = _get(
            f"{DUNE_API}/execution/{execution_id}/results",
            headers=headers
        )
        if not result_resp:
            continue
        state = result_resp.get("state", "")
        if state == "QUERY_STATE_COMPLETED":
            return result_resp.get("result", {}).get("rows", [])
        if "FAILED" in state or "CANCELLED" in state:
            log.warning(f"[Dune] Query execution {state}")
            return None

    log.warning("[Dune] Query timed out after 90s")
    return None


def fetch_dune_volume_spikes() -> list[dict]:
    """Placeholder — volume spike detection handled via Polymarket Gamma API directly."""
    return []


def fetch_dune_whale_wallets() -> list[str]:
    """Wallets that placed large USDC bets on Polymarket in last 24h."""
    rows = _dune_run_sql(DUNE_SQL_WHALE_WALLETS)
    if not rows:
        return []
    return [r.get("wallet") for r in rows if r.get("wallet")]


def fetch_dune_new_large_bettors() -> list[str]:
    """Brand-new wallets (no history before 30 days ago) making large bets."""
    rows = _dune_run_sql(DUNE_SQL_NEW_LARGE_BETTORS)
    if not rows:
        return []
    return [r.get("wallet") for r in rows if r.get("wallet")]


# ══════════════════════════════════════════════════════════════════════════════
# ARKHAM INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

def fetch_arkham_entity(address: str) -> dict:
    """
    Returns entity label, entity type, and related addresses from Arkham.
    Falls back to empty dict if unavailable.
    """
    if not ARKHAM_KEY:
        log.warning("[Arkham] No API key — skipping entity resolution")
        return {}

    headers = {"API-Key": ARKHAM_KEY}
    data = _get(f"{ARKHAM_API}/intelligence/address/{address}", headers=headers)
    time.sleep(0.2)

    if not data:
        return {}

    entity = data.get("arkhamEntity") or {}
    return {
        "label":        entity.get("name") or "Unknown",
        "type":         entity.get("type") or "unknown",
        "website":      entity.get("website"),
        "twitter":      entity.get("twitter"),
        "cluster_size": len(data.get("cluster", [])),
        "related_addresses": [
            c.get("address") for c in data.get("cluster", [])[:5] if c.get("address")
        ],
    }


def fetch_arkham_transfers(address: str, limit: int = 20) -> list[dict]:
    """Recent large transfers in/out — useful for spotting coordinated wallets."""
    if not ARKHAM_KEY:
        return []

    headers = {"API-Key": ARKHAM_KEY}
    data = _get(
        f"{ARKHAM_API}/transfers",
        params={"base": address, "limit": limit, "sortKey": "time", "sortDir": "desc"},
        headers=headers,
    )
    time.sleep(0.2)
    if not data:
        return []
    return data.get("transfers", [])
