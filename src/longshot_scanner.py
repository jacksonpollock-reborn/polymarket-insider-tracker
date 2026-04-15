"""
longshot_scanner.py — Market-level longshot fade & resolution-proximity short scanners.

EDGE (longshot fade):
  Deep longshots (<0.12) on Polymarket are systematically overpriced, consistent
  with favorite-longshot bias documented across 40+ years of prediction market
  literature. Buying NO on a deep-longshot YES token (or YES on a deep-longshot
  NO token) enters at 0.88–0.95 with ~88–95% win probability at resolution.

EDGE (resolution proximity short):
  Same bias, filtered by time-to-resolution. In the last 12–36 hours before a
  market ends, deep longshots that haven't moved are overwhelmingly resolving
  at 0. Tighter base rate, higher per-trade confidence.

Templated after src/fetchers.py::scan_market_for_arb — reuses fetch_clob_book
and the same market shape.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from src.fetchers import fetch_clob_book, extract_market_tokens

log = logging.getLogger(__name__)

# ── Longshot fade params ──────────────────────────────────────────────────────
# We target a longshot band, not just a max. Longshots below 0.05 have nearly
# zero remaining edge after we fade (remaining_edge ≈ longshot_ask), so they
# get silently rejected by paper_trader.PAPER_MIN_REMAINING_EDGE. The band
# [0.05, 0.15] means fade entries land in [0.85, 0.95] — actually tradeable.
LONGSHOT_FADE_MIN_ASK = float(os.environ.get("LONGSHOT_FADE_MIN_ASK", "0.05"))
LONGSHOT_FADE_MAX_ASK = float(os.environ.get("LONGSHOT_FADE_MAX_ASK", "0.15"))
LONGSHOT_FADE_MIN_LIQUIDITY = float(os.environ.get("LONGSHOT_FADE_MIN_LIQUIDITY", "5000"))
# Two distinct longshot-fade timeframes both exist on Polymarket:
#   - Short-dated launch/events markets that resolve within hours
#   - Long-dated championship/election/geopolitical markets (months out)
# Keep the window wide so both classes are catchable. min_days=0.1 prevents
# fading right at resolution (variance dominates). max_days=365 covers most
# long-dated markets without indefinite capital lock-up.
LONGSHOT_FADE_MIN_DAYS = float(os.environ.get("LONGSHOT_FADE_MIN_DAYS", "0.1"))
LONGSHOT_FADE_MAX_DAYS = float(os.environ.get("LONGSHOT_FADE_MAX_DAYS", "365"))

# ── Resolution proximity short params ─────────────────────────────────────────
# Same band concept: only catch longshots that produce fade entries the paper
# trader will actually trade.
RESOLUTION_SHORT_MIN_ASK = float(os.environ.get("RESOLUTION_SHORT_MIN_ASK", "0.05"))
RESOLUTION_SHORT_MAX_ASK = float(os.environ.get("RESOLUTION_SHORT_MAX_ASK", "0.15"))
RESOLUTION_SHORT_MIN_LIQUIDITY = float(os.environ.get("RESOLUTION_SHORT_MIN_LIQUIDITY", "3000"))
RESOLUTION_SHORT_MIN_DAYS = float(os.environ.get("RESOLUTION_SHORT_MIN_DAYS", "0.5"))
RESOLUTION_SHORT_MAX_DAYS = float(os.environ.get("RESOLUTION_SHORT_MAX_DAYS", "1.5"))


def _get_market_end_days(market: dict) -> float | None:
    """Compute days-to-end from market metadata. Returns None if unavailable."""
    cached = market.get("_days_to_end")
    if cached is not None:
        try:
            return float(cached)
        except (TypeError, ValueError):
            pass
    # Real Gamma API uses endDateIso / endDate; tests may use end_date_iso
    end_raw = (
        market.get("endDateIso")
        or market.get("end_date_iso")
        or market.get("endDate")
        or market.get("end_date")
    )
    if not end_raw:
        return None
    try:
        if isinstance(end_raw, str):
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
        elif isinstance(end_raw, datetime):
            end_dt = end_raw
        else:
            return None
        if end_dt.tzinfo is None:
            end_dt = end_dt.replace(tzinfo=timezone.utc)
        delta = end_dt - datetime.now(timezone.utc)
        return delta.total_seconds() / 86400.0
    except Exception:
        return None


def _best_ask(book: dict) -> tuple[float | None, float | None]:
    """Return (best_ask_price, best_ask_size) or (None, None) if book is empty.

    Legacy helper: the CLOB order books for most Polymarket markets are
    effectively empty (showing 0.001/0.999 placeholder bid/asks) because
    liquidity flows through AMM pools. Kept for test compatibility.
    """
    asks = book.get("asks", [])
    if not asks:
        return None, None
    try:
        return float(asks[0]["price"]), float(asks[0].get("size", 0))
    except (KeyError, ValueError, TypeError):
        return None, None


def _get_outcome_prices(market: dict) -> tuple[float | None, float | None]:
    """
    Extract (yes_price, no_price) from the market's `outcomePrices` field.

    This is the aggregate / AMM-pool price, which is the ACTUAL tradeable
    price for most Polymarket markets — not the CLOB best ask, which is
    usually an empty-book placeholder (0.001 / 0.999).

    Handles both shapes:
      - `outcomePrices` as JSON string (real Gamma API): '["0.72", "0.28"]'
      - `outcomePrices` as list (test fixture)
    Requires `outcomes` to order the prices correctly (YES first, NO second).
    """
    prices_raw = market.get("outcomePrices")
    outcomes_raw = market.get("outcomes")
    if not prices_raw or not outcomes_raw:
        return None, None
    try:
        prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
        outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
    except (ValueError, TypeError):
        return None, None
    if not isinstance(prices, list) or not isinstance(outcomes, list):
        return None, None
    if len(prices) != 2 or len(outcomes) != 2:
        return None, None

    yes_price = None
    no_price = None
    for outcome, price in zip(outcomes, prices):
        if not isinstance(outcome, str):
            continue
        try:
            p = float(price)
        except (ValueError, TypeError):
            continue
        if outcome.upper() == "YES":
            yes_price = p
        elif outcome.upper() == "NO":
            no_price = p
    return yes_price, no_price


def _opportunity_id(market_id: str, side: str, kind: str) -> str:
    """Deterministic 16-char alert ID so re-scans of the same market are deduped."""
    raw = f"{kind}:{market_id}:{side}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _build_opportunity(
    *,
    market: dict,
    kind: str,                    # "longshot_fade" or "resolution_short"
    bucket: str,                  # same as kind
    fade_side: str,               # "YES" or "NO" — side we BUY (opposite of longshot side)
    longshot_side: str,           # "YES" or "NO" — the deep-longshot side we're fading
    longshot_ask: float,
    fade_entry_price: float,
    fade_size: float,
    yes_token_id: str,
    no_token_id: str,
    days_to_end: float,
) -> dict:
    """
    Return a shared opportunity-dict shape. The same shape also serves as a
    synthetic alert for the paper trader, so downstream consumers don't need
    special handling.
    """
    market_id = market.get("conditionId") or market.get("condition_id") or ""
    market_name = market.get("question") or market.get("title") or ""
    category = market.get("_detected_category", "Other")
    alert_id = _opportunity_id(market_id, fade_side, kind)

    end_raw = (
        market.get("endDateIso")
        or market.get("end_date_iso")
        or market.get("endDate")
        or market.get("end_date")
    )
    market_end = ""
    if isinstance(end_raw, str):
        market_end = end_raw[:10]

    # Token list in the shape paper_trader.open_positions expects
    tokens_list = [
        {"outcome": "YES", "token_id": yes_token_id},
        {"outcome": "NO", "token_id": no_token_id},
    ]

    return {
        # ── synthetic-alert fields consumed by paper_trader ──
        "alert_id": alert_id,
        "best_bucket": bucket,
        "best_score": 60,
        "market_name": market_name,
        "market_id": market_id,
        "market_end": market_end,
        "category": category,
        "suggested_outcome": fade_side,
        "tokens": tokens_list,
        "active_exposure": {
            "entry_price": fade_entry_price,
            "dominant_outcome": fade_side,
            "dominant_usdc": 0,
        },
        "shared_features": {
            "remaining_edge_pct": round(max(0.0, 1.0 - fade_entry_price), 4),
        },
        # ── scanner-local metadata (kept separate from paper_trader fields) ──
        "kind": kind,
        "longshot_side": longshot_side,
        "longshot_ask": round(longshot_ask, 4),
        "fade_entry_price": round(fade_entry_price, 4),
        "fade_size_available": round(fade_size, 0),
        "days_to_end": round(days_to_end, 3),
        "liquidity": float(market.get("liquidity") or 0),
        "yes_token_id": yes_token_id,
        "no_token_id": no_token_id,
    }


def _scan_for_longshot(
    market: dict,
    *,
    kind: str,
    min_ask: float,
    max_ask: float,
    min_liquidity: float,
    min_days: float,
    max_days: float,
) -> dict | None:
    """
    Shared scan implementation for longshot_fade and resolution_short.

    Uses `outcomePrices` from the market metadata (aggregate / AMM pool
    price), NOT CLOB best asks — most Polymarket CLOBs for top markets
    are empty 0.001/0.999 placeholders and real liquidity flows through
    the AMM pool layer.

    Logic: if either YES or NO aggregate price is below `max_ask`, that
    side is a deep longshot and we fade it by buying the OPPOSITE side.
    """
    # Liquidity filter
    liquidity = float(market.get("liquidity") or 0)
    if liquidity < min_liquidity:
        return None

    # Time-to-end filter
    days_to_end = _get_market_end_days(market)
    if days_to_end is None:
        return None
    if days_to_end < min_days or days_to_end > max_days:
        return None

    yes_id, no_id = extract_market_tokens(market)
    if not yes_id or not no_id:
        return None

    yes_price, no_price = _get_outcome_prices(market)
    if yes_price is None or no_price is None:
        return None

    # Longshot band: min_ask < longshot < max_ask.
    # Below min_ask: fade entry is too close to 1.00 to leave tradeable edge.
    # Above max_ask: not a longshot, no bias to exploit.
    yes_is_longshot = min_ask <= yes_price < max_ask
    no_is_longshot = min_ask <= no_price < max_ask

    # Can't have both sides in the longshot band simultaneously (prices sum to ~1)
    if yes_is_longshot and no_is_longshot:
        return None
    if not yes_is_longshot and not no_is_longshot:
        return None

    if yes_is_longshot:
        longshot_side = "YES"
        longshot_ask = yes_price
        fade_side = "NO"
        fade_entry_price = no_price
    else:
        longshot_side = "NO"
        longshot_ask = no_price
        fade_side = "YES"
        fade_entry_price = yes_price

    # Sanity: fade entry must leave at least 1% remaining edge
    if fade_entry_price <= 0 or fade_entry_price >= 0.99:
        return None

    return _build_opportunity(
        market=market,
        kind=kind,
        bucket=kind,
        fade_side=fade_side,
        longshot_side=longshot_side,
        longshot_ask=longshot_ask,
        fade_entry_price=fade_entry_price,
        fade_size=0.0,  # not applicable when using aggregate prices
        yes_token_id=yes_id,
        no_token_id=no_id,
        days_to_end=days_to_end,
    )


def scan_market_for_longshot(market: dict) -> dict | None:
    """Scan a single market for a longshot-fade opportunity."""
    return _scan_for_longshot(
        market,
        kind="longshot_fade",
        min_ask=LONGSHOT_FADE_MIN_ASK,
        max_ask=LONGSHOT_FADE_MAX_ASK,
        min_liquidity=LONGSHOT_FADE_MIN_LIQUIDITY,
        min_days=LONGSHOT_FADE_MIN_DAYS,
        max_days=LONGSHOT_FADE_MAX_DAYS,
    )


def scan_market_for_resolution_short(market: dict) -> dict | None:
    """Scan a single market for a resolution-proximity short opportunity."""
    return _scan_for_longshot(
        market,
        kind="resolution_short",
        min_ask=RESOLUTION_SHORT_MIN_ASK,
        max_ask=RESOLUTION_SHORT_MAX_ASK,
        min_liquidity=RESOLUTION_SHORT_MIN_LIQUIDITY,
        min_days=RESOLUTION_SHORT_MIN_DAYS,
        max_days=RESOLUTION_SHORT_MAX_DAYS,
    )


def _batch_scan(markets: list[dict], scanner, limit: int) -> list[dict]:
    """Run `scanner` over top-N markets by 24h volume. Shared batch logic."""
    candidates = sorted(
        markets,
        key=lambda m: float(m.get("volume24hr") or 0),
        reverse=True,
    )[:limit]

    found = []
    for m in candidates:
        result = scanner(m)
        if result:
            found.append(result)
            log.info(
                f"[{result['kind'].upper()}] {result['market_name'][:50]} | "
                f"{result['longshot_side']} ask={result['longshot_ask']} | "
                f"fade {result['suggested_outcome']} @ {result['fade_entry_price']} | "
                f"{result['days_to_end']:.1f}d to end"
            )
    found.sort(key=lambda r: r["fade_entry_price"], reverse=True)
    log.info(f"[{scanner.__name__}] Scanned {len(candidates)} markets → {len(found)} opportunities")
    return found


def batch_scan_longshot(markets: list[dict], limit: int = 80) -> list[dict]:
    """Scan up to `limit` markets (sorted by 24h volume) for longshot-fade opportunities."""
    return _batch_scan(markets, scan_market_for_longshot, limit)


def batch_scan_resolution_short(markets: list[dict], limit: int = 80) -> list[dict]:
    """Scan up to `limit` markets (sorted by 24h volume) for resolution-short opportunities."""
    return _batch_scan(markets, scan_market_for_resolution_short, limit)
