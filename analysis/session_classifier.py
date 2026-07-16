"""
Session classifier (V2 — infer the session, don't predict it).

Two markets trade at once: 0DTE flow (immediate directional intent, theta-sensitive) and
next-day positioning (a move that may begin later / tomorrow). The session type tells the
engine which game is being played:

  A_EXPANSION   — strong directional leadership develops + spot expands off the open.
                  0DTE is the trade; be aggressive.
  B_POSITIONING — spot stays balanced, 0DTE decays on both sides, neither side leads.
                  The real move is likely being BUILT for later/tomorrow — be patient.
  C_TRANSITION  — started as positioning, then converted into expansion. The classic
                  "quiet, then it goes" day.
  UNDETERMINED  — still in the opening warm-up; not enough session yet.

Inferred each poll from what the tape is DOING — session range, how directional that range
is (trend vs chop), and whether one side has taken leadership — not from a forecast. Stateful
per symbol, because C requires knowing a positioning phase came first.
"""
from typing import Optional


# Session type constants
A_EXPANSION   = 'A_EXPANSION'
B_POSITIONING = 'B_POSITIONING'
C_TRANSITION  = 'C_TRANSITION'
UNDETERMINED  = 'UNDETERMINED'


def _pct(a, b):
    return (a - b) / b if b else 0.0


class SessionClassifier:
    """Per-symbol session-type state; call observe() each poll."""

    def __init__(self, *, warmup_min: int = 20, expansion_range_pct: float = 0.006,
                 directionality_min: float = 0.55, balance_range_pct: float = 0.004,
                 chop_max: float = 0.40, leadership_min: float = 0.60) -> None:
        self.warmup_min = warmup_min
        self.expansion_range_pct = expansion_range_pct   # session range that counts as 'expanded'
        self.directionality_min = directionality_min     # |net move| / range: trend vs chop
        self.balance_range_pct = balance_range_pct        # tight range = balanced
        self.chop_max = chop_max                          # low directionality = chop
        self.leadership_min = leadership_min              # a side's leadership score to count as 'leading'
        self._state: dict = {}

    def reset(self, symbol: str = None) -> None:
        if symbol is None:
            self._state.clear()
        else:
            self._state.pop(symbol, None)

    def observe(self, symbol: str, *, open_price: float, spot: float,
                session_high: float, session_low: float, minutes_elapsed: float,
                lead_strength: float, lead_side: Optional[str] = None) -> dict:
        """
        Advance one symbol's session classification.

        lead_strength : the dominant side's leadership score (0-1); lead_side its side.
        Returns {type, range_pct, net_pct, directionality, lead_strength, lead_side,
                 changed(bool)}.
        """
        st = self._state.setdefault(symbol, {'type': UNDETERMINED, 'was_positioning': False,
                                             'expanded': False})
        range_pct = _pct(session_high, session_low) if session_low else 0.0
        net_pct = _pct(spot, open_price) if open_price else 0.0
        directionality = abs(net_pct) / range_pct if range_pct > 1e-9 else 0.0
        strong_lead = lead_strength >= self.leadership_min

        expanding = (range_pct >= self.expansion_range_pct
                     and directionality >= self.directionality_min and strong_lead)
        balanced = (range_pct < self.balance_range_pct or directionality < self.chop_max) and not strong_lead

        if minutes_elapsed < self.warmup_min:
            new_type = UNDETERMINED
        elif expanding:
            st['expanded'] = True
            # transition only if a positioning phase was actually observed first
            new_type = C_TRANSITION if st['was_positioning'] else A_EXPANSION
        elif balanced:
            st['was_positioning'] = True
            new_type = B_POSITIONING
        else:
            # mixed/quiet: hold the prior verdict once past warm-up (default to positioning)
            new_type = st['type'] if st['type'] != UNDETERMINED else B_POSITIONING

        changed = new_type != st['type']
        st['type'] = new_type
        return {'type': new_type, 'range_pct': round(range_pct, 5), 'net_pct': round(net_pct, 5),
                'directionality': round(directionality, 3), 'lead_strength': round(lead_strength, 3),
                'lead_side': lead_side, 'changed': changed}
