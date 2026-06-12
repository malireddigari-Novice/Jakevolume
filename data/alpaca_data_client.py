"""
Alpaca market-data client — intraday data source for signal detection.

Mirrors the subset of the SchwabClient interface the intraday loop uses, so it can
be dropped in as the `data_src` in main.intraday_check():
    get_bars, get_quote, get_nearest_expiry, get_watched_contracts,
    get_option_bars, get_option_history_range, get_option_history_low, verify

Why Alpaca for intraday: our key carries the SIP (full-market) stock feed and the
OPRA options feed, and — unlike Schwab — Alpaca serves option price-history
(1-min and daily), so option_level_bars and the §13 historical-value gate work.

NOT used for the morning OI-level snapshot: Alpaca does not expose live open
interest, so that stays on Schwab (see main.morning_snapshot).

Volume semantics: get_watched_contracts returns CUMULATIVE day volume (dailyBar.v)
under the 'volume' key, exactly like SchwabClient, so the detector's per-minute
delta logic is unchanged.
"""
import logging
from datetime import date, datetime, timedelta
from typing import Optional

import pytz
import requests

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)

_DATA = 'https://data.alpaca.markets'
_TRADE = ('https://paper-api.alpaca.markets' if config.ALPACA_PAPER
          else 'https://api.alpaca.markets')
_STOCK_FEED = 'sip'        # full-market entitlement confirmed
_OPT_FEED   = 'indicative'


