---
name: databento-options-trader
description: >
  Generate Python scripts that connect to the DataBento platform to fetch, analyze,
  and act on options market data. Use this skill whenever the user wants to: pull
  options chains, greeks, implied volatility surfaces, or historical options data
  from DataBento; build scanners or screeners for options strategies (covered calls,
  spreads, straddles, iron condors, etc.); stream live options quotes or trades via
  DataBento's WebSocket API; backtest options strategies using DataBento's historical
  tick or OHLCV data; or compute derived analytics (IV rank, skew, P&L, delta exposure)
  from DataBento feeds. Also trigger when the user mentions DataBento alongside any of:
  options, greeks, implied volatility, open interest, volume analysis, expiration
  chains, or strategy screening.
---

# DataBento Options Trader Skill

This skill produces ready-to-run Python scripts that connect to the **DataBento**
platform for options market data — covering live streaming, historical pulls, chain
analysis, strategy screening, and derived analytics.

---

## Environment Setup

Before generating any script, confirm or remind the user that these are required:

```bash
pip install databento pandas numpy scipy tabulate python-dotenv gspread google-auth
```

**.env file variables this skill relies on:**

```env
DATABENTO_API_KEY=your_api_key_here

# Google Sheets output (see .env in the Jakevolume project for details)
GOOGLE_SERVICE_ACCOUNT_FILE=service_account.json
GOOGLE_SPREADSHEET_ID=your_spreadsheet_id_here
```

Retrieve your DataBento key from: https://app.databento.com/portal/api-keys

All scripts should load credentials via `python-dotenv`:

```python
from dotenv import load_dotenv
import os
load_dotenv()
API_KEY        = os.getenv("DATABENTO_API_KEY")
GS_KEY_FILE    = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")   # path to service_account.json
GS_SHEET_ID    = os.getenv("GOOGLE_SPREADSHEET_ID")         # spreadsheet ID from URL
```

---

## Google Sheets Output

All scripts that produce tabular results (chain scans, OI levels, strategy screeners)
should write their output to Google Sheets using the helper below. This keeps a
persistent morning log that can be reviewed, filtered, and shared from Google Drive.

### Sheet layout convention

Each script writes to a **named tab** matching its purpose. Tab names used in this skill:

| Tab name | Written by |
|---|---|
| `Mag7_OI_Levels` | Template 7 — morning OI resistance/support scan |
| `Mag7_Chain` | Template 7 — filtered 0DTE/1DTE contract candidates |
| `IV_Surface` | Template 4 — implied volatility surface |
| `OI_Activity` | Template 6 — unusual OI/volume scan |

### Reusable helper function

Add this to any script that needs to write to Google Sheets:

```python
import gspread
from google.oauth2.service_account import Credentials
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv()

def get_gsheet_client():
    creds = Credentials.from_service_account_file(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE"),
        scopes=[
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ],
    )
    return gspread.authorize(creds)

def write_df_to_sheet(df: pd.DataFrame, tab_name: str, mode: str = "replace") -> None:
    """
    Write a DataFrame to a named tab in the configured Google Spreadsheet.

    Args:
        df        : DataFrame to write
        tab_name  : Name of the worksheet tab (created if it does not exist)
        mode      : 'replace' clears the sheet first; 'append' adds rows below existing data
    """
    gc         = get_gsheet_client()
    spreadsheet = gc.open_by_key(os.getenv("GOOGLE_SPREADSHEET_ID"))

    try:
        ws = spreadsheet.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=tab_name, rows=2000, cols=30)

    if mode == "replace":
        ws.clear()
        data = [df.columns.tolist()] + df.astype(str).values.tolist()
        ws.update(data, value_input_option="USER_ENTERED")
    elif mode == "append":
        data = df.astype(str).values.tolist()
        ws.append_rows(data, value_input_option="USER_ENTERED")

    print(f"  -> Written {len(df)} rows to tab '{tab_name}'")
```

### Usage pattern in every script

```python
# After computing results DataFrame:
write_df_to_sheet(results, tab_name="Mag7_OI_Levels", mode="replace")
```

### Timestamp column convention

