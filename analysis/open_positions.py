"""
Open-position check for the morning briefing.

Reconciles what Alpaca actually holds (broker truth) against the trades table
(system state) so the 8:20 AM briefing can surface anything carried overnight —
normally nothing, since the system liquidates at EOD, so a non-empty result flags
a legit next-day-expiry hold or an orphan from a missed/crashed session.
"""
import logging

import db.ops as db
from data.alpaca_client import parse_occ_symbol

logger = logging.getLogger(__name__)


def collect_open_positions(alpaca, today) -> dict:
    """
    Return {'flat': bool, 'positions': [...]} for the briefing.

    Each position dict: symbol, occ, option_type, strike, expiry, qty,
    current_price, unrealized_pl, flag — where flag is:
        EXPIRED    expiry < today (already settled; should not still show)
        TRACKED    matches an open row in the trades table
        UNTRACKED  held by Alpaca but the system has no open trade for it
    Never raises — returns flat on any error so the briefing still sends.
    """
    try:
        positions = alpaca.list_positions()
    except Exception:
        logger.warning("open-position check: list_positions failed", exc_info=True)
        return {'flat': True, 'positions': []}

    db_open = {t['occ_symbol'] for t in db.get_open_trades()}
    out = []
    for p in positions:
        occ = p['occ']
        try:
            sym, expiry, otype, strike = parse_occ_symbol(occ)
        except Exception:
            sym, expiry, otype, strike = occ, None, '?', 0.0
        if expiry and expiry < today:
            flag = 'EXPIRED'
        elif occ in db_open:
            flag = 'TRACKED'
        else:
            flag = 'UNTRACKED'
        out.append({
            'symbol':        sym,
            'occ':           occ,
            'option_type':   otype,
            'strike':        strike,
            'expiry':        expiry,
            'qty':           p['qty'],
            'current_price': p['current_price'],
            'unrealized_pl': p['unrealized_pl'],
            'flag':          flag,
        })
    return {'flat': not out, 'positions': out}
