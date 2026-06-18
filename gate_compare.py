"""
Step 3 — historical shadow comparison: OLD V1 gate vs NEW two-path production gate.

The NEW gate verdict comes from the canonical SignalDetector._eval_volume (via
backtest_volume_gate.score_all). The OLD gate is a FROZEN, comparison-only replica of
the pre-2026-06-17 3-rule gate — it never fires a live alert; it exists solely to
measure what the new gate adds and removes. It reuses the shared metric helpers
(_cluster_metrics / _excitation) so only the decision boolean is duplicated, and it is
frozen so it cannot drift.

Usage:  python gate_compare.py
"""
import statistics
import config
from analysis.signal_detector import _cluster_metrics, _excitation
from backtest_volume_gate import score_all

_STRONG_MFE = 50.0   # "strong move" threshold for missed-move accounting


def legacy_v1_gate(symbol, history, delta, low_dist) -> bool:
    """FROZEN V1 3-rule gate (SingleBar OR Cluster OR StairStep). Comparison only."""
    volatile  = symbol in config.VOLATILE_SYMBOLS
    vol_floor = 250 if volatile else 100
    win_floor = 600 if volatile else 300
    prior20  = history[-21:-1] if len(history) > 1 else []
    prior10  = history[-11:-1] if len(history) > 1 else []
    median20 = statistics.median(prior20) if prior20 else 0.0
    max20    = max(prior20) if prior20 else 0.0
    avg10    = (sum(prior10) / len(prior10)) if prior10 else 0.0
    baseline = max(avg10, median20, 10.0)
    vol_ratio = delta / baseline
    last5 = history[-5:]; win5 = sum(last5)
    prior_windows = [sum(history[i:i + 5]) for i in range(0, max(0, len(history) - 5))][-20:]
    med_win = statistics.median(prior_windows) if prior_windows else 0.0
    max_win = max(prior_windows) if prior_windows else 0.0
    win_ratio5  = win5 / max(med_win, 50.0)
    cluster_dom = win5 / max(max_win, 1.0)
    active5 = sum(1 for v in last5 if v >= max(median20 * 2.0, 50.0))
    clu = _cluster_metrics(history); excite = _excitation(clu['window'], clu['base_unit'])
    near175 = (low_dist is None or low_dist <= 1.75)
    near200 = (low_dist is None or low_dist <= 2.00)
    single  = delta >= vol_floor and vol_ratio >= 8.0 and delta >= 0.75 * max20 and near175
    cluster = win5 >= win_floor and win_ratio5 >= 3.0 and active5 >= 3 and cluster_dom >= 0.75 and near175
    stair   = win5 >= win_floor and active5 >= 3 and excite >= 0.70 and win_ratio5 >= 2.5 and near200
    return bool(single or cluster or stair)


def _stats(rows):
    if not rows:
        return dict(n=0, win=0.0, avg=0.0, mfe=0.0, mae=0.0)
    n = len(rows)
    return dict(n=n,
                win=sum(1 for r in rows if r['pnl'] > 0) / n * 100,
                avg=sum(r['pnl'] for r in rows) / n,
                mfe=sum(r['mfe'] for r in rows) / n,
                mae=sum(r['mae'] for r in rows) / n)


def main():
    rows = score_all()
    R = rows[:-1]
    for r in R:
        r['old'] = legacy_v1_gate(r['sym'], r['vols'], r['delta'], r['cld'])
        r['new'] = bool(r['valid'])

    both    = [r for r in R if r['old'] and r['new']]
    removed = [r for r in R if r['old'] and not r['new']]      # new blocks what old admitted
    added   = [r for r in R if not r['old'] and r['new']]      # new admits what old blocked
    added_b = [r for r in added if r['path'] == 'B']           # via the contextual path
    old_adm = [r for r in R if r['old']]
    new_adm = [r for r in R if r['new']]

    def line(name, rows):
        s = _stats(rows)
        print(f"  {name:34} n={s['n']:4}  win%={s['win']:5.1f}  avgP&L%={s['avg']:+7.2f}  "
              f"MFE={s['mfe']:+6.1f}  MAE={s['mae']:+6.1f}")

    print(f"\nGATE COMPARISON — OLD V1 vs NEW two-path   (priceable signals: {len(R)})")
    print("=" * 92)
    line("Admitted by BOTH", both)
    line("Admitted by OLD only (removed by new)", removed)
    line("Admitted by NEW only (added)", added)
    line("  └─ added via Path B (contextual)", added_b)
    print("-" * 92)
    line("OLD gate admits (total)", old_adm)
    line("NEW gate admits (total)", new_adm)

    win_removed  = [r for r in removed if r['pnl'] > 0]
    los_removed  = [r for r in removed if r['pnl'] <= 0]
    missed_moves = [r for r in removed if r['mfe'] >= _STRONG_MFE]
    o, nw = len(old_adm), len(new_adm)
    print("\nWHAT THE NEW GATE CHANGES")
    print("-" * 92)
    print(f"  Alert-count: OLD {o} → NEW {nw}   "
          f"({(o - nw)} fewer, {((o - nw) / o * 100) if o else 0:.0f}% reduction)")
    print(f"  Removed by new: {len(removed)}   "
          f"→ winners removed {len(win_removed)} (avg {_stats(win_removed)['avg']:+.1f}%), "
          f"losers removed {len(los_removed)} (avg {_stats(los_removed)['avg']:+.1f}%)")
    print(f"  Missed strong moves (MFE ≥ {_STRONG_MFE:.0f}% but now blocked): {len(missed_moves)}")
    for r in missed_moves:
        print(f"       {r['sym']:5} {r['styp']:7} MFE={r['mfe']:+.0f}% pnl={r['pnl']:+.0f}% "
              f"peak1m={r['peak1m']} block={r['block']}")
    print(f"  Added by new (old missed): {len(added)}  (Path B {len(added_b)})")
    for r in added:
        print(f"       {r['sym']:5} {r['styp']:7} path={r['path']} pnl={r['pnl']:+.0f}% "
              f"MFE={r['mfe']:+.0f}% peak1m={r['peak1m']}")
    print(f"\n  MFE/MAE shift (admitted set): OLD MFE {_stats(old_adm)['mfe']:+.1f} / "
          f"MAE {_stats(old_adm)['mae']:+.1f}  →  NEW MFE {_stats(new_adm)['mfe']:+.1f} / "
          f"MAE {_stats(new_adm)['mae']:+.1f}")
    print()


if __name__ == "__main__":
    main()
