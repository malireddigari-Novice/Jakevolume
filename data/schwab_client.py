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
from datetime import date
from typing import Optional

import schwab
import schwab.client

import config

logger = logging.getLogger(__name__)


class SchwabClient:

    def __init__(self) -> None:
        """Initialise the client; call login() before any data methods."""
        self._client: Optional[schwab.client.Client] = None

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

        except FileNotFoundError:
            logger.info(
                "Schwab: no token file found at %s — starting interactive OAuth flow.",
                config.SCHWAB_TOKEN_FILE,
            )
            logger.info(
                "Schwab: a browser window will open.  Log in with your Schwab account "
                "and approve access.  The token will be saved automatically."
            )
            self._client = schwab.auth.easy_client(
                api_key=config.SCHWAB_API_KEY,
                app_secret=config.SCHWAB_APP_SECRET,
                callback_url=config.SCHWAB_CALLBACK_URL,
                token_path=config.SCHWAB_TOKEN_FILE,
            )
            logger.info("Schwab: token saved to %s", config.SCHWAB_TOKEN_FILE)

        except Exception as exc:
            logger.error("Schwab: login failed: %s", exc, exc_info=True)
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

        # Return only the strikes that appear in the S/R levels list
        result: dict = {}
        for level in levels:
            strike      = float(level['strike'])
            option_type = str(level.get('option_type', ''))
            key = (strike, option_type)
            if key in all_quotes:
                result[key] = all_quotes[key]
            else:
                logger.debug(
                    "Schwab: no quote for %s %s@%.2f (expiry %s)",
                    symbol, option_type, strike, expiry,
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

    def _get_option_chain(
        self,
        symbol: str,
        expiry: date,
        max_retries: int = 4,
    ) -> Optional[dict]:
        """
        Call the Schwab option chain endpoint with exponential back-off on 429.

        Returns the parsed JSON dict on success, None on unrecoverable error.
        """
        for attempt in range(max_retries):
            try:
                resp = self._client.get_option_chain(
                    symbol,
                    contract_type=schwab.client.Client.Options.ContractType.ALL,
                    from_date=expiry,
                    to_date=expiry,
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
                    'bid':    c.get('bid'),
                    'ask':    c.get('ask'),
                    'mark':   c.get('mark'),
                    'volume': int(c.get('totalVolume', 0) or 0),
                }
                if include_oi:
                    entry['open_interest'] = int(c.get('openInterest', 0) or 0)
                result[(strike, opt_type)] = entry
    return result
