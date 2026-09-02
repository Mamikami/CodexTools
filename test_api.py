import json
import urllib.request
import os

AUTH_FILE = os.path.expanduser(r"~\.codex\auth.json")
try:
    with open(AUTH_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        tokens = data.get("tokens", {})
except:
    tokens = {}

token = tokens.get("access_token")
account_id = tokens.get("account_id")

def fetch_url(url):
    print(f"\nFetching {url}")
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36")
    if account_id: req.add_header("Chatgpt-Account-Id", account_id)
    try:
        with urllib.request.urlopen(req) as response:
            print("Status:", response.status)
            print("Body:", response.read().decode("utf-8")[:500])
    except Exception as e:
        print("Error:", e)

fetch_url("https://chatgpt.com/backend-api/me/message_cap")
fetch_url("https://chatgpt.com/backend-api/usage")
fetch_url("https://chatgpt.com/backend-api/account/usage")
