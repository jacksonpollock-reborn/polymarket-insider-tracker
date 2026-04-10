"""
reporter.py — Formats the strategy watchlist into email and Telegram digests.
"""

from __future__ import annotations

import html
import logging
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

try:
    import requests as _requests
except ImportError:  # pragma: no cover - lets report rendering tests run without deps installed
    _requests = None

log = logging.getLogger(__name__)

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO = os.environ.get("EMAIL_TO", GMAIL_USER)
EMAIL_RETRY_COUNT = int(os.environ.get("EMAIL_RETRY_COUNT", "1"))
EMAIL_RETRY_DELAY_SECONDS = int(os.environ.get("EMAIL_RETRY_DELAY_SECONDS", "30"))

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_API = "https://api.telegram.org"

EMAIL_BUCKET_ORDER = ["insider", "sports_news", "momentum", "contrarian"]
EMAIL_BUCKET_LIMIT = int(os.environ.get("EMAIL_BUCKET_LIMIT", "5"))
BUCKET_LABELS = {
    "insider": "Insider Strategy",
    "sports_news": "Sports News Strategy",
    "momentum": "Momentum Strategy",
    "contrarian": "Contrarian Strategy",
}
BUCKET_DESCRIPTIONS = {
    "insider": "Politics, finance, crypto-event, and asymmetric-information style alerts.",
    "sports_news": "Sports-only late-news, lineup, injury, or weather-driven alerts.",
    "momentum": "Follow-through and price-discovery alerts where the move is still running.",
    "contrarian": "Fade or reversal alerts where the move already looks stretched.",
}
BUCKET_COLORS = {
    "insider": ("#2b6cb0", "#63b3ed"),
    "sports_news": ("#b7791f", "#f6ad55"),
    "momentum": ("#2f855a", "#68d391"),
    "contrarian": ("#c05621", "#f6ad55"),
}


def _html(value) -> str:
    if value is None:
        return ""
    return html.escape(str(value), quote=True)


def _tg_escape(text: str) -> str:
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def send_telegram_message(text: str, parse_mode: str = "MarkdownV2") -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("[Telegram] Not configured — set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID")
        return False
    if _requests is None:
        log.warning("[Telegram] requests is not installed")
        return False
    try:
        response = _requests.post(
            f"{TELEGRAM_API}/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": parse_mode},
            timeout=10,
        )
        if not response.ok:
            log.error(f"[Telegram] Send failed: {response.status_code} {response.text[:200]}")
            return False
        return True
    except Exception as exc:
        log.error(f"[Telegram] Error: {exc}")
        return False


def _score_color(score: int) -> str:
    if score >= 70:
        return "#e53e3e"
    if score >= 55:
        return "#dd6b20"
    if score >= 40:
        return "#d69e2e"
    return "#38a169"


def _group_watchlist(watchlist: list[dict]) -> dict[str, list[dict]]:
    grouped = {bucket: [] for bucket in EMAIL_BUCKET_ORDER}
    for alert in watchlist:
        grouped.setdefault(alert.get("best_bucket", "insider"), []).append(alert)
    for bucket in grouped:
        grouped[bucket].sort(key=lambda alert: (-alert.get("best_score", 0), -alert.get("candidate_score", 0)))
    return grouped


def _is_thin_edge_follow(alert: dict) -> bool:
    return bool(alert.get("thin_edge_follow"))


def _split_bucket_alerts(alerts: list[dict]) -> tuple[list[dict], list[dict]]:
    primary = [alert for alert in alerts if not _is_thin_edge_follow(alert)]
    thin_edge = [alert for alert in alerts if _is_thin_edge_follow(alert)]
    return primary, thin_edge


def _build_action_text(alert: dict) -> str:
    action = (alert.get("recommended_action") or "follow").lower()
    outcome = alert.get("suggested_outcome")
    if outcome:
        return f"{action.title()} {outcome}"
    return action.title()


