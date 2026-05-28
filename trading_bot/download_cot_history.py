"""Download 2+ years of COT data for backtesting."""
import sys, os, json
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "cot_dashboard"))
from cot_fetcher import COTDataFetcher

fetcher = COTDataFetcher()

# Map bot pairs to COT instrument names
PAIRS_COT = {
    "XAU/USD": "XAU (Золото)",
    "USD/JPY": "USD/JPY",
}

START = "2010-01-01"
END = "2026-05-27"

result = {}
for pair_name, cot_name in PAIRS_COT.items():
    print(f"\nDownloading {cot_name}...")
    records = fetcher.fetch_historical_data(cot_name, START, END, limit=900)
    if records:
        result[pair_name] = records
    else:
        print(f"  WARNING: no data for {cot_name}")

result["metadata"] = {
    "downloaded_at": datetime.now().isoformat(),
    "date_range": f"{START} -> {END}",
}

out = os.path.join(os.path.dirname(__file__), "cot_history.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(result, f, ensure_ascii=False, indent=2)

total = sum(len(v) for k, v in result.items() if k != "metadata")
print(f"\n{'='*50}")
print(f"Saved {total} records to {out}")
for k, v in result.items():
    if k != "metadata":
        print(f"  {k}: {len(v)} records ({v[0]['date']} -> {v[-1]['date']})")
