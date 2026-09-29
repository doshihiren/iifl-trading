import os
import hashlib
from dotenv import load_dotenv

load_dotenv("/root/iifl/.env")

api_key = os.getenv("IIFL_API_KEY")
api_secret = os.getenv("IIFL_API_SECRET")
client_id = os.getenv("IIFL_CLIENT_ID")

print("IIFL configuration check")
print("-------------------------")

print("API Key configured :", bool(api_key))
print("API Secret configured :", bool(api_secret))
print("Client ID configured :", bool(client_id))

if not api_key:
    raise SystemExit("ERROR: IIFL_API_KEY missing")

if not api_secret:
    raise SystemExit("ERROR: IIFL_API_SECRET missing")

if not client_id:
    raise SystemExit("ERROR: IIFL_CLIENT_ID missing")

print()
print("Configuration loaded successfully.")
print("Credentials are NOT displayed.")