def send_telegram_alerts(watchlist: list[dict], stats: dict, arb_alerts: list | None = None) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False

    arb_alerts = arb_alerts or []
    bucketed = _group_watchlist(watchlist)
    run_date = datetime.now(timezone.utc).strftime("%b %d %H:%M UTC")

    def _send_block(lines: list[str]) -> bool:
        message = "\n".join(lines)
        if len(message) > 4000:
            message = message[:3950] + "\n\\.\\.\\. \\(truncated\\)"
        return send_telegram_message(message)

    ok = True
    ok = _send_block([
        f"🔍 *Polymarket Strategy Tracker* · {_tg_escape(run_date)}",
        "",
        f"📊 *{stats.get('flagged_alerts', 0)} alerts* · *{len(arb_alerts)} arb*",
        f"Markets scanned: {stats.get('markets_scanned', 0)} · Large trades: {stats.get('large_trades', 0)}",
        f"Buckets: insider {stats.get('insider_watchlist', 0)} · sports {stats.get('sports_watchlist', 0)} · "
        f"momentum {stats.get('momentum_watchlist', 0)} · contrarian {stats.get('contrarian_watchlist', 0)}",
    ]) and ok

    if arb_alerts:
        arb_lines = ["", "💰 *ARBITRAGE OPPORTUNITIES*"]
        for arb in arb_alerts[:4]:
            arb_lines.extend([
                "",
                f"• {_tg_escape(arb['market'][:55])}",
                f"  YES ask `{arb['yes_ask']}` \\+ NO ask `{arb['no_ask']}` \\= `{arb['combined']}`",
                f"  Net edge: *\\+{arb['net_arb_pct']}%* \\| Max profit: `${arb.get('max_profit_usdc', 0):,.0f}`",
            ])
        ok = _send_block(arb_lines) and ok

    bucket_lines = []
    thin_edge_lines = []
    for bucket in EMAIL_BUCKET_ORDER:
        primary_alerts, thin_edge_alerts = _split_bucket_alerts(bucketed.get(bucket, []))
        alerts = primary_alerts[:2]
        if alerts:
            bucket_lines.extend(["", f"*{_tg_escape(BUCKET_LABELS[bucket])}*"])
            for alert in alerts:
                exposure = alert.get("active_exposure", {})
                bucket_lines.extend([
                    f"• {_tg_escape(alert['market_name'][:60])}",
                    f"  {_tg_escape(alert['wallet_address'][:10])}… \\| score *{alert['best_score']}* \\| candidate {alert['candidate_score']}",
                    f"  Action: *{_tg_escape(_build_action_text(alert))}* \\| size `${exposure.get('dominant_usdc', 0):,.0f}`",
                ])

        thin_alerts = thin_edge_alerts[:2]
        if not thin_alerts:
            continue
        thin_edge_lines.extend(["", f"*{_tg_escape(BUCKET_LABELS[bucket])} — Thin Edge*"])
        for alert in thin_alerts:
            exposure = alert.get("active_exposure", {})
            thin_edge_lines.extend([
                f"• {_tg_escape(alert['market_name'][:60])}",
                f"  {_tg_escape(alert['wallet_address'][:10])}… \\| score *{alert['best_score']}* \\| candidate {alert['candidate_score']}",
                f"  Action: *{_tg_escape(_build_action_text(alert))}* \\| entry `{exposure.get('entry_price', 'N/A')}` \\| size `${exposure.get('dominant_usdc', 0):,.0f}`",
            ])
    if bucket_lines:
        ok = _send_block(bucket_lines) and ok
    if thin_edge_lines:
        ok = _send_block(["", "⚠️ *Thin-Edge Follow Alerts*", "Visible for monitoring, but entry price is already close to 1.00.", *thin_edge_lines]) and ok

    paper = stats.get("paper_portfolio")
    if paper and paper.get("total_trades", 0) > 0:
        pnl = paper.get("total_pnl", 0)
        pnl_sign = "\\+" if pnl >= 0 else ""
        paper_lines = [
            "", "📈 *Paper Portfolio*",
            f"Equity: `${paper.get('current_equity', 100):.2f}` \\| P&L: `{pnl_sign}${abs(pnl):.2f}` \\({paper.get('total_pnl_pct', 0):+.1f}%\\)",
            f"Open: {paper.get('open_positions', 0)} \\| Closed: {paper.get('closed_trades', 0)} \\| Win rate: {paper.get('win_rate_pct', 0):.0f}%",
        ]
        if paper.get("ready_for_real"):
            paper_lines.append("✅ *READY for real trading*")
        ok = _send_block(paper_lines) and ok

    ok = _send_block(["", "_Not financial advice\\. Always DYOR\\._"]) and ok
    return ok


