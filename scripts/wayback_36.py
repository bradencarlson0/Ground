#!/usr/bin/env python3
"""Collect first Wayback captures for the 36 AL/FL 7 Brew pages lacking c_openDate."""
from __future__ import annotations
import csv, hashlib, json, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data/7brew_field_absent_36.csv"
OUT = ROOT / "wayback_outputs"
CDX = "https://web.archive.org/cdx/search/cdx"
UA = "BC-Land-USA-opening-research/1.0"


def read_rows():
    with SEED.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def query(url: str, collapse: str = "digest"):
    params = {
        "url": url,
        "output": "json",
        "fl": "timestamp,original,statuscode,mimetype,digest",
        "filter": "statuscode:200",
        "filter": "mimetype:text/html",
        "collapse": collapse,
        "limit": "25",
        "from": "2021",
    }
    req = Request(CDX + "?" + urlencode(params), headers={"User-Agent": UA})
    with urlopen(req, timeout=90) as response:
        return json.load(response)


def first_capture(url: str):
    variants = [url, url.replace("https://", "http://"), url.rstrip("/") + "/"]
    candidates = []
    errors = []
    for variant in dict.fromkeys(variants):
        for attempt in range(4):
            try:
                data = query(variant)
                if len(data) > 1:
                    header = data[0]
                    for values in data[1:]:
                        row = dict(zip(header, values))
                        row["queried_url"] = variant
                        candidates.append(row)
                break
            except Exception as exc:
                errors.append(f"{variant}: {type(exc).__name__}: {exc}")
                time.sleep(3 * (attempt + 1))
        time.sleep(0.5)
    candidates.sort(key=lambda r: r["timestamp"])
    return (candidates[0] if candidates else None), candidates, errors


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    results = []
    audit = []
    for index, row in enumerate(read_rows(), 1):
        url = row["locator_url"]
        first, candidates, errors = first_capture(url)
        if first:
            ts = first["timestamp"]
            date_value = f"{ts[:4]}-{ts[4:6]}-{ts[6:8]}"
            archive_url = f"https://web.archive.org/web/{ts}/{first['original']}"
        else:
            date_value = ""
            archive_url = ""
        results.append({
            "state": row["state"], "city": row["city"], "address": row["address"],
            "locator_url": url, "wayback_first_seen": date_value,
            "wayback_timestamp": first.get("timestamp", "") if first else "",
            "wayback_original": first.get("original", "") if first else "",
            "wayback_archive_url": archive_url,
            "statuscode": first.get("statuscode", "") if first else "",
            "mimetype": first.get("mimetype", "") if first else "",
            "digest": first.get("digest", "") if first else "",
            "candidate_capture_count": len(candidates),
            "errors": " | ".join(errors[-3:]),
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "interpretation": "First archived locator-page visibility; not an opening date.",
        })
        audit.append({"locator_url": url, "candidates": candidates, "errors": errors})
        print(f"{index:02d}/36 {row['state']} {row['city']}: {date_value or 'NONE'}", flush=True)
        time.sleep(1.0)
    fields = list(results[0])
    with (OUT / "wayback_first_seen_36.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields); w.writeheader(); w.writerows(results)
    (OUT / "wayback_audit_36.jsonl").write_text(
        "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in audit), encoding="utf-8")
    summary = {
        "locations": len(results),
        "first_capture_found": sum(bool(r["wayback_first_seen"]) for r in results),
        "no_capture": sum(not r["wayback_first_seen"] for r in results),
    }
    (OUT / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))

if __name__ == "__main__":
    main()
