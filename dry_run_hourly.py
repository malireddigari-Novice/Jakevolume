"""
One-symbol dry run for the morning 1-hour option-bar pull.

Mirrors main._collect_hourly_option_bars end-to-end (Schwab chain -> OI levels ->
OCC symbols -> Alpaca hourly bars) but is READ-ONLY: it prints results and writes
nothing to Postgres. Run: python dry_run_hourly.py [SYMBOL]
"""
import sys

import config
from analysis.oi_levels import compute_oi_levels
from data.schwab_client import SchwabClient
from data.alpaca_data_client import AlpacaDataClient
from data.alpaca_client import occ_symbol

SYMBOL = (sys.argv[1] if len(sys.argv) > 1 else 'AAPL').upper()


def main() -> int:
    schwab = SchwabClient()
    schwab.login()
    adata = AlpacaDataClient()
    if not adata.verify():
        print("Alpaca data verify FAILED")
        return 1

    spot = adata.get_quote_mid(SYMBOL) or schwab.get_quote(SYMBOL).get('price')
    chain = schwab.get_option_chain(SYMBOL)
    expiry = chain['expiry']
    levels = compute_oi_levels(chain, spot)
    print(f"\n{SYMBOL}  spot={spot}  nearest_expiry={expiry}  levels={len(levels)}  "
          f"(lookback={config.OPT_HOURLY_LOOKBACK_DAYS}d, 1Hour)\n")

    total = 0
    for lv in levels:
        strike = float(lv['strike'])
        occ = occ_symbol(SYMBOL, expiry, strike, lv['option_type'])
        bars = adata.get_option_hourly_bars(occ)
        total += len(bars)
        if bars:
            first, last, s = bars[0]['bar_time'], bars[-1]['bar_time'], bars[-1]
            print(f"  {lv['level_type'][:3]}{lv['rank']} {lv['option_type']:<4} "
                  f"{strike:<8} {occ:<22} {len(bars):>3} bars  [{first} -> {last}]")
            print(f"        last: O={s['open']} H={s['high']} L={s['low']} "
                  f"C={s['close']} V={s['volume']}")
        else:
            print(f"  {lv['level_type'][:3]}{lv['rank']} {lv['option_type']:<4} "
                  f"{strike:<8} {occ:<22}   0 bars  (no Alpaca history)")

    print(f"\nTOTAL hourly bars: {total} across {len(levels)} levels")
    return 0


if __name__ == "__main__":
    sys.exit(main())