def _reason_pills(values: list[str], bg: str, fg: str) -> str:
    return "".join(
        f'<span style="display:inline-block;margin:3px 6px 3px 0;padding:3px 8px;border-radius:999px;'
        f'background:{bg};color:{fg};font-size:11px;">{_html(value)}</span>'
        for value in values
    )


def _trade_table(trades: list[dict]) -> str:
    rows = ""
    for trade in trades[:5]:
        side = trade.get("side", "BUY").upper()
        outcome = trade.get("outcome", "UNKNOWN")
        side_color = "#68d391" if side == "BUY" else "#fc8181"
        rows += f"""
        <tr>
          <td style="padding:6px 8px;border-bottom:1px solid #2d3748;">{_html(trade.get('timestamp', '')[:16].replace('T', ' '))}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #2d3748;color:{side_color};font-weight:700;">{_html(side)}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #2d3748;">{_html(outcome)}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #2d3748;text-align:right;">${trade.get('amount_usdc', 0):,.0f}</td>
          <td style="padding:6px 8px;border-bottom:1px solid #2d3748;text-align:center;">{trade.get('price', 0):.3f}</td>
        </tr>
        """
    return f"""
    <table style="width:100%;border-collapse:collapse;font-size:12px;color:#e2e8f0;margin-top:8px;">
      <thead>
        <tr style="background:#2d3748;">
          <th style="padding:6px 8px;text-align:left;">Time</th>
          <th style="padding:6px 8px;text-align:left;">Side</th>
          <th style="padding:6px 8px;text-align:left;">Outcome</th>
          <th style="padding:6px 8px;text-align:right;">USDC</th>
          <th style="padding:6px 8px;text-align:center;">Price</th>
        </tr>
      </thead>
      <tbody>{rows}</tbody>
    </table>
    """