Always add a `run_at` column before writing so each row is traceable:

```python
from datetime import datetime
results["run_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
write_df_to_sheet(results, "Mag7_OI_Levels")
```

---

---

## DataBento Core Concepts

| Concept | Details |
|---|---|
| **Client** | `databento.Historical()` for historical; `databento.Live()` for streaming |
| **Dataset** | e.g., `OPRA.PILLAR` (US Options), `XNAS.ITCH`, `GLBX.MDP3` (CME futures/options) |
| **Schema** | `trades`, `mbp-1`, `mbp-10`, `ohlcv-1s`, `ohlcv-1m`, `ohlcv-1d`, `definition` |
| **Symbols** | OSI format for equity options: `SPY 240621C00530000` |
| **SType** | `raw_symbol`, `continuous`, `parent` |
| **Date range** | ISO 8601: `"2024-01-02"` or `datetime` objects |

---

## Jakevolume Production Architecture

Jakevolume uses **all Databento** — Historical for the pre-market snapshot,
Live for intraday polling. yfinance is not used.

### Data flow

| Source | Dataset | Schema | Purpose |
|---|---|---|---|
| Historical | `XNAS.ITCH` | `ohlcv-1d` | Previous day close only (T+1; no live substitute) |
| **Live** | `XNAS.ITCH` | `ohlcv-1m` | 1-min equity bars for volume-spike detection |
| **Live** | `OPRA.PILLAR` | `definition` | Contract catalogue: strike, expiry, call/put |
| **Live** | `OPRA.PILLAR` | `statistics` | Real-time OI per contract (stat_type = 9) |
| **Live** | `OPRA.PILLAR` | `ohlcv-1m` | Intraday option close/volume at S/R strikes |

All option chain data and OI come from the Live feed. Historical is used only for `prev_close`.

### Expiry selection — 0DTE or next

`get_nearest_expiry()` fetches yesterday's OPRA definitions, sorts all expiry dates,
and picks the first date **>= today**. That gives today's contracts (0DTE) when they
exist, or the very next expiry otherwise. No hard-coded weekday list needed.

```python
for exp in sorted_available_expiries:
    if exp >= today:
        return exp   # 0DTE if exp == today, else "next"
```

### Live feed — background thread pattern

Each Databento dataset requires its own `db.Live` session. Jakevolume opens two.
The OPRA.PILLAR session carries **three schemas** on one connection:

```python
live_equity = db.Live(key=api_key)
live_equity.subscribe(dataset="XNAS.ITCH", schema="ohlcv-1m",
                      symbols=SYMBOLS, stype_in="raw_symbol")

live_options = db.Live(key=api_key)
opt_syms = [f"{s}.OPT" for s in SYMBOLS]
# Multiple subscribe() calls on the same session = multiple schemas
live_options.subscribe(dataset="OPRA.PILLAR", schema="definition",
                       symbols=opt_syms, stype_in="parent")
live_options.subscribe(dataset="OPRA.PILLAR", schema="statistics",
                       symbols=opt_syms, stype_in="parent")
live_options.subscribe(dataset="OPRA.PILLAR", schema="ohlcv-1m",
                       symbols=opt_syms, stype_in="parent")
```

The options consumer distinguishes record types by their attribute signatures:

```python
def _is_definition(r): return hasattr(r, 'strike_price') and hasattr(r, 'expiration')
def _is_statistic(r):  return hasattr(r, 'stat_type') and hasattr(r, 'quantity')
def _is_ohlcv(r):      return hasattr(r, 'open') and hasattr(r, 'close') \
                               and not hasattr(r, 'stat_type') \
                               and not hasattr(r, 'strike_price')
```

Each session is consumed in a daemon thread. Symbol resolution uses
`client.symbology_map[instrument_id].raw_symbol` (populated from definition
records sent at session start). Option symbols are parsed with `_parse_osi()`:

```python
def _parse_osi(raw: str):
    # 'AAPL  260518C00300000' -> ('AAPL', 300.0, 'CALL')
    s = raw.strip()
    if len(s) < 21: return None
    underlying = s[:6].strip()
    opt_type   = 'CALL' if s[12] == 'C' else 'PUT'
    strike     = int(s[13:21]) / 1000.0
    return (underlying, strike, opt_type)
```

