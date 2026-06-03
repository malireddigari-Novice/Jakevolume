"""
Connection health check — probes every external dependency Jakevolume needs.

Read-only: no orders are placed, no Sheets rows written, no DB rows inserted.
Run:  python check_connections.py
"""
import sys

import config


def _ok(msg):   print(f"  [ OK ]  {msg}")
def _fail(msg): print(f"  [FAIL]  {msg}")
def _skip(msg): print(f"  [SKIP]  {msg}")


def check_postgres() -> bool:
    print("PostgreSQL")
    try:
        import db.ops as ops
        ops.init_pool()
        conn = ops._get()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT version();")
                ver = cur.fetchone()[0]
        finally:
            ops._put(conn)
        _ok(f"{config.DB_USER}@{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME}")
        _ok(ver.split(',')[0])
        return True
    except Exception as exc:
        _fail(f"{config.DB_HOST}:{config.DB_PORT}/{config.DB_NAME} - {exc}")
        return False


def check_schwab() -> bool:
    print("Charles Schwab (market data)")
    if not config.SCHWAB_API_KEY or not config.SCHWAB_APP_SECRET:
        _fail("SCHWAB_API_KEY / SCHWAB_APP_SECRET not set in .env")
        return False
    try:
        from data.schwab_client import SchwabClient
        c = SchwabClient()
        c.login()                       # loads/refreshes token, no browser if valid
        q = c.get_quote(config.SYMBOLS[0])
        _ok(f"token valid; {config.SYMBOLS[0]} quote = {q['price']}")
        return True
    except Exception as exc:
        _fail(f"{exc}")
        return False


def check_alpaca() -> bool:
    print("Alpaca (execution)")
    if not config.ALPACA_API_KEY or not config.ALPACA_SECRET_KEY:
        _fail("ALPACA_API_KEY / ALPACA_SECRET_KEY not set in .env")
        return False
    try:
        from data.alpaca_client import AlpacaClient
        c = AlpacaClient()
        mode = "PAPER" if config.ALPACA_PAPER else "LIVE"
        if c.verify():
            enabled = "ENABLED" if config.ALPACA_ENABLED else "disabled (no auto-trade)"
            _ok(f"{mode} account reachable; execution {enabled}")
            return True
        _fail(f"{mode} credentials rejected")
        return False
    except Exception as exc:
        _fail(f"{exc}")
        return False


def check_discord() -> bool:
    print("Discord (alerts)")
    import requests
    any_ok = False
    for label, url in [
        ("DISCORD_WEBHOOK_URL", config.DISCORD_WEBHOOK_URL),
        ("DISCORD_MORNING_WEBHOOK_URL", config.DISCORD_MORNING_WEBHOOK_URL),
    ]:
        if not url:
            _skip(f"{label} not set")
            continue
        try:
            # GET on a webhook URL returns its metadata without posting a message.
            r = requests.get(url, timeout=10)
            if r.status_code == 200:
                name = r.json().get("name", "?")
                _ok(f"{label} -> webhook '{name}' live")
                any_ok = True
            else:
                _fail(f"{label} -> HTTP {r.status_code}: {r.text[:120]}")
        except Exception as exc:
            _fail(f"{label} -> {exc}")
    return any_ok


def check_sheets() -> bool:
    print("Google Sheets (logging)")
    if not config.GOOGLE_SPREADSHEET_ID:
        _fail("GOOGLE_SPREADSHEET_ID not set in .env")
        return False
    try:
        import os
        if not os.path.exists(config.GOOGLE_SERVICE_ACCOUNT_FILE):
            _fail(f"service-account file missing: {config.GOOGLE_SERVICE_ACCOUNT_FILE}")
            return False
        import gspread
        from google.oauth2.service_account import Credentials
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive',
        ]
        creds = Credentials.from_service_account_file(
            config.GOOGLE_SERVICE_ACCOUNT_FILE, scopes=scopes)
        gc = gspread.authorize(creds)
        ss = gc.open_by_key(config.GOOGLE_SPREADSHEET_ID)
        tabs = [ws.title for ws in ss.worksheets()]
        _ok(f"opened '{ss.title}' as {creds.service_account_email}")
        missing = [n for n in config.SHEET_NAMES.values() if n not in tabs]
        if missing:
            _skip(f"tabs not yet created: {', '.join(missing)}")
        return True
    except Exception as exc:
        _fail(f"{exc}")
        return False


def main() -> int:
    print("=" * 60)
    print("Jakevolume connection health check")
    print("=" * 60)
    results = {
        "PostgreSQL": check_postgres(),
        "Schwab":     check_schwab(),
        "Alpaca":     check_alpaca(),
        "Discord":    check_discord(),
        "Sheets":     check_sheets(),
    }
    print("=" * 60)
    for name, ok in results.items():
        print(f"  {name:<12} {'OK' if ok else 'FAIL'}")
    print("=" * 60)
    return 0 if all(results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
