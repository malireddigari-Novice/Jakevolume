"""
Primary-level interaction classifier (P-BD).

Primary OI levels are not only reversal zones — price can ACCEPT through them
(continuation). This maps a (side, level_type, spot, level) interaction to one of:

  BOUNCE_CALL       CALL at support that holds        (existing behavior)
  REJECTION_PUT     PUT  at resistance that rejects    (existing behavior)
  BREAKOUT_CALL     CALL at resistance accepted above  (new continuation)
  BREAKDOWN_PUT     PUT  at support accepted below     (new continuation)
  FALSE_BREAKOUT / FALSE_BREAKDOWN  crossed but not accepted (no alert)
  MIXED             none of the above

Acceptance uses the greater of an absolute and a percentage buffer past the level.
Directional-intent / flow-activation (premium, leadership) is validated elsewhere
(the intent gate) — this module only classifies the structural level interaction.
"""
import config


def level_buffer(level_price: float) -> float:
    """Greater of the absolute and percentage acceptance buffer for a level."""
    return max(config.BREAKOUT_LEVEL_BUFFER_ABS,
               abs(level_price) * config.BREAKOUT_LEVEL_BUFFER_PCT)


def classify_interaction(side: str, level_type: str, spot: float, level_price: float,
                         *, bar_close: float = None) -> str:
    """
    Classify how a candidate interacts with the primary level it is near.

    side       : 'CALL' | 'PUT' (the candidate's directional side)
    level_type : 'SUPPORT' | 'RESISTANCE' (the frozen role of the level)
    spot       : current underlying
    level_price: the level's strike
    bar_close  : completed-bar close (defaults to spot) — used for acceptance

    Continuation (breakout/breakdown) requires acceptance PAST the level by the buffer.
    A cross without acceptance is FALSE_*; the classic same-side setups are BOUNCE/REJECTION.
    """
    close = bar_close if bar_close is not None else spot
    buf = level_buffer(level_price)
    above = close >= level_price + buf
    below = close <= level_price - buf

    if side == 'CALL':
        if level_type == 'SUPPORT':
            return 'BOUNCE_CALL'                       # call at support (hold decided by intent)
        # CALL at RESISTANCE — continuation only if accepted above
        if above:
            return 'BREAKOUT_CALL'
        return 'FALSE_BREAKOUT' if close > level_price else 'MIXED'
    else:  # PUT
        if level_type == 'RESISTANCE':
            return 'REJECTION_PUT'                     # put at resistance (reject decided by intent)
        # PUT at SUPPORT — continuation only if accepted below
        if below:
            return 'BREAKDOWN_PUT'
        return 'FALSE_BREAKDOWN' if close < level_price else 'MIXED'


def level_side(level_type: str, spot: float, level_price: float, *, bar_close: float = None):
    """
    Decide which side to evaluate at a primary level based on price acceptance:
      RESISTANCE accepted above  -> ('CALL','BULLISH','BREAKOUT_CALL')
      RESISTANCE not crossed     -> ('PUT','BEARISH','REJECTION_PUT')
      SUPPORT accepted below     -> ('PUT','BEARISH','BREAKDOWN_PUT')
      SUPPORT not crossed        -> ('CALL','BULLISH','BOUNCE_CALL')
      crossed-but-not-accepted   -> None  (FALSE_BREAKOUT / FALSE_BREAKDOWN: skip)
    Returns (confirm_type, signal_type, level_action) or None.
    """
    close = bar_close if bar_close is not None else spot
    buf = level_buffer(level_price)
    if level_type == 'RESISTANCE':
        if close >= level_price + buf:
            return 'CALL', 'BULLISH', 'BREAKOUT_CALL'
        if close > level_price:                      # crossed but not accepted
            return None
        return 'PUT', 'BEARISH', 'REJECTION_PUT'
    else:  # SUPPORT
        if close <= level_price - buf:
            return 'PUT', 'BEARISH', 'BREAKDOWN_PUT'
        if close < level_price:
            return None
        return 'CALL', 'BULLISH', 'BOUNCE_CALL'


# Which interactions are actionable (map to a Gold subtype) vs blocked.
ACTIONABLE = {'BOUNCE_CALL', 'REJECTION_PUT', 'BREAKOUT_CALL', 'BREAKDOWN_PUT'}
BLOCKED    = {'FALSE_BREAKOUT', 'FALSE_BREAKDOWN', 'MIXED'}


def is_actionable(interaction: str) -> bool:
    return interaction in ACTIONABLE
