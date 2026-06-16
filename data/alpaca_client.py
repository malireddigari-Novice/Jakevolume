"""
Alpaca brokerage client — auto-executes option orders when signals fire.

Paper vs live is controlled by ALPACA_PAPER in .env (default: true).
Execution is off by default; set ALPACA_ENABLED=true to turn it on.

.env keys required:
  ALPACA_API_KEY      — from alpaca.markets → API keys
  ALPACA_SECRET_KEY   — from alpaca.markets → API keys
  ALPACA_PAPER        — true (paper) | false (live)  [default: true]
  ALPACA_ENABLED      — true to execute trades       [default: false]
  TRADE_PCT           — fraction of buying power per trade [default: 0.05]
  MAX_OPEN_POSITIONS  — skip new orders beyond this count [default: 3]

Order logic
-----------
On each BULLISH signal  → buy the CALL at the support strike.
On each BEARISH signal  → buy the PUT  at the resistance strike.
Qty = floor(TRADE_PCT * buying_power / (price_to_enter * 100)).
Order type: limit at price_to_enter (the ask at signal time), day order.
"""
import logging
from datetime import date
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

_PAPER_URL = 'https://paper-api.alpaca.markets'
_LIVE_URL  = 'https://api.alpaca.markets'


class AlpacaClient:

    def __init__(self) -> None:
        self._base = _PAPER_URL if config.ALPACA_PAPER else _LIVE_URL
        self._headers = {
            'APCA-API-KEY-ID':     config.ALPACA_API_KEY,
            'APCA-API-SECRET-KEY': config.ALPACA_SECRET_KEY,
            'Content-Type':        'application/json',
        }

    # ── Account ───────────────────────────────────────────────────────────────

    def verify(self) -> bool:
        """Confirm credentials are valid and log current buying power."""
        try:
            r = requests.get(f"{self._base}/v2/account", headers=self._headers, timeout=10)
            r.raise_for_status()
            acct = r.json()
            mode = "PAPER" if config.ALPACA_PAPER else "LIVE"
            logger.info(
                "Alpaca %s — buying_power=$%.2f  portfolio_value=$%.2f",
                mode,
                float(acct.get('buying_power', 0)),
                float(acct.get('portfolio_value', 0)),
            )
            return True
        except Exception as exc:
            logger.error("Alpaca: verify failed: %s", exc)
            return False

    def portfolio_value(self) -> float:
        """Return current portfolio value, 0.0 on error."""
        try:
            r = requests.get(f"{self._base}/v2/account", headers=self._headers, timeout=10)
            r.raise_for_status()
            return float(r.json().get('portfolio_value', 0))
        except Exception as exc:
            logger.warning("Alpaca: could not fetch portfolio value: %s", exc)
            return 0.0

    def open_position_count(self) -> int:
        """Return number of currently open positions."""
        try:
            r = requests.get(f"{self._base}/v2/positions", headers=self._headers, timeout=10)
            r.raise_for_status()
            return len(r.json())
        except Exception as exc:
            logger.warning("Alpaca: could not fetch positions: %s", exc)
            return 0

    def position_qty(self, occ: str) -> int:
        """
        Contracts currently held for a specific OCC option symbol, 0 if none.

        Returns 0 when Alpaca holds no position for `occ` — e.g. the buy-to-open
        limit order has not filled yet. Callers use this to avoid arming exit
        management (and firing sells that reject as 'uncovered') on a position
        that was never actually acquired.
        """
        try:
            r = requests.get(
                f"{self._base}/v2/positions/{occ}", headers=self._headers, timeout=10
            )
            if r.status_code == 404:
                return 0
            r.raise_for_status()
            return int(float(r.json().get('qty', 0)))
        except Exception as exc:
            logger.warning("Alpaca: could not fetch position for %s: %s", occ, exc)
            return 0

    def position_unrealized_pl(self, occ: str) -> Optional[float]:
        """
        Unrealized P&L (dollars) for an open option position, None if no position.

        Used by EOD logic to decide profit-vs-loss: a position in profit is banked,
        a strong loser with expiry life left may be held overnight. Returns None on
        404 (no position) or any error so callers can fall back to closing.
        """
        try:
            r = requests.get(
                f"{self._base}/v2/positions/{occ}", headers=self._headers, timeout=10
            )
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return float(r.json().get('unrealized_pl', 0))
        except Exception as exc:
            logger.warning("Alpaca: could not fetch P&L for %s: %s", occ, exc)
            return None

    # ── Position sizing ───────────────────────────────────────────────────────

    def calculate_qty(self, limit_price: float) -> tuple[int, float]:
        """
        Return (contracts, dollars_to_spend) using TRADE_PCT of buying power.

        contracts = floor(TRADE_PCT * buying_power / (limit_price * 100))
        One option contract controls 100 shares, so cost = limit_price * 100.
        Returns (0, 0.0) if buying power is insufficient or unavailable.
        """
        pv = self.portfolio_value()
        if pv <= 0 or limit_price <= 0:
            return 0, 0.0
        budget = pv * config.TRADE_PCT
        qty    = int(budget / (limit_price * 100))
        spend  = qty * limit_price * 100
        logger.info(
            "Alpaca: sizing  portfolio_value=$%.2f  budget=%.1f%%=$%.2f  "
            "limit=$%.2f/contract → qty=%d  spend=$%.2f",
            pv, config.TRADE_PCT * 100, budget, limit_price, qty, spend,
        )
        return qty, spend

    # ── Order placement ───────────────────────────────────────────────────────

    def close_position_qty(self, occ: str, qty: int) -> Optional[dict]:
        """
        Sell `qty` contracts of an existing option position at market.
        Used for partial exits (half at R1, half at R2).
        """
        payload = {
            'symbol':        occ,
            'qty':           str(qty),
            'side':          'sell',
            'type':          'market',
            'time_in_force': 'day',
        }
        try:
            r = requests.post(
                f"{self._base}/v2/orders",
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            r.raise_for_status()
            order = r.json()
            logger.info(
                "Alpaca: EXIT ORDER  %s  qty=%d  id=%s",
                occ, qty, order.get('id', '?')[:8],
            )
            return order
        except Exception as exc:
            resp_body = ''
            if hasattr(exc, 'response') and exc.response is not None:
                resp_body = exc.response.text[:300]
            logger.error("Alpaca: exit order FAILED for %s: %s  %s", occ, exc, resp_body)
            return None

    def close_all_positions(self) -> int:
        """
        Market-sell every open position (EOD liquidation).
        Returns count of positions closed.
        """
        try:
            r = requests.delete(
                f"{self._base}/v2/positions",
                headers=self._headers,
                params={'cancel_orders': 'true'},
                timeout=15,
            )
            # 207 Multi-Status means one row per position
            if r.status_code in (200, 204, 207):
                count = len(r.json()) if r.status_code == 207 else 0
                logger.info("Alpaca: EOD liquidation — closed %d position(s)", count)
                return count
            r.raise_for_status()
            return 0
        except Exception as exc:
            logger.error("Alpaca: EOD liquidation failed: %s", exc)
            return 0

    def place_option_order(
        self,
        symbol:      str,
        expiry:      date,
        strike:      float,
        option_type: str,
        qty:         int,
        limit_price: float,
    ) -> Optional[dict]:
        """
        Submit a buy-to-open limit day order.

        Returns the Alpaca order dict (including 'id' and 'symbol') on success,
        None on any failure.
        """
        occ = occ_symbol(symbol, expiry, strike, option_type)
        payload = {
            'symbol':        occ,
            'qty':           str(qty),
            'side':          'buy',
            'type':          'limit',
            'time_in_force': 'day',
            'limit_price':   f"{limit_price:.2f}",
            'order_class':   'simple',
        }
        try:
            r = requests.post(
                f"{self._base}/v2/orders",
                json=payload,
                headers=self._headers,
                timeout=10,
            )
            r.raise_for_status()
            order = r.json()
            logger.info(
                "Alpaca: ORDER PLACED  %s  qty=%d  limit=$%.2f  id=%s",
                occ, qty, limit_price, order.get('id', '?')[:8],
            )
            return order
        except Exception as exc:
            resp_body = ''
            if hasattr(exc, 'response') and exc.response is not None:
                resp_body = exc.response.text[:300]
            logger.error("Alpaca: order FAILED for %s: %s  %s", occ, exc, resp_body)
            return None


# ── Module-level helper (importable by main.py) ───────────────────────────────

def occ_symbol(symbol: str, expiry: date, strike: float, option_type: str) -> str:
    """
    Build the OCC option ticker.
    Format: {SYMBOL}{YYMMDD}{C|P}{strike*1000 zero-padded to 8 digits}
    Example: AAPL 2026-05-27 Call $305 → AAPL260527C00305000
    """
    date_s   = expiry.strftime('%y%m%d')
    cp       = 'C' if option_type == 'CALL' else 'P'
    strike_i = int(round(strike * 1000))
    return f"{symbol}{date_s}{cp}{strike_i:08d}"
