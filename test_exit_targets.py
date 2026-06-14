"""Unit checks for compute_exit_targets — full-ladder, skip-only-if-too-close."""
from analysis.signal_detector import compute_exit_targets

# S3=380 S2=385 S1=390 | R1=400 R2=405 R3=410
LV = [{'level_type': 'SUPPORT', 'strike': 380}, {'level_type': 'SUPPORT', 'strike': 385},
      {'level_type': 'SUPPORT', 'strike': 390}, {'level_type': 'RESISTANCE', 'strike': 400},
      {'level_type': 'RESISTANCE', 'strike': 405}, {'level_type': 'RESISTANCE', 'strike': 410}]

def ck(name, got, exp):
    assert got == exp, f"{name}: got {got}, expected {exp}"
    print(f"PASS  {name}: targets {got}")

# CALL entered at S3, S2 NOT too close -> use S2 then S1 (the new all-levels behavior).
ck("call@S3 (S2 has room)", compute_exit_targets('BULLISH', 380.0, LV), (385.0, 390.0))
# CALL entered ~S3, S2 within 0.25% -> skip S2 -> S1 then R1.
ck("call@S3 (S2 too close)", compute_exit_targets('BULLISH', 384.5, LV), (390.0, 400.0))
# CALL entered ~S2, S1 too close -> R1 then R2.
ck("call@S2 (S1 too close)", compute_exit_targets('BULLISH', 389.5, LV), (400.0, 405.0))
# CALL entered ~S1, R1 too close -> R2 then R3.
ck("call@S1 (R1 too close)", compute_exit_targets('BULLISH', 399.5, LV), (405.0, 410.0))

# PUT entered at R3 -> R2 then R1 (mirror, has room).
ck("put@R3 (R2 has room)", compute_exit_targets('BEARISH', 410.0, LV), (405.0, 400.0))
# PUT entered ~R3, R2 within 0.25% -> skip R2 -> R1 then S1.
ck("put@R3 (R2 too close)", compute_exit_targets('BEARISH', 405.3, LV), (400.0, 390.0))
# PUT entered ~R1, S1 too close -> S2 then S3.
ck("put@R1 (S1 too close)", compute_exit_targets('BEARISH', 390.5, LV), (385.0, 380.0))

print("\nAll exit-target ladder checks passed.")