def _format_alert(alert: dict) -> str:
    bucket = alert.get("best_bucket", "insider")
    border, accent = BUCKET_COLORS.get(bucket, ("#2d3748", "#63b3ed"))
    score = alert.get("best_score", 0)
    score_color = _score_color(score)
    exposure = alert.get("active_exposure", {})
    shared = alert.get("shared_features", {})
    history = alert.get("historical_record", {})

    wallet = alert["wallet_address"]
    market = _html(alert["market_name"])
    market_end = _html(alert.get("market_end") or "?")
    action_text = _build_action_text(alert)
    review_status = alert.get("review_status", "pending")
    entity_label = _html(alert.get("entity_label", "Unknown"))
    entity_type = _html(alert.get("entity_type", "unknown"))

    polygonscan_url = f"https://polygonscan.com/address/{wallet}"
    polymarket_url = f"https://polymarket.com/profile/{wallet}"
    arkham_url = f"https://platform.arkhamintelligence.com/explorer/address/{wallet}"

    reason_html = _reason_pills(alert.get("core_reasons", []), "#1a365d", "#bee3f8")
    caution_html = _reason_pills(alert.get("caution_flags", []), "#742a2a", "#feb2b2")
    funding_html = "".join(
        f'<div style="margin:4px 0;padding:6px 8px;background:#742a2a;border-left:3px solid #e53e3e;'
        f'border-radius:3px;font-size:11px;color:#fed7d7;">{_html(item)}</div>'
        for item in alert.get("funding_warnings", [])
    )

    overall_wr = history.get("overall_win_rate")
    longshot_wr = history.get("longshot_win_rate")
    overall_wr_text = f"{overall_wr:.0%}" if overall_wr is not None else "N/A"
    longshot_wr_text = f"{longshot_wr:.0%}" if longshot_wr is not None else "N/A"

    return f"""
    <div style="background:#1a202c;border:1px solid {border};border-radius:10px;margin-bottom:18px;overflow:hidden;">
      <div style="background:{border};padding:12px 16px;display:flex;justify-content:space-between;gap:12px;align-items:center;flex-wrap:wrap;">
        <div>
          <div style="font-size:15px;font-weight:700;color:#fff;">{market}</div>
          <div style="font-size:12px;color:#e2e8f0;">{_html(BUCKET_LABELS[bucket])} · {_html(alert.get('category', 'Other'))} · {_html(action_text)}</div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:20px;font-weight:800;color:#fff;">{score}</div>
          <div style="font-size:11px;color:#e2e8f0;">bucket score · candidate {alert.get('candidate_score', 0)}</div>
        </div>
      </div>

      <div style="padding:16px;">
        <div style="font-family:monospace;font-size:12px;color:#a0aec0;margin-bottom:10px;">
          {_html(wallet)}
          <a href="{polymarket_url}" style="color:#63b3ed;text-decoration:none;margin-left:8px;">[Polymarket]</a>
          <a href="{polygonscan_url}" style="color:#63b3ed;text-decoration:none;margin-left:6px;">[Polygonscan]</a>
          <a href="{arkham_url}" style="color:#63b3ed;text-decoration:none;margin-left:6px;">[Arkham]</a>
        </div>

        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:{score_color};">{score}</div>
            <div style="font-size:10px;color:#a0aec0;">Best Bucket Score</div>
          </div>
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">{alert.get('candidate_score', 0)}</div>
            <div style="font-size:10px;color:#a0aec0;">Candidate Score</div>
          </div>
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">${exposure.get('dominant_usdc', 0):,.0f}</div>
            <div style="font-size:10px;color:#a0aec0;">Dominant Exposure</div>
          </div>
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">{exposure.get('dominant_outcome', 'UNKNOWN')}</div>
            <div style="font-size:10px;color:#a0aec0;">Dominant Outcome</div>
          </div>
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">{overall_wr_text}</div>
            <div style="font-size:10px;color:#a0aec0;">Overall Win Rate</div>
          </div>
          <div style="background:#2d3748;border-radius:8px;padding:10px 12px;min-width:120px;">
            <div style="font-size:22px;font-weight:800;color:#e2e8f0;">{review_status}</div>
            <div style="font-size:10px;color:#a0aec0;">Review Status</div>
          </div>
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:12px;font-weight:700;color:{accent};margin-bottom:6px;">Core Reasons</div>
          {reason_html or '<div style="font-size:12px;color:#a0aec0;">No strong reason flags.</div>'}
        </div>

        <div style="margin-bottom:12px;">
          <div style="font-size:12px;font-weight:700;color:#feb2b2;margin-bottom:6px;">Caution Flags</div>
          {caution_html or '<div style="font-size:12px;color:#a0aec0;">No major caution flags.</div>'}
        </div>

        {f'<div style="margin-bottom:12px;">{funding_html}</div>' if funding_html else ''}

        <div style="background:#111827;border:1px solid #2d3748;border-radius:8px;padding:12px;margin-bottom:12px;font-size:12px;color:#e2e8f0;line-height:1.6;">
          <div><b>Action:</b> {_html(action_text)}</div>
          <div><b>Entity:</b> {entity_label} ({entity_type})</div>
          <div><b>Liquidity:</b> ${shared.get('market_liquidity', 0):,.0f} · <b>Capital impact:</b> {shared.get('capital_impact_pct', 0):.1f}% · <b>Hedge ratio:</b> {exposure.get('hedge_ratio', 0):.2f}</div>
          <div><b>Entry price:</b> {exposure.get('entry_price') if exposure.get('entry_price') is not None else 'N/A'} · <b>Ends:</b> {market_end}</div>
          <div><b>History:</b> {history.get('total_wins', 0)}W / {history.get('total_resolved', 0)} resolved · <b>Longshot WR:</b> {longshot_wr_text}</div>
        </div>

        <div style="font-size:12px;font-weight:700;color:#a0aec0;margin-bottom:4px;">Recent Trades In This Alert</div>
        {_trade_table(alert.get('recent_trades', []))}
      </div>
    </div>
    """


