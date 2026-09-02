import json
import base64
import os

AUTH_FILE = os.path.expanduser(r"~\.codex\auth.json")
try:
    with open(AUTH_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
        id_token = data.get("tokens", {}).get("id_token", "")
        
        parts = id_token.split(".")
        if len(parts) >= 2:
            payload = parts[1]
            payload += "=" * ((4 - len(payload) % 4) % 4)
            decoded = base64.b64decode(payload).decode("utf-8")
            parsed = json.loads(decoded)
            auth_data = parsed.get("https://api.openai.com/auth", {})
            print("Plan:", auth_data.get("chatgpt_plan_type"))
            print("Until:", auth_data.get("chatgpt_subscription_active_until"))
except Exception as e:
    print(e)