Bars are stored in per-symbol `deque(maxlen=60)` ring buffers, protected by a
single `threading.Lock`. The 1-minute polling loop reads from the buffer without
blocking the live consumer threads.

### Price fixed-point conversion

```python
_UNDEF_PRICE = (1 << 63) - 1   # INT64_MAX — Databento sentinel for "no price"

def _fp(price_int: int) -> Optional[float]:
    return None if price_int >= _UNDEF_PRICE else price_int * 1e-9
```

`to_df()` handles this automatically for Historical DataFrames.
For Live record attributes (`record.open`, `record.close`, etc.) apply `_fp()` manually.

### License requirement

The `db.Live` client requires a **live-data license** on the API key.
If the key only has historical access, `db.Live()` raises `BentoError`
with a clear "live data license required" message.

---

## Script Templates

### 1. Pull a Full Options Chain (Snapshot)

Use when: user wants all strikes/expirations for an underlying at a point in time.

```python
import databento as db
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

UNDERLYING = "SPY"
DATE       = "2024-06-14"

# Fetch option definitions (chain metadata)
data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="definition",
    start=DATE,
    end=DATE,
    symbols=UNDERLYING,
    stype_in="parent",      # parent maps underlying → all option contracts
)

df = data.to_df()

# Filter to a specific expiry (optional)
TARGET_EXPIRY = "2024-06-21"
chain = df[df["expiration"].str.startswith(TARGET_EXPIRY)]

# Separate calls and puts
calls = chain[chain["instrument_class"] == "C"].sort_values("strike_price")
puts  = chain[chain["instrument_class"] == "P"].sort_values("strike_price")

print(f"\n=== {UNDERLYING} Options Chain — Expiry {TARGET_EXPIRY} ===")
print(f"Calls ({len(calls)} strikes):")
print(calls[["raw_symbol","strike_price","expiration"]].to_string(index=False))
print(f"\nPuts ({len(puts)} strikes):")
print(puts[["raw_symbol","strike_price","expiration"]].to_string(index=False))
```

---

### 2. Historical OHLCV for a Specific Option Contract

Use when: user wants price history for a specific contract (backtesting, charting).

```python
import databento as db
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

# OSI symbol format: UNDERLYING + YYMMDD + C/P + 8-digit strike (padded)
SYMBOL     = "SPY   240621C00530000"   # SPY $530 Call expiring 2024-06-21
START_DATE = "2024-05-01"
END_DATE   = "2024-06-21"

data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="ohlcv-1d",          # daily bars; use ohlcv-1m for minute bars
    start=START_DATE,
    end=END_DATE,
    symbols=[SYMBOL],
    stype_in="raw_symbol",
)

df = data.to_df()
df.index = pd.to_datetime(df.index)

print(df[["open","high","low","close","volume"]].to_string())
```

---

### 3. Live Options Quote Stream (WebSocket)

Use when: user wants real-time bid/ask updates for one or more option contracts.

```python
import databento as db
from dotenv import load_dotenv
import os

load_dotenv()

SYMBOLS = [
    "SPY   240621C00530000",
    "SPY   240621P00520000",
]

def on_record(record):
    """Callback fires on every incoming market data record."""
    print(
        f"{record.ts_recv} | {record.instrument_id} | "
        f"Bid: {record.bid_px_00 / 1e9:.4f}  Ask: {record.ask_px_00 / 1e9:.4f}  "
        f"BidSz: {record.bid_sz_00}  AskSz: {record.ask_sz_00}"
    )

client = db.Live(os.getenv("DATABENTO_API_KEY"))

client.subscribe(
    dataset="OPRA.PILLAR",
    schema="mbp-1",           # top-of-book quotes
    stype_in="raw_symbol",
    symbols=SYMBOLS,
)

print("Streaming live quotes — press Ctrl+C to stop.\n")
for record in client:
    on_record(record)
```

---

### 4. Implied Volatility Surface Builder

Use when: user wants to compute and visualize the IV surface across strikes and expiries.

