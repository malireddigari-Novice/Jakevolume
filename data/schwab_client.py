"""
Charles Schwab (formerly TD Ameritrade) option quote client.

Provides real-time bid/ask prices for option contracts at S/R strikes.
Used to populate price_to_enter and price_to_exit in fired signals.

Setup (one-time)
----------------
1. Register a developer app at developer.schwab.com.
2. Set the callback URL to  https://127.0.0.1
3. Copy the Client ID -> SCHWAB_API_KEY  in .env
   Copy the Secret   -> SCHWAB_APP_SECRET in .env
4. Run  python -c "from data.schwab_client import SchwabClient; SchwabClient().login()"
   A browser opens for OAuth consent.  After approval the token is saved to
   SCHWAB_TOKEN_FILE (default: schwab_token.json) and refreshed automatically
   on every subsequent run.

Rate limits
-----------
Schwab allows ~120 API calls/minute per app.
At 9 symbols × 1 call/minute we use <10 % of the budget.
_get_option_chain() retries on HTTP 429 with exponential back-off.
"""
import logging
import random
import time
from datetime import date, datetime, timedelta
from typing import Optional

import pytz
import schwab
import schwab.client

import config
from data.market_utils import CST

logger = logging.getLogger(__name__)


class SchwabClient:

    def __init__(self) -> None:
        """Initialise the client; call login() before any data methods."""
        self._client: Optional[schwab.client.Client] = None
        # Expiry cache: {symbol: expiry_date}; cleared at start of each trading day
        self._expiry_cache:      dict[str, date] = {}
        self._expiry_cache_date: Optional[date]  = None

    # ── Authentication ────────────────────────────────────────────────────────

    def login(self) -> None:
        """
        Authenticate with the Schwab API.

        Loads an existing token from SCHWAB_TOKEN_FILE when available and
        refreshes it automatically.  On the very first run (no token file)
        opens a browser for OAuth consent and saves the token.

        Raises RuntimeError if required credentials are missing from .env.
        """
        if not config.SCHWAB_API_KEY or not config.SCHWAB_APP_SECRET:
            raise RuntimeError(
                "SCHWAB_API_KEY and SCHWAB_APP_SECRET must be set in .env.\n"
                "Register a developer app at developer.schwab.com to obtain them."
            )

        try:
            self._client = schwab.auth.client_from_token_file(
                config.SCHWAB_TOKEN_FILE,
                config.SCHWAB_API_KEY,
                config.SCHWAB_APP_SECRET,
            )
            logger.info("Schwab: token loaded from %s", config.SCHWAB_TOKEN_FILE)

        except Exception as exc:
            # Covers FileNotFoundError (first run) AND expired/revoked refresh token
            logger.info(
                "Schwab: token load failed (%s) — starting manual OAuth flow.",
                exc,
            )
            try:
                self._client = schwab.auth.client_from_manual_flow(
                    api_key=config.SCHWAB_API_KEY,
                    app_secret=config.SCHWAB_APP_SECRET,
                    callback_url=config.SCHWAB_CALLBACK_URL,
                    token_path=config.SCHWAB_TOKEN_FILE,
                )
                logger.info("Schwab: token saved to %s", config.SCHWAB_TOKEN_FILE)
            except Exception as flow_exc:
                logger.error("Schwab: OAuth flow failed: %s", flow_exc, exc_info=True)
                raise

    # ── Public API ────────────────────────────────────────────────────────────

    def get_option_quotes_for_levels(
        self,
        symbol: str,
        expiry: date,
        levels: list,
    ) -> dict:
        """
        Fetch real-time bid/ask/mark for each S/R level strike from Schwab.

        Parameters
        ----------
        symbol  : underlying equity symbol e.g. 'AAPL'
        expiry  : the expiry date to query (usually today's 0DTE or next expiry)
        levels  : rows from db.get_today_levels() — each must have 'strike' and
                  'option_type' ('CALL' | 'PUT')

        Returns
        -------
        {(strike, option_type): {'bid': float, 'ask': float, 'mark': float,
                                  'volume': int}}
        Empty dict on any error so callers can proceed without option prices.
        """
        if not self._client:
            logger.debug("Schwab: client not initialised, skipping quote fetch")
            return {}

        raw = self._get_option_chain(symbol, expiry)
        if raw is None:
            return {}

        # Parse the full chain into a flat lookup table
        all_quotes = _parse_chain(raw)

        # Return BOTH the level's own option type AND the confirming type for every
        # S/R strike so the signal detector can check the correct side:
        #   RESISTANCE strike → confirm with PUT volume (rejection)
        #   SUPPORT    strike → confirm with CALL volume (bounce)
        result: dict = {}
        for level in levels:
            strike = float(level['strike'])
            for opt_type in ('CALL', 'PUT'):
                key = (strike, opt_type)
                if key in all_quotes:
                    result[key] = all_quotes[key]
                else:
                    logger.debug(
                        "Schwab: no quote for %s %s@%.2f (expiry %s)",
                        symbol, opt_type, strike, expiry,
                    )
        return result

    def get_watched_contracts(
        self,
        symbol: str,
        expiry: date,
        spot: float,
        n: int = 2,
    ) -> dict:
        """
        Return the n nearest call and put strikes to spot, each tagged with
        whether it is the primary (highest OI) or secondary watched contract.

        At support  → watch CALLS: 2 nearest strikes to spot, highest OI = primary
        At resistance → watch PUTS: 2 nearest strikes to spot, highest OI = primary

        Both strikes are returned so the signal detector can run ATM + ITM
        cluster checks.  The 'primary' flag marks the highest-OI strike.

        Returns {(strike, opt_type): {bid, ask, mark, volume, open_interest, primary}}
        Empty dict on any error.
        """
        if not self._client:
            return {}
        raw = self._get_option_chain(symbol, expiry)
        if raw is None:
            return {}

        all_quotes = _parse_chain(raw, include_oi=True)

        result: dict = {}
        for opt_type in ('CALL', 'PUT'):
            # All available strikes for this side
            strikes = sorted(
                {s for (s, ot) in all_quotes if ot == opt_type},
                key=lambda s: abs(s - spot),
            )
            nearest_n = strikes[:n]
            if not nearest_n:
                continue

            # Primary = highest OI among the n nearest
            primary_strike = max(
                nearest_n,
                key=lambda s: all_quotes.get((s, opt_type), {}).get('open_interest', 0),
            )

            for strike in nearest_n:
                data = dict(all_quotes.get((strike, opt_type), {}))
                data['primary'] = (strike == primary_strike)
                result[(strike, opt_type)] = data

        logger.debug(
            "%s: watched contracts near %.2f → calls=%s puts=%s",
            symbol, spot,
            [(s, d.get('open_interest'), d.get('primary'))
             for (s, ot), d in result.items() if ot == 'CALL'],
            [(s, d.get('open_interest'), d.get('primary'))
             for (s, ot), d in result.items() if ot == 'PUT'],
        )
        return result

    def get_option_chain_full(
        self,
        symbol: str,
        expiry: date,
    ) -> dict:
        """
        Return the complete parsed option chain for a symbol/expiry.

        Result keyed by (strike, option_type) -> {bid, ask, mark, volume, open_interest}.
        Useful for OI-level computation as an alternative to Databento Historical.
        """
        if not self._client:
            return {}
        raw = self._get_option_chain(symbol, expiry)
        return _parse_chain(raw, include_oi=True) if raw else {}

    # ── Internal helpers ──────────────────────────────────────────────────────

    def get_option_chain_normalized(self, symbol: str) -> Optional[dict]:
        """
        Return the nearest-expiry option chain in DatabentoClient format.

        Queries Schwab for the next 7 days of expiries and picks the nearest one
        that has both calls and puts.  Returns a dict with keys:
          expiry : date
          calls  : list of contract dicts
          puts   : list of contract dicts
          all    : combined list with 'option_type' key added
        Each contract has: strike, expiry, open_interest, volume, bid, ask, mark.
        Returns None on any error so the caller can fall back to Databento.
        """
        if not self._client:
            return None
        raw = self._get_option_chain(symbol)
        if raw is None:
            return None
        result = _normalize_chain(raw)
        if result:
            result['symbol'] = symbol
            logger.info(
                "Schwab: %s chain expiry=%s calls=%d puts=%d",
                symbol, result['expiry'], len(result['calls']), len(result['puts']),
            )
        return result

    def get_bars(self, symbol: str, count: int = None) -> list[dict]:
        """
        Return the latest `count` 1-minute OHLCV bars for an equity, oldest-first.

        Fetches today's regular-session price history from Schwab and returns
        the same format as DatabentoClient.get_bars() so the SignalDetector
        and volume-spike logic work without modification.
        """
        count = count or config.BARS_TO_FETCH
        if not self._client:
            return []
        try:
            # Bound the query to TODAY so Schwab cannot return a stale/previous
            # session. Without explicit start/end the DAY/ONE_DAY query can hand
            # back the last completed session, producing a wrong spot price.
            now   = datetime.now(CST)
            start = CST.localize(datetime(now.year, now.month, now.day, 0, 0))
            resp = self._client.get_price_history(
                symbol,
                period_type=schwab.client.Client.PriceHistory.PeriodType.DAY,
                frequency_type=schwab.client.Client.PriceHistory.FrequencyType.MINUTE,
                frequency=schwab.client.Client.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start,
                end_datetime=now,
                need_extended_hours_data=False,
            )
            resp.raise_for_status()
            candles = resp.json().get('candles', [])
            bars = []
            for c in candles:
                bar_time = datetime.fromtimestamp(
                    c['datetime'] / 1000, tz=pytz.UTC
                ).astimezone(CST)
                bars.append({
                    'bar_time': bar_time,
                    'open':     float(c['open']),
                    'high':     float(c['high']),
                    'low':      float(c['low']),
                    'close':    float(c['close']),
                    'volume':   int(c['volume']),
                })
            bars.sort(key=lambda b: b['bar_time'])
            logger.debug("Schwab: %s — %d bars returned (latest %s)",
                         symbol, len(bars),
                         bars[-1]['bar_time'].strftime('%Y-%m-%d %H:%M') if bars else 'none')
            return bars[-count:]
        except Exception as exc:
            logger.warning("Schwab: get_bars failed for %s: %s", symbol, exc)
            return []

    def get_option_bars(self, occ_symbol: str, count: int = None) -> list[dict]:
        """
        Return today's 1-minute OHLCV bars for an option contract, oldest-first.

        `occ_symbol` is the OCC ticker (e.g. NVDA260529C00130000). Schwab's
        price-history endpoint accepts option symbols and returns the same candle
        shape as the equity feed, so the result matches get_bars().
        Returns [] on any error so the caller can skip that contract.
        """
        count = count or config.BARS_TO_FETCH
        if not self._client:
            return []
        try:
            # Bound to today (same staleness fix as the equity get_bars).
            now   = datetime.now(CST)
            start = CST.localize(datetime(now.year, now.month, now.day, 0, 0))
            resp = self._client.get_price_history(
                occ_symbol,
                period_type=schwab.client.Client.PriceHistory.PeriodType.DAY,
                frequency_type=schwab.client.Client.PriceHistory.FrequencyType.MINUTE,
                frequency=schwab.client.Client.PriceHistory.Frequency.EVERY_MINUTE,
                start_datetime=start,
                end_datetime=now,
                need_extended_hours_data=False,
            )
            resp.raise_for_status()
            candles = resp.json().get('candles', [])
            bars = []
            for c in candles:
                bar_time = datetime.fromtimestamp(
                    c['datetime'] / 1000, tz=pytz.UTC
                ).astimezone(CST)
                bars.append({
                    'bar_time': bar_time,
                    'open':     float(c['open']),
                    'high':     float(c['high']),
                    'low':      float(c['low']),
                    'close':    float(c['close']),
                    'volume':   int(c['volume']),
                })
            bars.sort(key=lambda b: b['bar_time'])
            logger.debug("Schwab: %s — %d option bars returned", occ_symbol, len(bars))
            return bars[-count:]
        except Exception as exc:
            logger.warning("Schwab: get_option_bars failed for %s: %s", occ_symbol, exc)
            return []

    def get_nearest_expiry(self, symbol: str) -> Optional[date]:
        """
        Return the nearest available option expiry for a symbol.

        Cached per trading day — only one API call per symbol per day regardless
        of how many times the intraday loop calls this.
        """
        today = date.today()
        if self._expiry_cache_date != today:
            self._expiry_cache.clear()
            self._expiry_cache_date = today

        if symbol in self._expiry_cache:
            return self._expiry_cache[symbol]

        if not self._client:
            return None
        raw = self._get_option_chain(symbol)   # fetches next 7 days
        if not raw:
            return None
        result = _normalize_chain(raw)
        if not result:
            return None
        exp = result['expiry']
        self._expiry_cache[symbol] = exp
        logger.debug("Schwab: %s nearest expiry = %s (cached)", symbol, exp)
        return exp

    def get_prev_close(self, symbol: str) -> float:
        """Return the previous regular-session closing price from Schwab."""
        if not self._client:
            raise RuntimeError("SchwabClient not logged in")
        resp = self._client.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        return float(data[symbol]['quote']['closePrice'])

    def get_quote(self, symbol: str) -> dict:
        """Return current/pre-market price as {'price': float}, matching DatabentoClient interface."""
        if not self._client:
            raise RuntimeError("SchwabClient not logged in")
        resp = self._client.get_quote(symbol)
        resp.raise_for_status()
        data = resp.json()
        q = data[symbol]['quote']
        price = q.get('lastPrice') or q.get('closePrice')
        return {'price': float(price)}

    def get_option_chain(self, symbol: str) -> Optional[dict]:
        """Alias for get_option_chain_normalized() — DatabentoClient interface compatibility."""
        return self.get_option_chain_normalized(symbol)

    def _get_option_chain(
        self,
        symbol: str,
        expiry: Optional[date] = None,
        max_retries: int = 4,
    ) -> Optional[dict]:
        """
        Call the Schwab option chain endpoint with exponential back-off on 429.

        If expiry is given, restricts to that single date.  Otherwise fetches
        the next 7 days so the caller can pick the nearest available expiry.
        Returns the raw JSON dict on success, None on unrecoverable error.
        """
        for attempt in range(max_retries):
            try:
                if expiry:
                    from_date, to_date = expiry, expiry
                else:
                    from_date = date.today()
                    to_date   = from_date + timedelta(days=7)
                resp = self._client.get_option_chain(
                    symbol,
                    contract_type=schwab.client.Client.Options.ContractType.ALL,
                    from_date=from_date,
                    to_date=to_date,
                )
                resp.raise_for_status()
                data = resp.json()
                if data.get('status') != 'SUCCESS':
                    logger.warning(
                        "Schwab: option chain status=%s for %s",
                        data.get('status'), symbol,
                    )
                    return None
                return data

            except Exception as exc:
                msg   = str(exc).lower()
                is_rl = '429' in msg or 'rate limit' in msg or 'too many' in msg
                if is_rl and attempt < max_retries - 1:
                    wait = (2 ** attempt) + random.random()
                    logger.warning(
                        "Schwab rate limit for %s (attempt %d/%d), "
                        "retrying in %.1fs",
                        symbol, attempt + 1, max_retries, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.warning(
                        "Schwab: option chain fetch failed for %s: %s",
                        symbol, exc,
                    )
                    return None
        return None


# ── Module-level helpers ──────────────────────────────────────────────────────

def _normalize_chain(data: dict) -> Optional[dict]:
    """
    Convert a raw Schwab chain response to DatabentoClient-compatible format.

    Picks the nearest expiry that has both calls and puts.
    """
    calls_by_exp: dict[date, list] = {}
    puts_by_exp:  dict[date, list] = {}

    for opt_type, exp_map, bucket in [
        ('CALL', data.get('callExpDateMap', {}), calls_by_exp),
        ('PUT',  data.get('putExpDateMap',  {}), puts_by_exp),
    ]:
        for date_dte, strikes_map in exp_map.items():
            exp_date = date.fromisoformat(date_dte.split(':')[0])
            bucket.setdefault(exp_date, [])
            for strike_str, contracts in strikes_map.items():
                if not contracts:
                    continue
                c = contracts[0]
                bucket[exp_date].append({
                    'strike':        float(strike_str),
                    'expiry':        exp_date,
                    'open_interest': int(c.get('openInterest', 0) or 0),
                    'volume':        int(c.get('totalVolume', 0) or 0),
                    'bid':           c.get('bid'),
                    'ask':           c.get('ask'),
                    'mark':          c.get('mark'),
                })

    shared = sorted(set(calls_by_exp) & set(puts_by_exp))
    if not shared:
        return None
    nearest = shared[0]
    calls = calls_by_exp[nearest]
    puts  = puts_by_exp[nearest]
    return {
        'expiry': nearest,
        'calls':  calls,
        'puts':   puts,
        'all':    [{'option_type': 'CALL', **c} for c in calls] +
                  [{'option_type': 'PUT',  **p} for p in puts],
    }


def _parse_chain(data: dict, include_oi: bool = False) -> dict:
    """
    Flatten Schwab's callExpDateMap / putExpDateMap into a keyed lookup.

    The Schwab chain response groups contracts as:
      {opt_type}ExpDateMap -> {"YYYY-MM-DD:DTE": {"strike": [contract, ...]}}

    Returns {(strike, option_type): {bid, ask, mark, volume[, open_interest]}}.
    """
    result: dict = {}
    sides = [
        ('CALL', data.get('callExpDateMap', {})),
        ('PUT',  data.get('putExpDateMap',  {})),
    ]
    for opt_type, exp_map in sides:
        for _date_dte, strikes_map in exp_map.items():
            for strike_str, contracts in strikes_map.items():
                if not contracts:
                    continue
                c      = contracts[0]
                strike = float(strike_str)
                entry  = {
                    'bid':      c.get('bid'),
                    'ask':      c.get('ask'),
                    'mark':     c.get('mark'),
                    'volume':   int(c.get('totalVolume', 0) or 0),
                    'day_high': c.get('highPrice') or c.get('high'),
                    'day_low':  c.get('lowPrice')  or c.get('low'),
                }
                if include_oi:
                    entry['open_interest'] = int(c.get('openInterest', 0) or 0)
                result[(strike, opt_type)] = entry
    return result
