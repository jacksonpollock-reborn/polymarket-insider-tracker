"""
reporter.py — Formats the watchlist into a clean HTML email and sends it via Gmail.
"""

import os
import smtplib
import logging
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

log = logging.getLogger(__name__)

GMAIL_USER     = os.environ.get("GMAIL_USER", "")
GMAIL_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
EMAIL_TO       = os.environ.get("EMAIL_TO", GMAIL_USER)


def _score_color(score: int) -> str:
    if score >= 70:
        return "#e53e3e"   # red — high risk
    if score >= 50:
        return "#dd6b20"   # orange — medium
    if score >= 40:
        return "#d69e2e"   # yellow — watch
    return "#38a169"       # green — low


def _flag_icon(val) -> str:
    if val is True:
        return "🚩"
    if val is False:
        return "✅"
    return "➖"


def _format_wallet(w: dict) -> str:
    addr    = w["wallet_address"]
    score   = w["suspicion_score"]
    color   = _score_color(score)
    flags   = w.get("score_breakdown", {})
    hist    = w.get("historical_record", {})
    pos     = w.get("active_positions", [])
    alerts  = w.get("alert_triggers", [])
    funding = w.get("funding_warnings", [])
    label   = w.get("entity_label", "Unknown")
    etype   = w.get("entity_type", "unknown")

    polygonscan_url = f"https://polygonscan.com/address/{addr}"
    polymarket_url  = f"https://polymarket.com/profile/{addr}"
    arkham_url      = f"https://platform.arkhamintelligence.com/explorer/address/{addr}"

    # Active positions table rows
    pos_rows = ""
    for p in pos:
        side    = p.get("side", "BUY").upper()
        outcome = p.get("outcome", "")
        side_color = "#38a169" if side == "BUY" else "#e53e3e"

        # Format resolution date + days remaining
        end_raw  = p.get("market_end") or "?"
        end_str  = "?"
        days_str = ""
        if end_raw and end_raw != "?":
            try:
                end_dt   = datetime.fromisoformat(str(end_raw).replace("Z", "+00:00"))
                end_str  = end_dt.strftime("%b %d %Y")
                days_left = (end_dt - datetime.now(timezone.utc)).days
                if days_left >= 0:
                    days_str = f" ({days_left}d)"
            except Exception:
                end_str = str(end_raw)[:10]

        # ── Category badge + sports warning
        category = p.get("category", "Other")
        cat_colors = {
            "Sports":   ("#744210", "#f6ad55"),   # orange bg, orange text
            "Politics": ("#1a365d", "#63b3ed"),   # blue
            "Crypto":   ("#1c4532", "#68d391"),   # green
            "Finance":  ("#322659", "#b794f4"),   # purple
            "Other":    ("#1a202c", "#a0aec0"),   # grey
        }
        cat_bg, cat_fg = cat_colors.get(category, cat_colors["Other"])
        cat_badge = (
            f'<span style="background:{cat_bg};color:{cat_fg};font-size:10px;'
            f'font-weight:700;padding:1px 5px;border-radius:3px;margin-right:4px;'
            f'vertical-align:middle;">{category}</span>'
        )
        sports_warning = ""
        if category == "Sports":
            sports_warning = (
                '<br><span style="color:#e53e3e;font-size:10px;font-weight:600;">'
                '⚠️ 體育市場 — 可能不是 Insider，請自行判斷</span>'
            )

        # ── Market name: show FULL name, never truncate the end
        # The key info (date, specific condition) is usually at the END of the question
        market_name = p["market_name"]
        # Split into two lines if long: first 60 chars + rest on second line
        if len(market_name) > 65:
            split_idx   = market_name.rfind(" ", 0, 65)
            split_idx   = split_idx if split_idx > 30 else 65
            name_line1  = market_name[:split_idx]
            name_line2  = f'<br><span style="color:#a0aec0;">{market_name[split_idx:].strip()}</span>'
        else:
            name_line1 = market_name
            name_line2 = ""

        # ── Side + outcome: always show YES/NO prominently
        # For binary markets: "BUY YES" / "BUY NO"
        # For multi-outcome: "BUY · Overpass"
        outcome_upper = outcome.upper()
        if outcome_upper in ("YES", "NO"):
            outcome_color  = "#38a169" if outcome_upper == "YES" else "#e53e3e"
            position_label = (
                f'<span style="color:{side_color};font-weight:700;">{side}</span> ' +
                f'<span style="color:{outcome_color};font-weight:700;font-size:13px;">{outcome_upper}</span>'
            )
        elif outcome and outcome_upper not in ("BUY", "SELL", ""):
            position_label = (
                f'<span style="color:{side_color};font-weight:700;">{side}</span> ' +
                f'<span style="color:#e2e8f0;">· {outcome}</span>'
            )
        else:
            position_label = f'<span style="color:{side_color};font-weight:700;">{side}</span>'

        pos_rows += f"""
        <tr>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;max-width:260px;font-size:12px;line-height:1.4;">{cat_badge}{name_line1}{name_line2}{sports_warning}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;white-space:nowrap;">{position_label}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:right;">${p['amount_usdc']:,.0f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;">{p['entry_price']:.2f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:right;">${p['market_liquidity']:,.0f}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #2d3748;text-align:center;white-space:nowrap;">{end_str}<span style="color:#e53e3e;">{days_str}</span></td>
        </tr>"""

    pos_table = f"""
    <table style="width:100%;border-collapse:collapse;font-size:12px;color:#e2e8f0;margin-top:8px;">
      <thead>
        <tr style="background:#2d3748;">
          <th style="padding:6px 10px;text-align:left;">Market</th>
          <th style="padding:6px 10px;">Side · Outcome</th>
          <th style="padding:6px 10px;text-align:right;">Size (USDC)</th>
          <th style="padding:6px 10px;">Entry</th>
          <th style="padding:6px 10px;text-align:right;">Market TVL</th>
          <th style="padding:6px 10px;">Resolves</th>
        </tr>
      </thead>
      <tbody>{pos_rows if pos_rows else '<tr><td colspan="6" style="padding:8px 10px;color:#718096;">No qualifying positions found</td></tr>'}</tbody>
    </table>""" if pos else '<p style="color:#718096;font-size:12px;">No active positions extracted.</p>'

    # Score breakdown pills
    flag_items = [
        ("New Wallet (<30d)",       flags.get("new_wallet")),
        ("Large Niche Bet",         flags.get("large_bet_niche")),
        ("Zero Hedging",            flags.get("zero_hedge")),
        ("Timing Sniper",           flags.get("immaculate_timing")),
        ("Longshot Win Rate",       flags.get("longshot_flag")),
        ("Bridge Funding",          flags.get("bridge_flag")),
        ("Mixer Funding",           flags.get("mixer_flag")),
        ("Arkham Project Link",     flags.get("arkham_project_link")),
        ("Coordinated Cluster",     flags.get("coordinated_cluster")),
        ("Dune Whale",              flags.get("dune_whale_flag")),
        ("Dune New Wallet",         flags.get("dune_new_wallet_flag")),
    ]

    flags_html = ""
    for name, val in flag_items:
        bg = "#742a2a" if val is True else "#1a202c"
        border = "#e53e3e" if val is True else "#4a5568"
        icon = _flag_icon(val)
        flags_html += f'<span style="display:inline-block;margin:3px 4px 3px 0;padding:3px 8px;border:1px solid {border};border-radius:4px;background:{bg};font-size:11px;">{icon} {name}</span>'

    # Historical stats
    ls_rate = hist.get("longshot_win_rate")
    ls_str  = f"{ls_rate:.0%}" if ls_rate is not None else "N/A"
    ov_rate = hist.get("overall_win_rate")
    ov_str  = f"{ov_rate:.0%}" if ov_rate is not None else "N/A"

    # Alert badges
    alert_html = ""
    for a in alerts:
        alert_html += f'<span style="display:inline-block;margin:2px 4px 2px 0;padding:2px 8px;background:#744210;border-radius:3px;font-size:11px;color:#fbd38d;">⚡ {a}</span>'

    # Funding warnings
    funding_html = ""
    for f in funding:
        funding_html += f'<div style="margin:3px 0;padding:4px 8px;background:#742a2a;border-left:3px solid #e53e3e;border-radius:2px;font-size:11px;">{f}</div>'

    return f"""
    <div style="background:#1a202c;border:1px solid {color};border-radius:8px;margin-bottom:24px;overflow:hidden;">

      <!-- Header bar -->
      <div style="background:{color};padding:10px 16px;display:flex;justify-content:space-between;align-items:center;">
        <span style="font-weight:700;font-size:15px;color:#fff;">Score: {score}</span>
        <span style="font-size:12px;color:#fff;opacity:0.9;">{label} · {etype}</span>
      </div>

      <div style="padding:16px;">

        <!-- Address + links -->
        <div style="margin-bottom:12px;font-family:monospace;font-size:12px;color:#a0aec0;">
          <span style="color:#e2e8f0;">{addr}</span>
          &nbsp;
          <a href="{polymarket_url}" style="color:#63b3ed;text-decoration:none;">[Polymarket]</a>
          <a href="{polygonscan_url}" style="color:#63b3ed;text-decoration:none;margin-left:6px;">[Polygonscan]</a>
          <a href="{arkham_url}" style="color:#63b3ed;text-decoration:none;margin-left:6px;">[Arkham]</a>
        </div>

        <!-- Stats row -->
        <div style="display:flex;gap:16px;margin-bottom:14px;flex-wrap:wrap;">
          <div style="background:#2d3748;padding:8px 14px;border-radius:6px;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#e2e8f0;">{ov_str}</div>
            <div style="font-size:10px;color:#718096;">Overall Win Rate</div>
            <div style="font-size:10px;color:#718096;">{hist.get('total_wins',0)}W / {hist.get('total_resolved',0)} resolved</div>
          </div>
          <div style="background:#2d3748;padding:8px 14px;border-radius:6px;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#e2e8f0;">{ls_str}</div>
            <div style="font-size:10px;color:#718096;">Longshot Win Rate</div>
            <div style="font-size:10px;color:#718096;">{hist.get('longshot_wins',0)}W / {hist.get('longshot_total',0)} &lt;20% bets</div>
          </div>
          <div style="background:#2d3748;padding:8px 14px;border-radius:6px;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#e2e8f0;">{w.get('polygon_tx_count',0)}</div>
            <div style="font-size:10px;color:#718096;">Polygon Txns</div>
          </div>
          <div style="background:#2d3748;padding:8px 14px;border-radius:6px;text-align:center;">
            <div style="font-size:18px;font-weight:700;color:#e2e8f0;">{flags.get('wallet_age_days', '?')}</div>
            <div style="font-size:10px;color:#718096;">Days on Polygon</div>
          </div>
        </div>

        <!-- Alert triggers -->
        {f'<div style="margin-bottom:10px;">{alert_html}</div>' if alerts else ''}

        <!-- Funding warnings -->
        {f'<div style="margin-bottom:12px;">{funding_html}</div>' if funding else ''}

        <!-- Score flags -->
        <div style="margin-bottom:14px;">{flags_html}</div>

        <!-- Active positions -->
        <div style="font-size:12px;font-weight:600;color:#a0aec0;margin-bottom:4px;">ACTIVE POSITIONS IN FLAGGED MARKETS</div>
        {pos_table}

      </div>
    </div>"""


