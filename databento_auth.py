"""
Probe Databento with the stored credentials and print the API key.
Run once, then paste the key into .env as DATABENTO_API_KEY=db-xxxx
"""
import os, requests
from dotenv import load_dotenv
load_dotenv()

user = os.environ['DATABENTO_USER']
pwd  = os.environ['DATABENTO_PASSWORD']

BASE = 'https://hist.databento.com/v0'

# Databento REST API uses HTTP Basic Auth: key=username, password=empty string
# But to *retrieve* a key from an account, try the known endpoints
endpoints = [
    ('GET',  f'{BASE}/metadata.list_datasets'),
    ('GET',  f'{BASE}/users.me'),
    ('GET',  f'{BASE}/auth.keys'),
]

print(f"Testing credentials for: {user}")
print()

for method, url in endpoints:
    resp = requests.request(method, url, auth=(user, pwd), timeout=10)
    label = url.split('.')[-1]
    print(f"  {label:25s}  HTTP {resp.status_code}  {resp.text[:120]}")

print()
print("If you see HTTP 200 above, copy the 'key' value and run:")
print("  python -c \"from dotenv import set_key; set_key('.env','DATABENTO_API_KEY','db-...')\"")
