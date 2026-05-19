"""Quick Databento connectivity check — Historical + Live."""
from dotenv import load_dotenv
load_dotenv()

import os
import databento as db
from data.webull_client import WebullClient

key = os.environ.get('DATABENTO_API_KEY', '')
print(f"API key present: {bool(key)}  (ends ...{key[-4:]})")

# ── 1. Historical metadata ────────────────────────────────────────────────────
print("\n[1] Historical API metadata...")
hist = db.Historical(key)
itch  = hist.metadata.get_dataset_range(dataset="XNAS.ITCH")
opra  = hist.metadata.get_dataset_range(dataset="OPRA.PILLAR")
print(f"    XNAS.ITCH   available: {itch['start'][:10]}  ->  {itch['end'][:10]}")
print(f"    OPRA.PILLAR available: {opra['start'][:10]}  ->  {opra['end'][:10]}")

# ── 2. AAPL prev_close (actual data pull) ─────────────────────────────────────
print("\n[2] AAPL prev_close (Historical ohlcv-1d)...")
wb = WebullClient()
close = wb.get_prev_close("AAPL")
print(f"    AAPL prev_close = ${close:.2f}  OK")

# ── 3. Live feed connectivity ─────────────────────────────────────────────────
print("\n[3] Live feed connectivity...")

try:
    live = db.Live(key=key)
    live.subscribe(
        dataset="XNAS.ITCH", schema="ohlcv-1m",
        symbols=["AAPL"], stype_in="raw_symbol",
    )
    print("    XNAS.ITCH   Live session: CONNECTED")
    live.stop()
except Exception as e:
    print(f"    XNAS.ITCH   Live session: FAILED — {e}")

try:
    live2 = db.Live(key=key)
    live2.subscribe(
        dataset="OPRA.PILLAR", schema="definition",
        symbols=["AAPL.OPT"], stype_in="parent",
    )
    print("    OPRA.PILLAR Live session: CONNECTED")
    live2.stop()
except Exception as e:
    print(f"    OPRA.PILLAR Live session: FAILED — {e}")

print("\nDone.")