```python
import databento as db
import pandas as pd
import numpy as np
from scipy.stats import norm
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

UNDERLYING = "SPY"
DATE       = "2024-06-14"
SPOT_PRICE = 530.0       # Enter current underlying price
RISK_FREE  = 0.053       # Risk-free rate (annualized)

# ── Black-Scholes IV solver ────────────────────────────────────────────────
def bs_price(S, K, T, r, sigma, flag="c"):
    d1 = (np.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * np.sqrt(T))
    d2 = d1 - sigma * np.sqrt(T)
    if flag == "c":
        return S * norm.cdf(d1) - K * np.exp(-r * T) * norm.cdf(d2)
    return K * np.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def implied_vol(market_price, S, K, T, r, flag, tol=1e-6, max_iter=200):
    lo, hi = 1e-6, 10.0
    for _ in range(max_iter):
        mid = (lo + hi) / 2
        price = bs_price(S, K, T, r, mid, flag)
        if abs(price - market_price) < tol:
            return mid
        if price > market_price:
            hi = mid
        else:
            lo = mid
    return np.nan

# ── Fetch chain definitions ────────────────────────────────────────────────
data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="definition",
    start=DATE,
    end=DATE,
    symbols=UNDERLYING,
    stype_in="parent",
)

df = data.to_df()

# Attach last trade price (requires trades schema — simplified here)
# In production: join with trades/mbp schema on instrument_id
# For illustration, we'll show the structure:

records = []
for _, row in df.iterrows():
    K = row["strike_price"] / 1e9    # DataBento stores prices in fixed-point
    expiry = pd.to_datetime(row["expiration"])
    T = (expiry - pd.Timestamp(DATE)).days / 365
    flag = "c" if row["instrument_class"] == "C" else "p"

    mid_price = (row.get("bid_price", np.nan) + row.get("ask_price", np.nan)) / 2 / 1e9
    if T > 0 and mid_price > 0:
        iv = implied_vol(mid_price, SPOT_PRICE, K, T, RISK_FREE, flag)
        records.append({
            "expiry": expiry.date(),
            "strike": K,
            "type": flag.upper(),
            "mid": mid_price,
            "iv": round(iv * 100, 2) if not np.isnan(iv) else None,
            "dte": int(T * 365),
        })

surface = pd.DataFrame(records).dropna(subset=["iv"])
surface = surface.pivot_table(index="strike", columns="expiry", values="iv")
print("\n=== Implied Volatility Surface (%) ===")
print(surface.to_string())
```

---

### 5. Strategy Scanner — Covered Calls / Cash-Secured Puts

Use when: user wants to screen for income-generating options setups meeting yield/delta targets.

```python
import databento as db
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

UNDERLYING     = "SPY"
DATE           = "2024-06-14"
SPOT_PRICE     = 530.0
MIN_PREMIUM    = 1.00          # Minimum credit per contract (in $)
MAX_DELTA      = 0.35          # Max delta for short options
MIN_DTE        = 7             # Minimum days to expiration
MAX_DTE        = 45            # Maximum days to expiration

data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="definition",
    start=DATE,
    end=DATE,
    symbols=UNDERLYING,
    stype_in="parent",
)

df = data.to_df()

candidates = []
for _, row in df.iterrows():
    expiry = pd.to_datetime(row["expiration"])
    dte    = (expiry - pd.Timestamp(DATE)).days

    if not (MIN_DTE <= dte <= MAX_DTE):
        continue

    strike    = row["strike_price"] / 1e9
    flag      = row["instrument_class"]          # "C" or "P"
    bid_price = row.get("bid_price", 0) / 1e9   # Use bid for short premium

    if bid_price < MIN_PREMIUM:
        continue

    # Approximate delta via moneyness (replace with actual greeks if available)
    moneyness = strike / SPOT_PRICE
    if flag == "C" and moneyness < 1.0:        # ITM calls — skip for covered call screen
        continue
    if flag == "P" and moneyness > 1.0:        # ITM puts — skip for CSP screen
        continue

    annualized_yield = (bid_price / SPOT_PRICE) * (365 / dte) * 100

    candidates.append({
        "symbol":    row["raw_symbol"],
        "type":      flag,
        "strike":    strike,
        "expiry":    expiry.date(),
        "dte":       dte,
        "bid":       round(bid_price, 2),
        "ann_yield": round(annualized_yield, 2),
    })

results = pd.DataFrame(candidates).sort_values("ann_yield", ascending=False)
print(f"\n=== Options Income Scanner — {UNDERLYING} as of {DATE} ===")
print(results.to_string(index=False))
```

