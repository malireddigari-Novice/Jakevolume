"""
Show what's still open and reconcile the two sources of truth. READ-ONLY — places
no orders, writes nothing. Run any time:  python check_open_positions.py

  • Alpaca /v2/positions      → what the broker actually holds right now
  • Alpaca /v2/orders (open)  → buy/sell orders not yet filled
  • trades table (status=placed, unfilled exits) → what the system THINKS is open

Reconciliation flags drift:
  [OK]        open in Alpaca AND tracked open in DB
  [UNTRACKED] held in Alpaca but no matching open DB trade
  [STALE?]    DB says open but Alpaca holds nothing (exit filled, or buy never acquired)
"""
import config
import db.ops as db
from data.alpaca_client import AlpacaClient


def main() -> int:
    db.init_pool()
    ac = AlpacaClient()
    if not ac.verify():
        print("Alpaca verify failed — check credentials; aborting.")
        return 1

    positions = ac.list_positions()
    orders    = ac.list_open_orders()
    db_open   = db.get_open_trades()
    mode      = "PAPER" if config.ALPACA_PAPER else "LIVE"

    print(f"\n=== Alpaca {mode} — open positions ({len(positions)}) ===")
    for p in positions:
        print(f"  {p['occ']:<22} qty={p['qty']:>3} {p['side']:<5} "
              f"entry=${p['avg_entry_price']:.2f} now=${p['current_price']:.2f} "
              f"uPL=${p['unrealized_pl']:+.2f} ({p['unrealized_plpc'] * 100:+.1f}%)")
    if not positions:
        print("  (none)")

    print(f"\n=== Alpaca open (unfilled) orders ({len(orders)}) ===")
    for o in orders:
        lp = f"@${o['limit_price']:.2f}" if o['limit_price'] else ""
        print(f"  {o['occ']:<22} {o['side']:<4} qty={o['qty']} filled={o['filled_qty']} "
              f"{o['type']} {lp} [{o['status']}]")
    if not orders:
        print("  (none)")

    print(f"\n=== DB trades table — system thinks OPEN ({len(db_open)}) ===")
    for t in db_open:
        print(f"  {t['occ_symbol']:<22} {t['signal_type']:<8} qty={t['qty']} "
              f"exit1_filled={t['exit1_filled']} exit2_filled={t['exit2_filled']}")
    if not db_open:
        print("  (none)")

    # ── Reconcile by OCC symbol ──
    pos_occ = {p['occ'] for p in positions}
    ord_occ = {o['occ'] for o in orders}
    db_occ  = {t['occ_symbol'] for t in db_open}

    print("\n=== Reconciliation ===")
    matched        = pos_occ & db_occ
    held_untracked = pos_occ - db_occ
    db_no_pos      = db_occ - pos_occ
    for occ in sorted(matched):
        print(f"  [OK]        {occ}: open in Alpaca AND tracked open in DB")
    for occ in sorted(held_untracked):
        print(f"  [UNTRACKED] {occ}: held in Alpaca but NOT an open DB trade")
    for occ in sorted(db_no_pos):
        pend = " (has a pending order)" if occ in ord_occ else ""
        print(f"  [STALE?]    {occ}: DB open but NO Alpaca position{pend} "
              f"— exit likely filled, or buy never acquired")
    if not (matched or held_untracked or db_no_pos):
        print("  Nothing open on either side — flat and reconciled.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
