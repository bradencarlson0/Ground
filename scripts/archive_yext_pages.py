#!/usr/bin/env python3
"""Freeze raw official 7 Brew Yext stand pages referenced by an extraction CSV."""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "raw_yext_pages"
UA = "Mozilla/5.0 (compatible; BC-Land-USA-Yext-Date-Audit/2.0)"


def fetch(url: str, attempts: int = 5) -> tuple[int, bytes, str, str]:
    last = ""
    for attempt in range(attempts):
        try:
            req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"})
            with urlopen(req, timeout=60) as response:
                return getattr(response, "status", 200), response.read(), response.geturl(), ""
        except HTTPError as exc:
            last = f"HTTPError {exc.code}: {exc.reason}"
        except (URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 ** attempt)
    return 0, b"", url, last


def main(path: str) -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with Path(path).open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    manifest = []
    for index, row in enumerate(rows, 1):
        url = row["locator_url"]
        observed = datetime.now(timezone.utc).isoformat()
        status, body, final_url, error = fetch(url)
        digest = hashlib.sha256(body).hexdigest() if body else ""
        rel = ""
        if body:
            target = OUT / f"{digest}.html"
            target.write_bytes(body)
            rel = str(target.relative_to(ROOT))
        manifest.append({
            "locator_url": url,
            "status": status,
            "final_url": final_url,
            "bytes": len(body),
            "sha256": digest,
            "raw_path": rel,
            "observed_at_utc": observed,
            "error": error,
        })
        print(f"{index:03d}/{len(rows):03d} {status} {url} {digest[:12] or '-'}", flush=True)
        time.sleep(0.15)
    (OUT / "manifest.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in manifest), encoding="utf-8"
    )
    failed = [item for item in manifest if item["status"] != 200]
    print(json.dumps({"rows": len(manifest), "http_200": len(manifest) - len(failed), "failed": len(failed)}, indent=2))
    return 1 if failed else 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: archive_yext_pages.py <extraction.csv>")
    raise SystemExit(main(sys.argv[1]))
