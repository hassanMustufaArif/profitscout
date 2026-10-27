import os
from dotenv import load_dotenv

load_dotenv()

# Oxylabs Agent Browser (cloud Chromium with residential IPs, CAPTCHA handling). Preferred.
AB_USER = os.getenv("OXYLABS_AB_USER")          # include the account suffix, e.g. user_ab12
AB_PASS = os.getenv("OXYLABS_AB_PASS")
AB_HOST = os.getenv("OXYLABS_AB_HOST", "hb.oxylabs.io")

# Fallback: local Chromium through Oxylabs Residential Proxies.
RESI_USER = os.getenv("OXYLABS_RESI_USER")      # e.g. customer-xxxx
RESI_PASS = os.getenv("OXYLABS_RESI_PASS")
RESI_SERVER = os.getenv("OXYLABS_RESI_SERVER", "http://us-pr.oxylabs.io:10000")

COUNTRY = os.getenv("SOURCING_COUNTRY", "US")
MAX_ATTEMPTS = int(os.getenv("SOURCING_MAX_ATTEMPTS", "3"))
PAGE_TIMEOUT_MS = int(os.getenv("SOURCING_PAGE_TIMEOUT_MS", "45000"))
MIN_SIMILARITY = float(os.getenv("SOURCING_MIN_SIMILARITY", "0.80"))   # 0-1
KEEP_TOP = int(os.getenv("SOURCING_KEEP_TOP", "5"))
POLL_SECONDS = int(os.getenv("SOURCING_POLL_SECONDS", "3"))
CONCURRENCY = int(os.getenv("SOURCING_CONCURRENCY", "3"))
DEBUG_DIR = os.getenv("SOURCING_DEBUG_DIR", "/tmp/sourcing_debug")   # screenshots + HTML on failure
