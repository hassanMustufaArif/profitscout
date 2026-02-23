from dotenv import load_dotenv
from apify_client import ApifyClient
import os

load_dotenv()

token = os.getenv("APIFY_API_TOKEN")
print(f"Token loaded: {token}")
print(f"Token length: {len(token) if token else 0}")

client = ApifyClient(token)

# Test connection
try:
    user = client.user().get()
    print(f"Connected! Apify user: {user.get('username')}")
except Exception as e:
    print(f"Error: {e}")
    