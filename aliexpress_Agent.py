
import json
import os
from pydantic import BaseModel , Field

from agents import Agent, Runner, function_tool
from agents import enable_verbose_stdout_logging
from crawlbase import CrawlingAPI
enable_verbose_stdout_logging()

OPENAI_API_KEY  = "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"           # your OpenAI key
CRAWLBASE_TOKEN = "ZydHLLIg5Wv8HvjV-WJpSA"   # your Crawlbase JS token

os.environ["OPENAI_API_KEY"] = OPENAI_API_KEY

crawlbase = CrawlingAPI({"token": CRAWLBASE_TOKEN})

class ProductOutput(BaseModel):
    URL: str
    Price: int
    MOQ: int
    Supplier_Name: str = Field(alias="Supplier Name")

class top3_output(BaseModel):
    products: list[ProductOutput]



@function_tool
def search_products(keyword: str, page: int = 1) -> str:
    """Search AliExpress for products by keyword."""
    clean = keyword.lower().replace(" ", "-")
    url = f"https://www.aliexpress.com/w/wholesale-{clean}.html?page={page}&g=y"
    print(f"Query to tool Search: {keyword} ")
    response = crawlbase.get(url, {"scraper": "aliexpress-serp", "scroll": "true" , "country": "US"})
    
    if response["status_code"] != 200:
        return json.dumps({"error": f"Status {response['status_code']}"})

    data = json.loads(response["body"].decode("utf-8") if isinstance(response["body"], bytes) else response["body"])
    products = data.get("body", {}).get("products", [])
    return json.dumps(products[:4])

@function_tool
def get_product_detail(product_url: str) -> str:
    """Fetch full details of an AliExpress product including MOQ and Supplier."""
    response = crawlbase.get(product_url, {"scraper": "aliexpress-product", "scroll": "true"})
    if response["status_code"] != 200:
        return json.dumps({"error": "Failed to fetch details"})
    
    data = json.loads(response["body"].decode("utf-8") if isinstance(response["body"], bytes) else response["body"])
    p = data.get("body", {})
    return json.dumps(p)


agent = Agent(
    name="White-Label Sourcing Agent",
    model="gpt-4o-mini",
    output_type=ProductOutput,
    instructions="""
# ROLE
You are an expert Strategic Sourcing Specialist specializing in identifying the original white-label manufacturers for consumer brands on AliExpress.

# CONTEXT
Many direct-to-consumer (DTC) brands use standard "off-the-shelf" products from AliExpress. Your mission is to bypass the brand and find the raw manufacturing source.

# OPERATIONAL PROTOCOL (THINK-ACT-OBSERVE)
For every request, follow these steps before providing a final answer:

1. **ANALYSIS**: Deconstruct the branded product into its core "Generic DNA":
   - **Active Ingredients**: Extract key compounds (e.g., Ashwagandha,Reusable Cup , Magnesium).
   - **Delivery Form**: (e.g., Gummies, Powder, Vegan Capsules).
   - **Specifications**: Dosage per serving and total unit count (e.g., 60ct, 500mg).
   - **Aesthetics**: Identify unique packaging shapes (e.g., amber glass bottle, matte black tub).

2. **EXECUTION**: Call `search_products` using a query of "title given exact without brand name. 
   - **Strict Rule**: Never include the original Brand Name in the search query to avoid branded resellers.

3. **EVALUATION**: Compare search results against the branded product using these priority weights:
   - **Formula Parity (Mandatory)**: Ingredients and dosages must be identical.
   - **Visual Match**: Does the manufacturer offer the same packaging form factor?
   - **Service Match**: Prioritize manufacturers explicitly listing "OEM", "Custom Logo", or "Private Label" services.

4. **FINAL SELECTION**: Select the single best-fit manufacturer based on the highest parity score.

# CONSTRAINTS & OUTPUT
- **Data Integrity**: Price and ID must be returned as integers only.
- **Ambiguity**: If ingredients are unclear from the brand name, ask the user for a nutrition label/ingredient list before searching.
- **Error Handling**: If no exact formula match is found, give a "close" alternative you can match.


Important : "YOU WILL ONLY CALL search_products for 2 times maximum . IF you got Source on first call THEN dont call again"
""", 
    tools=[search_products],
)

if __name__ == "__main__":
    query = " Nello Supercalm Tub (2-Pack): Calming Drink Mix with KSM-66"
    result = Runner.run_sync(agent, f"Find white-label source for: {query}", max_turns=15)
    print("\n" + str(result.final_output))


