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
import logging
import os
from datetime import datetime, timezone

from src.fetchers import fetch_clob_book, extract_market_tokens

log = logging.getLogger(__name__)

# ── Longshot fade params ──────────────────────────────────────────────────────
LONGSHOT_FADE_MAX_ASK = float(os.environ.get("LONGSHOT_FADE_MAX_ASK", "0.12"))
LONGSHOT_FADE_MIN_LIQUIDITY = float(os.environ.get("LONGSHOT_FADE_MIN_LIQUIDITY", "5000"))
LONGSHOT_FADE_MIN_DAYS = float(os.environ.get("LONGSHOT_FADE_MIN_DAYS", "1"))
LONGSHOT_FADE_MAX_DAYS = float(os.environ.get("LONGSHOT_FADE_MAX_DAYS", "30"))

# ── Resolution proximity short params ─────────────────────────────────────────
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
    """Return (best_ask_price, best_ask_size) or (None, None) if book is empty."""
    asks = book.get("asks", [])
    if not asks:
        return None, None
    try:
        return float(asks[0]["price"]), float(asks[0].get("size", 0))
    except (KeyError, ValueError, TypeError):
        return None, None


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
    max_ask: float,
    min_liquidity: float,
    min_days: float,
    max_days: float,
) -> dict | None:
    """
    Shared scan implementation for longshot_fade and resolution_short.

    Logic: fetch YES and NO order books. If EITHER side's best ask is below
    `max_ask`, that side is a deep longshot and we fade it by buying the
    OPPOSITE side at (1 - longshot_ask) ≈ high price.
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

    yes_book = fetch_clob_book(yes_id)
    no_book = fetch_clob_book(no_id)
    yes_ask, yes_size = _best_ask(yes_book)
    no_ask, no_size = _best_ask(no_book)

    if yes_ask is None or no_ask is None:
        return None

    # Determine which side (if any) is a deep longshot
    yes_is_longshot = yes_ask < max_ask
    no_is_longshot = no_ask < max_ask

    # If both sides show a deep longshot, the book is broken — skip
    if yes_is_longshot and no_is_longshot:
        return None
    if not yes_is_longshot and not no_is_longshot:
        return None

    if yes_is_longshot:
        longshot_side = "YES"
        longshot_ask = yes_ask
        # Fade by buying NO at its best ask (not 1 - yes_ask, because book prices
        # are set independently and may diverge slightly)
        fade_side = "NO"
        fade_entry_price = no_ask
        fade_size = no_size
    else:
        longshot_side = "NO"
        longshot_ask = no_ask
        fade_side = "YES"
        fade_entry_price = yes_ask
        fade_size = yes_size

    # Sanity: fade entry must leave some remaining edge to target
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
        fade_size=fade_size,
        yes_token_id=yes_id,
        no_token_id=no_id,
        days_to_end=days_to_end,
    )


def scan_market_for_longshot(market: dict) -> dict | None:
    """Scan a single market for a longshot-fade opportunity."""
    return _scan_for_longshot(
        market,
        kind="longshot_fade",
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