---

### 6. Open Interest & Volume Analysis

Use when: user wants to identify unusual options activity or key strike clusters.

```python
import databento as db
import pandas as pd
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

UNDERLYING = "SPY"
DATE       = "2024-06-14"

data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="ohlcv-1d",
    start=DATE,
    end=DATE,
    symbols=UNDERLYING,
    stype_in="parent",
)

df = data.to_df()

# Flag unusual volume (volume > 2× average for that strike)
df["vol_oi_ratio"] = df["volume"] / df["open_interest"].replace(0, pd.NA)
df["unusual"]      = df["volume"] > df["volume"].mean() * 2

top = df[df["unusual"]].sort_values("volume", ascending=False).head(20)

print(f"\n=== Unusual Options Activity — {UNDERLYING} on {DATE} ===")
print(top[["raw_symbol","volume","open_interest","vol_oi_ratio","close"]].to_string(index=False))
```

---

### 7. Mag7 Morning Resistance Scanner — 0DTE / 1DTE Chain Filter

Use when: user wants to compute 2–3 key resistance levels at market open for the
Magnificent 7 stocks and immediately filter the 0DTE or 1DTE options chain to
contracts positioned near those levels — for directional plays, fades, or spreads.

**Magnificent 7 tickers:** `AAPL`, `MSFT`, `GOOGL`, `AMZN`, `NVDA`, `META`, `TSLA`

#### How OI-based resistance and support levels are calculated

Open Interest is the primary signal. Strikes with the highest **call OI** act as
**resistance** — dealer gamma hedging creates selling pressure as price approaches.
Strikes with the highest **put OI** act as **support** — dealer hedging creates
buying pressure at those strikes. The script also computes **Max Pain** — the
strike where total option dollar value expires worthless — which acts as an
additional gravitational level, especially for 0DTE into the close.

| Side | Method | Signal | Notes |
|---|---|---|---|
| **Resistance** | Top-3 Call OI strikes | Highest call OI above spot | Dealers short calls → sell into rallies |
| **Resistance** | Max Pain (if above spot) | Minimum total option loss | Price drifts toward max pain into expiry |
| **Support** | Top-3 Put OI strikes | Highest put OI below spot | Dealers long puts → buy dips |
| **Support** | Max Pain (if below spot) | Minimum total option loss | Price drifts toward max pain into expiry |

**Why OI beats pivot math for 0DTE/1DTE:**
Pivot points are price-derived and static. OI walls are positioning-derived — they
reflect where the most contracts are concentrated, creating real hedging flows that
market makers must act on. For short-dated options, these are the levels that matter.

#### Morning workflow this script supports

```
Pre-market (8:30–9:29 AM CT)
  1. Fetch today’s 0DTE + 1DTE options chain (definition schema) for all Mag7
  2. Compute Max Pain from the chain (minimize total option dollar value)
  3. Rank call strikes by OI descending -> top 3 above spot = resistance
  4. Rank put strikes by OI descending  -> top 3 below spot = support
  5. Insert Max Pain as resistance or support depending on side vs spot
  6. Filter chain to contracts at exactly those key OI-wall strikes
  7. Print ranked candidates with OI, bid/ask, level type, and moneyness
  8. Write two tabs to Google Sheets:
       Mag7_OI_Levels  -- one row per ticker/level (label, price, OI, spot)
       Mag7_Chain      -- full filtered contract list (bid/ask/mid/dte/OI)

At open (9:30 AM CT)
  9. Optionally stream live quotes on those filtered strikes
```

