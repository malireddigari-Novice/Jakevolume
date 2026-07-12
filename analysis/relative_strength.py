"""
Relative strength vs a benchmark (QQQ by default).

MAG7 usually moves with QQQ. This module surfaces the exceptions — names moving
relatively strong or weak INDEPENDENT of the index — using a raw relative return:

    RS = stock %change - benchmark %change   (percentage points)

Positive RS = outperforming the benchmark; negative = lagging. |RS| at/above a
threshold flags a divergence worth attention. Pure functions, no I/O.
"""
from typing import Optional


def pct_change(price: Optional[float], prev_close: Optional[float]) -> Optional[float]:
    """Percent change from prev_close to price, e.g. +1.234 (%). None if unknown."""
    if price is None or not prev_close or float(prev_close) <= 0:
        return None
    return round((float(price) - float(prev_close)) / float(prev_close) * 100.0, 3)


def relative_strength(stock_pct: Optional[float], bench_pct: Optional[float]) -> Optional[float]:
    """Raw relative return: stock %change minus benchmark %change (pct points)."""
    if stock_pct is None or bench_pct is None:
        return None
    return round(float(stock_pct) - float(bench_pct), 3)


def classify_rs(rs: Optional[float], threshold: float) -> str:
    """RELATIVELY_STRONG / RELATIVELY_WEAK / IN_LINE / UNKNOWN from RS vs threshold."""
    if rs is None:
        return 'UNKNOWN'
    if rs >= threshold:
        return 'RELATIVELY_STRONG'
    if rs <= -threshold:
        return 'RELATIVELY_WEAK'
    return 'IN_LINE'


_TAG = {'RELATIVELY_STRONG': 'STRONG', 'RELATIVELY_WEAK': 'WEAK',
        'IN_LINE': 'in-line', 'UNKNOWN': 'n/a'}


def rs_tag(rs: Optional[float], threshold: float) -> str:
    """Short display label for an RS value."""
    return _TAG[classify_rs(rs, threshold)]


def compute_row(symbol: str, price, prev_close, bench_pct, threshold: float) -> dict:
    """One symbol's relative-strength row (all fields None-safe)."""
    spct = pct_change(price, prev_close)
    rs = relative_strength(spct, bench_pct)
    return {'symbol': symbol, 'pct': spct, 'rs': rs,
            'rs_class': classify_rs(rs, threshold), 'rs_tag': rs_tag(rs, threshold)}


def divergences(rows: list, threshold: float) -> list:
    """Rows with |RS| >= threshold, strongest divergence first (by |RS|)."""
    out = [r for r in rows if r.get('rs') is not None and abs(r['rs']) >= threshold]
    return sorted(out, key=lambda r: abs(r['rs']), reverse=True)