def build_html_report(watchlist: list[dict], run_date: str, stats: dict) -> str:
    if not watchlist:
        body = '<p style="color:#a0aec0;text-align:center;padding:40px;">No wallets exceeded the suspicion threshold today.</p>'
    else:
        body = "".join(_format_wallet(w) for w in sorted(watchlist, key=lambda x: -x["suspicion_score"]))

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Polymarket Insider Watchlist</title></head>
<body style="margin:0;padding:0;background:#0f1117;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;color:#e2e8f0;">

  <div style="max-width:800px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="background:linear-gradient(135deg,#1a365d,#2d3748);border-radius:10px;padding:24px;margin-bottom:24px;border:1px solid #2d3748;">
      <h1 style="margin:0 0 6px;font-size:22px;color:#63b3ed;">🔍 Polymarket Insider Watchlist</h1>
      <p style="margin:0;color:#a0aec0;font-size:13px;">Daily scan · {run_date}</p>
    </div>

    <!-- Summary stats -->
    <div style="display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap;">
      <div style="flex:1;min-width:120px;background:#1a202c;border:1px solid #2d3748;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#e53e3e;">{stats.get('flagged_wallets',0)}</div>
        <div style="font-size:11px;color:#718096;">Flagged Wallets</div>
      </div>
      <div style="flex:1;min-width:120px;background:#1a202c;border:1px solid #2d3748;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#dd6b20;">{stats.get('markets_scanned',0)}</div>
        <div style="font-size:11px;color:#718096;">Markets Scanned</div>
      </div>
      <div style="flex:1;min-width:120px;background:#1a202c;border:1px solid #2d3748;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#d69e2e;">{stats.get('large_trades',0)}</div>
        <div style="font-size:11px;color:#718096;">Large Trades Found</div>
      </div>
      <div style="flex:1;min-width:120px;background:#1a202c;border:1px solid #2d3748;border-radius:8px;padding:14px;text-align:center;">
        <div style="font-size:28px;font-weight:700;color:#38a169;">{stats.get('data_sources_active',0)}/4</div>
        <div style="font-size:11px;color:#718096;">Data Sources Active</div>
      </div>
    </div>

    <!-- Score legend -->
    <div style="background:#1a202c;border:1px solid #2d3748;border-radius:8px;padding:12px 16px;margin-bottom:24px;font-size:12px;">
      <span style="color:#718096;margin-right:12px;">Score legend:</span>
      <span style="color:#e53e3e;margin-right:12px;">■ 70+ High Alert</span>
      <span style="color:#dd6b20;margin-right:12px;">■ 50–69 Medium</span>
      <span style="color:#d69e2e;margin-right:12px;">■ 40–49 Watch</span>
    </div>

    <!-- Watchlist -->
    {body}

    <!-- Footer -->
    <div style="margin-top:24px;padding:16px;border-top:1px solid #2d3748;font-size:11px;color:#4a5568;text-align:center;">
      Generated by Polymarket Insider Tracker · For informational purposes only.<br>
      Not financial advice. Always do your own research.
    </div>

  </div>
</body>
</html>"""


def send_email(watchlist: list[dict], stats: dict) -> bool:
    if not GMAIL_USER or not GMAIL_PASSWORD:
        log.error("Gmail credentials not configured. Set GMAIL_USER and GMAIL_APP_PASSWORD.")
        return False

    run_date = datetime.now(timezone.utc).strftime("%A, %B %d %Y · %H:%M UTC")
    n        = stats.get("flagged_wallets", 0)
    subject  = f"🔍 Polymarket Watchlist — {n} wallet{'s' if n!=1 else ''} flagged · {datetime.now(timezone.utc).strftime('%b %d')}"

    html_body = build_html_report(watchlist, run_date, stats)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = GMAIL_USER
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_PASSWORD)
            server.sendmail(GMAIL_USER, EMAIL_TO, msg.as_string())
        log.info(f"Email sent to {EMAIL_TO}")
        return True
    except Exception as e:
        log.error(f"Failed to send email: {e}")
        return False