```python
import databento as db
import pandas as pd
import numpy as np
from datetime import date
from dotenv import load_dotenv
import os

load_dotenv()
client = db.Historical(os.getenv("DATABENTO_API_KEY"))

# -- Configuration -----------------------------------------------------------
MAG7 = ["AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA"]

TODAY         = date.today().isoformat()
DTE_FILTER    = [0, 1]
TOP_N_RESIST  = 3
TOP_N_SUPPORT = 3

# Current spot prices -- update daily or wire up a live quote fetch.
SPOT_PRICES = {
    "AAPL": 189.00, "MSFT": 415.00, "GOOGL": 175.00, "AMZN": 185.00,
    "NVDA": 875.00, "META": 490.00, "TSLA":  175.00,
}

# -- Step 1: Fetch 0DTE + 1DTE chain for all Mag7 ----------------------------
print("Fetching 0DTE / 1DTE options chains for Mag7...\n")
chain_data = client.timeseries.get_range(
    dataset="OPRA.PILLAR",
    schema="definition",
    start=TODAY,
    end=TODAY,
    symbols=MAG7,
    stype_in="parent",
)
chain_df = chain_data.to_df()
chain_df["expiration_dt"] = pd.to_datetime(chain_df["expiration"])
chain_df["dte"]    = (chain_df["expiration_dt"] - pd.Timestamp(TODAY)).dt.days
chain_df           = chain_df[chain_df["dte"].isin(DTE_FILTER)].copy()
chain_df["strike"] = chain_df["strike_price"] / 1e9
chain_df["bid"]    = chain_df["bid_price"] / 1e9 if "bid_price" in chain_df else np.nan
chain_df["ask"]    = chain_df["ask_price"] / 1e9 if "ask_price" in chain_df else np.nan
chain_df["mid"]    = (chain_df["bid"] + chain_df["ask"]) / 2
chain_df["oi"]     = chain_df.get("open_interest", 0)

# -- Step 2: Max Pain calculator ---------------------------------------------
def compute_max_pain(tc):
    strikes = tc["strike"].unique()
    calls   = tc[tc["instrument_class"] == "C"]
    puts    = tc[tc["instrument_class"] == "P"]
    min_pain, mp = float("inf"), strikes[0]
    for K in strikes:
        cp = ((K - calls["strike"]).clip(lower=0) * calls["oi"]).sum()
        pp = ((puts["strike"] - K).clip(lower=0) * puts["oi"]).sum()
        if cp + pp < min_pain:
            min_pain, mp = cp + pp, K
    return round(mp, 2)

# -- Step 3: OI-based resistance and support ---------------------------------
def compute_oi_levels(tc, spot):
    """
    Resistance = top-N call-OI strikes ABOVE spot  (+ Max Pain if above spot)
    Support    = top-N put-OI strikes BELOW spot    (+ Max Pain if below spot)
    Sorted highest-OI first within each side.
    """
    calls = tc[tc["instrument_class"] == "C"]
    puts  = tc[tc["instrument_class"] == "P"]

    call_oi = (
        calls[calls["strike"] > spot]
        .groupby("strike")["oi"].sum()
        .sort_values(ascending=False)
    )
    put_oi = (
        puts[puts["strike"] < spot]
        .groupby("strike")["oi"].sum()
        .sort_values(ascending=False)
    )

    resistance = [
        {"level": round(s, 2),
         "label": f"Call OI Wall (OI={int(oi):,})",
         "side": "resistance", "oi": int(oi)}
        for s, oi in call_oi.head(TOP_N_RESIST).items()
    ]
    support = [
        {"level": round(s, 2),
         "label": f"Put OI Wall  (OI={int(oi):,})",
         "side": "support", "oi": int(oi)}
        for s, oi in put_oi.head(TOP_N_SUPPORT).items()
    ]

    try:
        mp    = compute_max_pain(tc)
        entry = {"level": mp, "label": "Max Pain", "oi": 0}
        if mp >= spot:
            entry["side"] = "resistance"
            resistance.append(entry)
            resistance.sort(key=lambda x: x["oi"], reverse=True)
            resistance = resistance[:TOP_N_RESIST]
        else:
            entry["side"] = "support"
            support.append(entry)
            support.sort(key=lambda x: x["oi"], reverse=True)
            support = support[:TOP_N_SUPPORT]
    except Exception:
        pass

    return resistance, support

level_map = {}
for ticker in MAG7:
    spot = SPOT_PRICES.get(ticker, 0)
    tc   = chain_df[chain_df["raw_symbol"].str.startswith(ticker)]
    if tc.empty or spot == 0:
        print(f"{ticker}: skipped"); continue
    try:
        resist, support = compute_oi_levels(tc, spot)
        level_map[ticker] = {"resistance": resist, "support": support}
        r_str = "  |  ".join(f'{lv["label"]}: ${lv["level"]}' for lv in resist)
        s_str = "  |  ".join(f'{lv["label"]}: ${lv["level"]}' for lv in support)
        print(f"{ticker:6s}  RESIST  -> {r_str}")
        print(f"{'':6s}  SUPPORT -> {s_str}\n")
    except Exception as e:
        print(f"{ticker}: error ({e})")

# -- Step 4: Pull contracts AT key OI strikes --------------------------------
# Exact-strike match -- no radius needed because levels ARE OI-wall strikes.
all_candidates = []

for ticker, sides in level_map.items():
    tc = chain_df[chain_df["raw_symbol"].str.startswith(ticker)]

    for side_name, levels in sides.items():
        preferred_type = "C" if side_name == "resistance" else "P"

        for lv in levels:
            ref  = lv["level"]
            near = tc[(tc["strike"] - ref).abs() < 0.01].copy()
            if near.empty:
                continue
            near["ticker"]         = ticker
            near["level_side"]     = side_name
            near["level_price"]    = ref
            near["level_label"]    = lv["label"]
            near["level_oi"]       = lv.get("oi", 0)
            near["preferred_type"] = preferred_type
            all_candidates.append(near)

# -- Step 5: Print output (highest OI levels first) --------------------------
if not all_candidates:
    print("No contracts found at OI key levels. Check chain data availability.")
else:
    results = pd.concat(all_candidates, ignore_index=True)
    results["level_oi"] = pd.to_numeric(results["level_oi"], errors="coerce").fillna(0)
    results = results.sort_values(
        ["ticker", "level_side", "level_oi", "dte", "instrument_class"],
        ascending=[True, True, False, True, True]
    )

    display_cols = [
        "ticker", "level_side", "level_label", "level_price", "level_oi",
        "preferred_type", "instrument_class", "strike", "dte",
        "bid", "ask", "mid", "oi", "raw_symbol"
    ]
    display_cols = [c for c in display_cols if c in results.columns]

    for ticker in MAG7:
        subset = results[results["ticker"] == ticker]
        if subset.empty:
            continue
        print(f"\n{'='*70}")
        print(f"  {ticker}   (spot ~${SPOT_PRICES.get(ticker, '?')})")
        print(f"{'='*70}")
        for side in ["resistance", "support"]:
            rows = subset[subset["level_side"] == side]
            if rows.empty:
                continue
            lbl = "^ RESISTANCE -- look for CALLS" if side == "resistance" else "v SUPPORT    -- look for PUTS"
            print(f"\n  {lbl}")
            print(rows[display_cols].to_string(index=False))
        pr

# -- Step 6: Write results to Google Sheets ----------------------------------
from datetime import datetime
run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
results["run_at"] = run_ts

# Tab 1: OI key levels summary (one row per ticker / level)
level_rows = []
for t, sides in level_map.items():
    for side_name, levels in sides.items():
        for lv in levels:
            level_rows.append({
                "run_at":      run_ts,
                "ticker":      t,
                "side":        side_name,
                "label":       lv["label"],
                "level_price": lv["level"],
                "level_oi":    lv.get("oi", ""),
                "spot":        SPOT_PRICES.get(t, ""),
            })
levels_df = pd.DataFrame(level_rows)
write_df_to_sheet(levels_df, tab_name="Mag7_OI_Levels", mode="replace")

# Tab 2: Filtered option contracts at key strikes
write_df_to_sheet(
    results[[c for c in display_cols if c in results.columns] + ["run_at"]],
    tab_name="Mag7_Chain",
    mode="replace",
)

print("\nGoogle Sheets updated.")
print(f"Open: https://docs.google.com/spreadsheets/d/{os.getenv('GOOGLE_SPREADSHEET_ID')}")int()

```

