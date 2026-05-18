"""
One-time setup: create the Google Spreadsheet, share it with the user,
and print the spreadsheet ID to put in .env.
"""
import json
import gspread
from google.oauth2.service_account import Credentials

SA_FILE   = "jakevolume-837eb417f8e2.json"
USER_EMAIL = "malireddigari@gmail.com"

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

creds = Credentials.from_service_account_file(SA_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)

print("Authenticating with Google... ", end="", flush=True)
print("OK")

print("Creating spreadsheet via Sheets API... ", end="", flush=True)
from googleapiclient.discovery import build
sheets_svc = build("sheets", "v4", credentials=creds)

body = {"properties": {"title": "Jakevolume Trading Log"}}
resp = sheets_svc.spreadsheets().create(body=body).execute()
ss_id = resp["spreadsheetId"]
print(f"OK  →  {ss_id}")

print(f"Opening spreadsheet in gspread... ", end="", flush=True)
ss = gc.open_by_key(ss_id)
print("OK")

print(f"Sharing with {USER_EMAIL}... ", end="", flush=True)
try:
    ss.share(USER_EMAIL, perm_type="user", role="writer")
    print("OK")
except Exception as e:
    print(f"SKIPPED (Drive API not enabled — share manually): {e}")

print("\n" + "="*60)
print(f"GOOGLE_SPREADSHEET_ID={ss.id}")
print("="*60)
print(f"\nOpen: https://docs.google.com/spreadsheets/d/{ss.id}/edit")
print("\nAdd this ID to your .env file.")