class AlpacaDataClient:

    def __init__(self) -> None:
        self._h = {'APCA-API-KEY-ID': config.ALPACA_API_KEY,
                   'APCA-API-SECRET-KEY': config.ALPACA_SECRET_KEY}
        self._expiry_cache: dict[str, date] = {}
        self._expiry_cache_date: Optional[date] = None

    # ── helpers ────────────────────────────────────────────────────────────────

    def _get(self, base: str, path: str, params: dict) -> Optional[dict]:
        try:
            r = requests.get(f"{base}{path}", headers=self._h, params=params, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            logger.warning("Alpaca GET %s failed: %s", path, exc)
            return None

    @staticmethod
    def _cst(iso_z: str) -> datetime:
        dt = datetime.fromisoformat(iso_z.replace('Z', '+00:00')).astimezone(pytz.UTC)
        return dt.astimezone(CST)

    @staticmethod
    def _today_start_utc() -> str:
        now = datetime.now(CST)
        start = CST.localize(datetime(now.year, now.month, now.day, 0, 0))
        return start.astimezone(pytz.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')

    # ── auth/health ─────────────────────────────────────────────────────────────

    def login(self) -> None:
        """No persistent session needed (REST + header auth)."""
        return None

    def verify(self) -> bool:
        j = self._get(_DATA, "/v2/stocks/AAPL/trades/latest",
                      {'feed': _STOCK_FEED})
        ok = bool(j and j.get('trade'))
        if ok:
            logger.info("Alpaca data ready — SIP stock feed + OPRA options")
        else:
            logger.error("Alpaca data verify failed")
        return ok

    # ── equity bars / quote ──────────────────────────────────────────────────────

    def get_bars(self, symbol: str, count: int = None) -> list[dict]:
        count = count or config.BARS_TO_FETCH
        j = self._get(_DATA, f"/v2/stocks/{symbol}/bars",
                      {'timeframe': '1Min', 'start': self._today_start_utc(),
                       'feed': _STOCK_FEED, 'limit': 10000, 'sort': 'asc'})
        raw = (j or {}).get('bars') or []
        bars = [{
            'bar_time': self._cst(b['t']),
            'open': float(b['o']), 'high': float(b['h']), 'low': float(b['l']),
            'close': float(b['c']), 'volume': int(b['v']),
        } for b in raw]
        bars.sort(key=lambda x: x['bar_time'])
        return bars[-count:]

    def get_quote(self, symbol: str) -> dict:
        """Latest trade price (fallback to quote mid), matching DatabentoClient/Schwab shape."""
        j = self._get(_DATA, f"/v2/stocks/{symbol}/trades/latest", {'feed': _STOCK_FEED})
        if j and j.get('trade') and j['trade'].get('p'):
            return {'price': float(j['trade']['p'])}
        q = self._get(_DATA, f"/v2/stocks/{symbol}/quotes/latest", {'feed': _STOCK_FEED})
        if q and q.get('quote'):
            bp, ap = q['quote'].get('bp'), q['quote'].get('ap')
            if bp and ap:
                return {'price': (float(bp) + float(ap)) / 2}
        return {'price': None}

    # ── expiry discovery ──────────────────────────────────────────────────────────

    def get_nearest_expiry(self, symbol: str) -> Optional[date]:
        today = date.today()
        if self._expiry_cache_date != today:
            self._expiry_cache.clear(); self._expiry_cache_date = today
        if symbol in self._expiry_cache:
            return self._expiry_cache[symbol]
        j = self._get(_TRADE, "/v2/options/contracts",
                      {'underlying_symbols': symbol, 'expiration_date_gte': today.isoformat(),
                       'type': 'call', 'limit': 1000})
        exps = sorted({c['expiration_date'] for c in (j or {}).get('option_contracts', [])})
        if not exps:
            return None
        exp = date.fromisoformat(exps[0])
        self._expiry_cache[symbol] = exp
        logger.debug("Alpaca: %s nearest expiry = %s", symbol, exp)
        return exp

    # ── watched option contracts (for volume detection + bid/ask) ──────────────────

    def get_watched_contracts(self, symbol: str, expiry: date, spot: float,
                              n: int = 2) -> dict:
        """
        n nearest strikes to spot per side, with bid/ask/mark, CUMULATIVE day volume,
        day high/low, and a `primary` flag (highest day volume of the n nearest —
        a liquidity proxy, since Alpaca exposes no live OI). open_interest is 0.
        """
        lo, hi = spot * 0.90, spot * 1.10
        snaps: dict = {}
        page = None
        while True:
            params = {'feed': _OPT_FEED, 'limit': 1000,
                      'expiration_date': expiry.isoformat(),
                      'strike_price_gte': round(lo, 2), 'strike_price_lte': round(hi, 2)}
            if page:
                params['page_token'] = page
            j = self._get(_DATA, f"/v1beta1/options/snapshots/{symbol}", params)
            if not j:
                break
            snaps.update(j.get('snapshots') or {})
            page = j.get('next_page_token')
            if not page:
                break

        parsed: dict = {}
        for occ, s in snaps.items():
            p = _parse_occ(occ)
            if not p:
                continue
            _sym, strike, ot = p
            q  = s.get('latestQuote') or {}
            db = s.get('dailyBar') or {}
            tr = s.get('latestTrade') or {}
            bid, ask = q.get('bp'), q.get('ap')
            mark = ((float(bid) + float(ask)) / 2) if (bid and ask) else (
                float(tr['p']) if tr.get('p') else None)
            parsed[(strike, ot)] = {
                'bid': float(bid) if bid else None,
                'ask': float(ask) if ask else None,
                'mark': mark,
                'volume': int(db.get('v', 0) or 0),          # cumulative day volume
                'open_interest': 0,                          # not available from Alpaca
                'day_high': float(db['h']) if db.get('h') else None,
                'day_low':  float(db['l']) if db.get('l') else None,
            }

        result: dict = {}
        for ot in ('CALL', 'PUT'):
            strikes = sorted({s for (s, o) in parsed if o == ot},
                             key=lambda s: abs(s - spot))[:n]
            if not strikes:
                continue
            primary = max(strikes, key=lambda s: parsed[(s, ot)].get('volume', 0))
            for s in strikes:
                d = dict(parsed[(s, ot)]); d['primary'] = (s == primary)
                result[(s, ot)] = d
        return result

    # ── option price history (1-min + daily) — Alpaca serves this; Schwab does not ──

    def _option_bars(self, occ: str, timeframe: str, start_iso: str) -> list[dict]:
        out: list[dict] = []
        page = None
        while True:
            params = {'symbols': occ, 'timeframe': timeframe, 'start': start_iso, 'limit': 10000}
            if page:
                params['page_token'] = page
            j = self._get(_DATA, "/v1beta1/options/bars", params)
            if not j:
                break
            for b in (j.get('bars') or {}).get(occ, []):
                out.append({
                    'bar_time': self._cst(b['t']),
                    'open': float(b['o']), 'high': float(b['h']), 'low': float(b['l']),
                    'close': float(b['c']), 'volume': int(b['v']),
                })
            page = j.get('next_page_token')
            if not page:
                break
        out.sort(key=lambda x: x['bar_time'])
        return out

    def get_option_bars(self, occ_symbol: str, count: int = None) -> list[dict]:
        count = count or config.BARS_TO_FETCH
        bars = self._option_bars(occ_symbol, '1Min', self._today_start_utc())
        return bars[-count:]

    def get_option_history_range(self, occ_symbol: str,
                                 lookback_days: int = None) -> Optional[tuple[float, float]]:
        lookback_days = lookback_days or config.OPT_HIST_LOOKBACK_DAYS
        start = (datetime.now(CST) - timedelta(days=lookback_days)).astimezone(
            pytz.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')
        bars = self._option_bars(occ_symbol, '1Day', start)
        lows  = [b['low']  for b in bars if b['low']  > 0]
        highs = [b['high'] for b in bars if b['high'] > 0]
        if not lows or not highs:
            return None
        return min(lows), max(highs)

    def get_option_history_low(self, occ_symbol: str,
                               lookback_days: int = None) -> Optional[float]:
        rng = self.get_option_history_range(occ_symbol, lookback_days)
        return rng[0] if rng else None


def _parse_occ(occ: str) -> Optional[tuple[str, float, str]]:
    """'AAPL260612C00290000' -> ('AAPL', 290.0, 'CALL'). Underlying is the part before YYMMDD."""
    i = 0
    while i < len(occ) and not occ[i].isdigit():
        i += 1
    if i == 0 or len(occ) - i < 15:
        return None
    underlying = occ[:i]
    cp = occ[i + 6]
    strike = int(occ[i + 7:i + 15]) / 1000.0
    return underlying, strike, ('CALL' if cp == 'C' else 'PUT')