#### Output example (illustrative — also written to Google Sheets tabs Mag7_OI_Levels and Mag7_Chain)

```
======================================================================
  NVDA   (spot ~$875.00)
======================================================================

  ^ RESISTANCE -- look for CALLS
ticker  level_side  level_label                  level_price  level_oi  preferred_type  instrument_class  strike  dte   bid    ask    mid       oi
NVDA    resistance  Call OI Wall (OI=45,200)          900.00     45200  C               C                 900.00    0  4.10   4.40   4.25    45200
NVDA    resistance  Call OI Wall (OI=38,500)          910.00     38500  C               C                 910.00    1  6.30   6.60   6.45    38500
NVDA    resistance  Call OI Wall (OI=21,000)          920.00     21000  C               C                 920.00    0  1.90   2.20   2.05    21000

  v SUPPORT    -- look for PUTS
ticker  level_side  level_label                  level_price  level_oi  preferred_type  instrument_class  strike  dte   bid    ask    mid       oi
NVDA    support     Put OI Wall  (OI=52,100)          850.00     52100  P               P                 850.00    0  3.80   4.10   3.95    52100
NVDA    support     Put OI Wall  (OI=34,700)          840.00     34700  P               P                 840.00    1  5.20   5.50   5.35    34700
NVDA    support     Max Pain                           855.00         0  P               P                 855.00    0  2.10   2.40   2.25    18300
```
#### Extending this script

