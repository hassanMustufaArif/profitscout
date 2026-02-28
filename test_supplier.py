from apify_client import ApifyClient
import os
from dotenv import load_dotenv
load_dotenv()

apify = ApifyClient(os.getenv("APIFY_API_TOKEN"))

# Test with a simple keyword
run = apify.actor("shareze001/scrape-alibaba-products-by-keywords").call(
    run_input={
        "search_url": "https://www.alibaba.com/trade/search?tab=all&SearchText=face+mask&has4Tab=true",
        "size": 3,
        "skip_page_parameter": True,
    },
    timeout_secs=120
)

results = list(apify.dataset(run["defaultDatasetId"]).iterate_items())
print(f"Results: {len(results)}")
if results:
    print(f"First result: {results[0].get('subject', 'no title')}")