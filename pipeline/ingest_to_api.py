#!/usr/bin/env python3
"""
Reads a JSONL events file and POSTs batches to the API.
Also handles POS CSV ingestion.

Usage:
    python ingest_to_api.py --events output/events.jsonl --api http://localhost:8000
    python ingest_to_api.py --pos data/pos_transactions.csv --api http://localhost:8000
"""
import argparse
import json
import sys
import time
import logging
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ingest_to_api")

BATCH_SIZE = 200
RETRY_ATTEMPTS = 3


def ingest_events(events_path: str, api_url: str) -> dict:
    batch = []
    total_accepted = total_duplicates = total_invalid = 0

    with open(events_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                batch.append(event)
            except json.JSONDecodeError as e:
                logger.warning("Bad JSON line: %s", e)
                continue

            if len(batch) >= BATCH_SIZE:
                result = _post_batch(batch, api_url)
                total_accepted += result.get("accepted", 0)
                total_duplicates += result.get("duplicates", 0)
                total_invalid += result.get("invalid", 0)
                batch.clear()

    if batch:
        result = _post_batch(batch, api_url)
        total_accepted += result.get("accepted", 0)
        total_duplicates += result.get("duplicates", 0)
        total_invalid += result.get("invalid", 0)

    summary = {
        "total_accepted": total_accepted,
        "total_duplicates": total_duplicates,
        "total_invalid": total_invalid,
    }
    logger.info("Ingest complete: %s", summary)
    return summary


def _post_batch(events: list, api_url: str) -> dict:
    url = f"{api_url.rstrip('/')}/events/ingest"
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = requests.post(url, json={"events": events}, timeout=30)
            if resp.status_code in (200, 201):
                return resp.json()
            logger.warning("POST %s → %d: %s", url, resp.status_code, resp.text[:200])
        except requests.exceptions.RequestException as exc:
            logger.warning("Attempt %d failed: %s", attempt, exc)
            if attempt < RETRY_ATTEMPTS:
                time.sleep(2 ** attempt)
    return {}


def ingest_pos(pos_path: str, api_url: str) -> dict:
    import csv
    transactions = []
    with open(pos_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            transactions.append({
                "store_id": row.get("store_id", ""),
                "transaction_id": row.get("transaction_id", ""),
                "timestamp": row.get("timestamp", ""),
                "basket_value_inr": float(row.get("basket_value_inr", 0)),
            })

    url = f"{api_url.rstrip('/')}/pos/ingest"
    for i in range(0, len(transactions), BATCH_SIZE):
        batch = transactions[i:i + BATCH_SIZE]
        try:
            resp = requests.post(url, json={"transactions": batch}, timeout=30)
            logger.info("POS batch %d: %s", i // BATCH_SIZE + 1, resp.json())
        except Exception as exc:
            logger.warning("POS ingest failed: %s", exc)

    return {"total": len(transactions)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest pipeline output into Store Intelligence API")
    parser.add_argument("--events", help="Path to events.jsonl file")
    parser.add_argument("--pos", help="Path to pos_transactions.csv file")
    parser.add_argument("--api", default="http://localhost:8000", help="API base URL")
    args = parser.parse_args()

    if not args.events and not args.pos:
        parser.error("Provide --events or --pos (or both)")

    if args.events:
        result = ingest_events(args.events, args.api)
        print(json.dumps(result))

    if args.pos:
        result = ingest_pos(args.pos, args.api)
        print(json.dumps(result))


if __name__ == "__main__":
    main()
