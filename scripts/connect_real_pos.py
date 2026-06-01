"""
Convert Brigade_Bangalore_10_April_26 real POS CSV to the
store-intelligence system format and ingest it live into the API.

Output format: store_id,transaction_id,timestamp,basket_value_inr
- Group by invoice_number (one row per transaction)
- Basket value = sum of NMV per invoice (carry bags / 0-value excluded)
- Timestamp uses today's UTC date + original time-of-day
  so the seed service can re-date live POS records on restart
"""
import csv, json, sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import urllib.request, urllib.error

SRC = Path("/data/Brigade_Bangalore_10_April_26 (1)bc6219c.csv")
OUT = Path("/app/data/pos_today.csv")
API = "http://host.docker.internal:8000"
STORE_ID = "STORE_BLR_002"
TODAY = datetime.now(timezone.utc).date().isoformat()

# ── 1. Read and group by invoice ──────────────────────────────────────────
invoices = defaultdict(lambda: {"nmv": 0.0, "time": None})

with open(SRC, encoding="utf-8") as f:
    for row in csv.DictReader(f):
        if row["invoice_type"] != "sales":
            continue
        inv = row["invoice_number"].strip()
        nmv = float(row["NMV"] or 0)
        if nmv <= 0:
            continue
        invoices[inv]["nmv"] += nmv
        if invoices[inv]["time"] is None:
            # Parse time from order_time (HH:MM:SS)
            invoices[inv]["time"] = row["order_time"].strip()[:8]

print(f"Unique invoices with revenue: {len(invoices)}")

# ── 2. Build system-format rows ───────────────────────────────────────────
pos_rows = []
for inv, data in invoices.items():
    ts = f"{TODAY}T{data['time']}Z"
    pos_rows.append({
        "store_id": STORE_ID,
        "transaction_id": inv,
        "timestamp": ts,
        "basket_value_inr": round(data["nmv"], 2),
    })

# Sort by timestamp
pos_rows.sort(key=lambda x: x["timestamp"])

# ── 3. Write pos_today.csv ────────────────────────────────────────────────
with open(OUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=["store_id", "transaction_id", "timestamp", "basket_value_inr"])
    writer.writeheader()
    writer.writerows(pos_rows)

print(f"Written {len(pos_rows)} transactions to {OUT}")
for r in pos_rows:
    print(f"  {r['transaction_id']}  {r['timestamp']}  Rs {r['basket_value_inr']:,.2f}")

# ── 4. POST live to the running API ──────────────────────────────────────
print(f"\nPosting {len(pos_rows)} transactions to {API}/pos/ingest ...")
payload = json.dumps({"transactions": pos_rows}).encode("utf-8")
req = urllib.request.Request(
    f"{API}/pos/ingest",
    data=payload,
    headers={"Content-Type": "application/json"},
    method="POST",
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = resp.read().decode()
        print(f"API response ({resp.status}): {body}")
except urllib.error.HTTPError as e:
    body = e.read().decode()
    print(f"HTTP {e.code}: {body}")
except Exception as e:
    print(f"Could not reach API: {e}")
    print("(pos_today.csv was updated — data will load on next seed restart)")