- **Refresh OI intraday**: Re-fetch the `definition` schema mid-session (e.g. at
  11 AM and 1 PM CT) to catch OI changes as new positions are opened or closed,
  and re-rank the resistance/support walls dynamically.
- **Gamma exposure (GEX) overlay**: Multiply each strike's OI by the contract's
  gamma and dealer sign to compute net GEX. Positive GEX strikes are pinning zones;
  negative GEX strikes are acceleration zones -- combine with OI walls for confluence.
- **Add Greeks filter**: After identifying key OI strikes, filter contracts by delta
  (e.g. 0.20-0.40 for directional plays, <0.15 for lottery lotto trades).
- **Spread builder**: Auto-generate vertical spread legs at adjacent OI walls -- e.g.
  bear call spread between the top-OI resistance and the second-highest resistance strike.
- **Alert on approach**: Use the Live WebSocket (Template 3) to fire an alert when
  the underlying's last trade price comes within 0.5% of any OI wall or Max Pain level.
- **Confluence scoring**: Score each level by combining OI rank + Max Pain proximity.
  A strike that is both a top call OI wall AND near Max Pain gets a higher score and
  should be prioritized for trade selection.
---

## Output Conventions

When generating scripts, always follow these patterns:

- **Prices**: DataBento stores fixed-point prices as integers (nanoseconds scale). Divide by `1e9` to get dollar values.
- **Timestamps**: Convert with `pd.to_datetime(df.index)` or use `.to_df()` which handles this automatically.
- **Symbol format**: OSI format `AAAA  YYMMDDCXXXXXXXX` (6-char ticker padded, 8-char strike padded with leading zeros).
- **Error handling**: Wrap API calls in `try/except db.BentoError` for quota and auth errors.
- **Rate limits**: Batch symbol requests where possible; avoid per-contract loops for large chains.

---

## Common DataBento Datasets for Options

| Dataset | Description |
|---|---|
| `OPRA.PILLAR` | US equity options (all exchanges via OPRA) |
| `GLBX.MDP3` | CME futures and options (ES, NQ, CL, GC options) |
| `IFEU.IMPACT` | ICE Europe futures and options |

---

## Helpful DataBento API Reference

- Full API docs: https://databento.com/docs
- Available schemas: https://databento.com/docs/schemas-and-data-formats
- Symbol lookup: https://databento.com/docs/symbology
- Pricing / cost estimator: https://app.databento.com/portal/cost-estimator
