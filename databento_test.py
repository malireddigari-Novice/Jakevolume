"""
Databento options data probe — finds correct symbology and available fields
for building the full client (OI, bid/ask, mark, volume per strike).
"""
import os
from dotenv import load_dotenv
load_dotenv()
import databento as db
import pandas as pd
pd.set_option('display.max_columns', None)
pd.set_option('display.width', 200)

client = db.Historical(os.environ['DATABENTO_API_KEY'])

# ── 1. AAPL option definitions (all contracts, nearest expiry) ────────────────
print("[1] OPRA definitions for AAPL.OPT (nearest expiry)...")
try:
    data = client.timeseries.get_range(
        dataset="OPRA.PILLAR",
        symbols=["AAPL.OPT"],
        schema="definition",
        start="2026-05-15",
        end="2026-05-16",
        stype_in="parent",
    )
    df = data.to_df()
    print(f"  {len(df)} contract definitions returned")
    if not df.empty:
        cols = [c for c in ['raw_symbol','strike_price','instrument_class',
                            'expiration','open_interest_qty'] if c in df.columns]
        print(df[cols].drop_duplicates('raw_symbol').head(10).to_string())
except Exception as e:
    print(f"  ERROR: {e}")

# ── 2. OPRA statistics (OI per contract) ──────────────────────────────────────
print("\n[2] OPRA statistics (OI) for AAPL.OPT...")
try:
    data = client.timeseries.get_range(
        dataset="OPRA.PILLAR",
        symbols=["AAPL.OPT"],
        schema="statistics",
        start="2026-05-15",
        end="2026-05-16",
        stype_in="parent",
    )
    df = data.to_df()
    print(f"  {len(df)} statistic records")
    if not df.empty:
        print(df.head(5).to_string())
except Exception as e:
    print(f"  ERROR: {e}")

# ── 3. OPRA consolidated BBO 1-min for AAPL options (bid/ask) ─────────────────
print("\n[3] OPRA cbbo-1m (bid/ask) for AAPL.OPT...")
try:
    data = client.timeseries.get_range(
        dataset="OPRA.PILLAR",
        symbols=["AAPL.OPT"],
        schema="cbbo-1m",
        start="2026-05-15T14:30:00",
        end="2026-05-15T14:32:00",
        stype_in="parent",
    )
    df = data.to_df()
    print(f"  {len(df)} cbbo records")
    if not df.empty:
        print(df.head(5).to_string())
except Exception as e:
    print(f"  ERROR: {e}")

# ── 4. OPRA ohlcv-1m for AAPL options (volume + price) ───────────────────────
print("\n[4] OPRA ohlcv-1m for AAPL.OPT...")
try:
    data = client.timeseries.get_range(
        dataset="OPRA.PILLAR",
        symbols=["AAPL.OPT"],
        schema="ohlcv-1m",
        start="2026-05-15T14:30:00",
        end="2026-05-15T14:32:00",
        stype_in="parent",
    )
    df = data.to_df()
    print(f"  {len(df)} ohlcv records")
    if not df.empty:
        print(df.head(5).to_string())
except Exception as e:
    print(f"  ERROR: {e}")

# ── 5. XNAS prev-day close via ohlcv-1d ───────────────────────────────────────
print("\n[5] XNAS.ITCH ohlcv-1d (prev close)...")
try:
    data = client.timeseries.get_range(
        dataset="XNAS.ITCH",
        symbols=["AAPL"],
        schema="ohlcv-1d",
        start="2026-05-13",
        end="2026-05-16",
    )
    df = data.to_df()
    print(f"  {len(df)} daily bars")
    print(df[['open','high','low','close','volume','symbol']].to_string())
except Exception as e:
    print(f"  ERROR: {e}")

print("\nDone.")