def _build_arb_section(arb_alerts: list[dict]) -> str:
    if not arb_alerts:
        return ""
    rows = ""
    for arb in arb_alerts[:8]:
        rows += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;">{_html(arb['market'][:70])}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;">{arb['yes_ask']:.4f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;">{arb['no_ask']:.4f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;">{arb['combined']:.4f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;color:#68d391;font-weight:700;">+{arb['net_arb_pct']}%</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:right;">${arb.get('max_profit_usdc', 0):,.0f}</td>
        </tr>
        """
    return f"""
    <div style="background:#1a202c;border:1px solid #2c7a7b;border-radius:10px;margin-bottom:22px;overflow:hidden;">
      <div style="background:#2c7a7b;padding:12px 16px;color:#fff;font-size:15px;font-weight:700;">Direct Arbitrage Opportunities</div>
      <div style="padding:14px;">
        <table style="width:100%;border-collapse:collapse;font-size:12px;color:#e2e8f0;">
          <thead>
            <tr style="background:#2d3748;">
              <th style="padding:6px 10px;text-align:left;">Market</th>
              <th style="padding:6px 10px;">YES Ask</th>
              <th style="padding:6px 10px;">NO Ask</th>
              <th style="padding:6px 10px;">Combined</th>
              <th style="padding:6px 10px;">Net Edge</th>
              <th style="padding:6px 10px;text-align:right;">Max Profit</th>
            </tr>
          </thead>
          <tbody>{rows}</tbody>
        </table>
      </div>
    </div>
    """


def _build_run_health_banner(run_health: dict | None) -> str:
    if not run_health:
        return ""

    status = run_health.get("status", "healthy")
    reason = run_health.get("reason")
    request_health = run_health.get("request_health", {})

    if status == "healthy":
        return ""

    return f"""
    <div style="background:#3b1d1d;border:1px solid #e53e3e;border-radius:10px;padding:16px;margin-bottom:22px;color:#fed7d7;">
      <div style="font-size:16px;font-weight:800;color:#feb2b2;margin-bottom:6px;">Run Unhealthy</div>
      <div style="font-size:13px;line-height:1.5;">
        This run did not produce a valid market scan. Upstream network or DNS requests failed during execution.
      </div>
        <div style="font-size:12px;line-height:1.6;margin-top:10px;color:#fbd38d;">
        <div><b>Reason:</b> {_html(reason or "Unknown error")}</div>
        <div><b>Successful calls:</b> {request_health.get('successful_calls', 0)} · <b>Failed calls:</b> {request_health.get('failed_calls', 0)} · <b>Attempt failures:</b> {request_health.get('attempt_failures', 0)}</div>
      </div>
    </div>
    """


def _build_thin_edge_section(grouped: dict[str, list[dict]]) -> str:
    sections = []
    for bucket in EMAIL_BUCKET_ORDER:
        _, thin_edge_alerts = _split_bucket_alerts(grouped.get(bucket, []))
        alerts = thin_edge_alerts[:EMAIL_BUCKET_LIMIT]
        if not alerts:
            continue
        border, accent = BUCKET_COLORS[bucket]
        sections.append(
            f"""
            <div style="margin-bottom:16px;">
              <div style="font-size:16px;font-weight:800;color:{accent};margin-bottom:4px;">{BUCKET_LABELS[bucket]} · Thin Edge</div>
              <div style="font-size:12px;color:#a0aec0;margin-bottom:12px;">These are still shown for monitoring, but the entry is already close to 1.00 and leaves less upside.</div>
              {''.join(_format_alert(alert) for alert in alerts)}
            </div>
            """
        )

    if not sections:
        return ""

    return (
        '<div style="margin-top:26px;padding-top:18px;border-top:1px solid #2d3748;">'
        '<div style="font-size:18px;font-weight:800;color:#f6ad55;margin-bottom:8px;">Thin-Edge Follow Alerts</div>'
        '<div style="font-size:12px;color:#a0aec0;margin-bottom:14px;">'
        'These alerts still passed the strategy threshold, but the wallet entered at a price that is already very close to settlement. '
        'Keep them visible for context, but treat them as lower-edge follow setups.'
        '</div>'
        + "".join(sections)
        + '</div>'
    )


def _build_paper_section(stats: dict) -> str:
    paper = stats.get("paper_portfolio")
    if not paper:
        return ""
    pnl = paper.get("total_pnl", 0)
    pnl_pct = paper.get("total_pnl_pct", 0)
    pnl_color = "#68d391" if pnl >= 0 else "#fc8181"
    ready = paper.get("ready_for_real", False)
    ready_badge = (
        '<span style="background:#2f855a;color:#68d391;font-size:10px;font-weight:700;padding:2px 8px;border-radius:999px;">READY</span>'
        if ready else
        f'<span style="background:#744210;color:#f6ad55;font-size:10px;font-weight:700;padding:2px 8px;border-radius:999px;">{_html(paper.get("ready_reason", "Collecting data"))}</span>'
    )
    bucket_rows = ""
    for bucket, bp in paper.get("bucket_performance", {}).items():
        if bp.get("trades", 0) == 0:
            continue
        bp_pnl = bp.get("pnl", 0)
        bp_color = "#68d391" if bp_pnl >= 0 else "#fc8181"
        wr = round(bp["wins"] / bp["trades"] * 100, 1) if bp["trades"] > 0 else 0
        bucket_rows += (
            f'<tr><td style="padding:4px 8px;color:#a0aec0;">{_html(BUCKET_LABELS.get(bucket, bucket))}</td>'
            f'<td style="padding:4px 8px;text-align:center;">{bp["trades"]}</td>'
            f'<td style="padding:4px 8px;text-align:center;color:#68d391;">{bp["wins"]}</td>'
            f'<td style="padding:4px 8px;text-align:center;color:#fc8181;">{bp["losses"]}</td>'
            f'<td style="padding:4px 8px;text-align:center;">{wr}%</td>'
            f'<td style="padding:4px 8px;text-align:right;color:{bp_color};">${bp_pnl:+.2f}</td></tr>'
        )

    return f"""
    <div style="background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:18px;margin-top:18px;">
      <div style="font-size:16px;font-weight:800;color:#e2e8f0;margin-bottom:6px;">Paper Trading Portfolio {ready_badge}</div>
      <div style="font-size:12px;color:#9ca3af;margin-bottom:14px;">Virtual $100 portfolio — auto-trades every watchlist alert</div>
      <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px;">
        <div style="flex:1;min-width:100px;text-align:center;">
          <div style="font-size:24px;font-weight:800;color:#e2e8f0;">${paper.get('current_equity', 100):.2f}</div>
          <div style="font-size:10px;color:#9ca3af;">Equity</div>
        </div>
        <div style="flex:1;min-width:100px;text-align:center;">
          <div style="font-size:24px;font-weight:800;color:{pnl_color};">${pnl:+.2f}</div>
          <div style="font-size:10px;color:#9ca3af;">P&amp;L ({pnl_pct:+.1f}%)</div>
        </div>
        <div style="flex:1;min-width:80px;text-align:center;">
          <div style="font-size:24px;font-weight:800;color:#e2e8f0;">{paper.get('open_positions', 0)}</div>
          <div style="font-size:10px;color:#9ca3af;">Open</div>
        </div>
        <div style="flex:1;min-width:80px;text-align:center;">
          <div style="font-size:24px;font-weight:800;color:#e2e8f0;">{paper.get('closed_trades', 0)}</div>
          <div style="font-size:10px;color:#9ca3af;">Closed</div>
        </div>
        <div style="flex:1;min-width:80px;text-align:center;">
          <div style="font-size:24px;font-weight:800;color:#e2e8f0;">{paper.get('win_rate_pct', 0):.0f}%</div>
          <div style="font-size:10px;color:#9ca3af;">Win Rate</div>
        </div>
      </div>
      {f'<table style="width:100%;border-collapse:collapse;font-size:12px;"><thead><tr style="color:#9ca3af;"><th style="padding:4px 8px;text-align:left;">Bucket</th><th style="padding:4px 8px;">Trades</th><th style="padding:4px 8px;">W</th><th style="padding:4px 8px;">L</th><th style="padding:4px 8px;">Win%</th><th style="padding:4px 8px;text-align:right;">P&amp;L</th></tr></thead><tbody>{bucket_rows}</tbody></table>' if bucket_rows else ''}
    </div>
    """


def build_html_report(
    watchlist: list[dict],
    run_date: str,
    stats: dict,
    arb_alerts: list | None = None,
    run_health: dict | None = None,
) -> str:
    arb_alerts = arb_alerts or []
    grouped = _group_watchlist(watchlist)
    sections = []
    thin_edge_section = _build_thin_edge_section(grouped)

    for bucket in EMAIL_BUCKET_ORDER:
        primary_alerts, _ = _split_bucket_alerts(grouped.get(bucket, []))
        alerts = primary_alerts[:EMAIL_BUCKET_LIMIT]
        if not alerts:
            continue
        border, accent = BUCKET_COLORS[bucket]
        sections.append(
            f"""
            <div style="margin-bottom:16px;">
              <div style="font-size:16px;font-weight:800;color:{accent};margin-bottom:4px;">{BUCKET_LABELS[bucket]}</div>
              <div style="font-size:12px;color:#a0aec0;margin-bottom:12px;">{BUCKET_DESCRIPTIONS[bucket]}</div>
              {''.join(_format_alert(alert) for alert in alerts)}
            </div>
            """
        )

    body = "".join(sections) if sections else (
        '<div style="background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:28px;text-align:center;color:#a0aec0;">'
        'No alerts cleared the bucket thresholds on this run.</div>'
    )

    health_banner = _build_run_health_banner(run_health)

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Polymarket Strategy Watchlist</title></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0;">
  <div style="max-width:920px;margin:0 auto;padding:24px 16px;">
    <div style="background:linear-gradient(135deg,#1f2937,#111827);border:1px solid #2d3748;border-radius:12px;padding:22px;margin-bottom:22px;">
      <h1 style="margin:0 0 6px;font-size:24px;color:#e2e8f0;">Polymarket Strategy Watchlist</h1>
      <div style="font-size:13px;color:#9ca3af;">{run_date}</div>
    </div>

    {health_banner}

    <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:22px;">
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#e2e8f0;">{stats.get('flagged_alerts', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Watchlist Alerts</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#e2e8f0;">{stats.get('candidate_alerts', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Stage-1 Candidates</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #2b6cb0;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#63b3ed;">{stats.get('insider_watchlist', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Insider</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #b7791f;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#f6ad55;">{stats.get('sports_watchlist', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Sports News</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #2f855a;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#68d391;">{stats.get('momentum_watchlist', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Momentum</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #c05621;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#f6ad55;">{stats.get('contrarian_watchlist', 0)}</div>
        <div style="font-size:11px;color:#9ca3af;">Contrarian</div>
      </div>
      <div style="flex:1;min-width:130px;background:#1a202c;border:1px solid #2d3748;border-radius:10px;padding:14px;text-align:center;">
        <div style="font-size:30px;font-weight:800;color:#e2e8f0;">{len(arb_alerts)}</div>
        <div style="font-size:11px;color:#9ca3af;">Arb Opportunities</div>
      </div>
    </div>

    {_build_arb_section(arb_alerts)}
    {body}
    {thin_edge_section}
    {_build_paper_section(stats)}

    <div style="margin-top:24px;padding:16px;border-top:1px solid #2d3748;font-size:11px;color:#6b7280;text-align:center;">
      Generated by Polymarket Strategy Tracker. Informational only, not financial advice.
    </div>
    </div>
</body>
</html>"""


def write_html_report(
    watchlist: list[dict],
    stats: dict,
    arb_alerts: list | None = None,
    run_health: dict | None = None,
    output_path: str = "report.html",
) -> str:
    arb_alerts = arb_alerts or []
    run_date = datetime.now(timezone.utc).strftime("%A, %B %d %Y · %H:%M UTC")
    html_body = build_html_report(
        watchlist,
        run_date,
        stats,
        arb_alerts=arb_alerts,
        run_health=run_health,
    )
    Path(output_path).write_text(html_body, encoding="utf-8")
    log.info(f"HTML report written to {output_path}")
    return output_path


def send_email(
    watchlist: list[dict],
    stats: dict,
    arb_alerts: list | None = None,
    run_health: dict | None = None,
) -> bool:
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error("Gmail credentials not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD.")
        return False

    arb_alerts = arb_alerts or []
    run_date = datetime.now(timezone.utc).strftime("%A, %B %d %Y · %H:%M UTC")
    subject = (
        f"Polymarket Strategy · {stats.get('flagged_alerts', 0)} alerts"
        f"{f' · {len(arb_alerts)} arb' if arb_alerts else ''}"
        f" · {datetime.now(timezone.utc).strftime('%b %d')}"
    )
    html_body = build_html_report(
        watchlist,
        run_date,
        stats,
        arb_alerts=arb_alerts,
        run_health=run_health,
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = GMAIL_USER
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    for attempt in range(EMAIL_RETRY_COUNT + 1):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                server.login(GMAIL_USER, GMAIL_PASSWORD)
                server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
            log.info(f"Email sent to {EMAIL_TO}")
            return True
        except Exception as exc:
            log.error(f"Failed to send email: {exc}")
            if attempt >= EMAIL_RETRY_COUNT:
                break
            log.warning(
                f"Retrying email delivery in {EMAIL_RETRY_DELAY_SECONDS}s "
                f"(attempt {attempt + 2}/{EMAIL_RETRY_COUNT + 1})"
            )
            time.sleep(EMAIL_RETRY_DELAY_SECONDS)
    return False
