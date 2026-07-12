#!/usr/bin/env python3
"""Recover official Yext c_openDate/c_approvalDate values for 7 Brew AL/FL.

The public Yext Pages HTML embeds page props as:
    JSON.parse(decodeURIComponent("<percent-encoded JSON>"))
This script crawls the official AL and FL directories, fetches each stand page,
decodes every embedded page-props object, and recursively extracts the custom
fields without relying on visible page text.
"""
from __future__ import annotations

import csv
import hashlib
import html as html_lib
import json
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urljoin, urlparse, urlunparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "outputs"
BASE = "https://locations.7brew.com"
STATES = ("al", "fl")
EXPECTED = {"al": 37, "fl": 51}
UA = "Mozilla/5.0 (compatible; BC-Land-USA-Yext-Date-Audit/2.0; +https://github.com/bradencarlson0/Ground)"

HYDRATION_RE = re.compile(
    r'JSON\.parse\(decodeURIComponent\(["\']([^"\']+)["\']\)\)', re.I | re.S
)
ISO_RE = re.compile(r"(?<!\d)(20\d{2})[-/](0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])(?!\d)")
COMPACT_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])([0-2]\d|3[01])(?!\d)")
US_RE = re.compile(r"(?<!\d)(0?[1-9]|1[0-2])[-/](0?[1-9]|[12]\d|3[01])[-/](20\d{2})(?!\d)")


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        for key, value in attrs:
            if key.lower() == "href" and value:
                self.links.append(value)


@dataclass
class FetchResult:
    status: int
    body: bytes
    final_url: str
    error: str = ""


def fetch(url: str, attempts: int = 5, timeout: int = 60) -> FetchResult:
    last = ""
    for attempt in range(attempts):
        try:
            req = Request(
                url,
                headers={
                    "User-Agent": UA,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Cache-Control": "no-cache",
                },
            )
            with urlopen(req, timeout=timeout) as resp:
                return FetchResult(
                    status=getattr(resp, "status", 200),
                    body=resp.read(),
                    final_url=resp.geturl(),
                )
        except HTTPError as exc:
            last = f"HTTPError {exc.code}: {exc.reason}"
            if 400 <= exc.code < 500 and exc.code not in (408, 429):
                break
        except (URLError, TimeoutError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2 ** attempt)
    return FetchResult(status=0, body=b"", final_url=url, error=last)


def decode_layers(text: str, rounds: int = 5) -> list[str]:
    seen: dict[str, None] = {}
    frontier = [text]
    for _ in range(rounds):
        next_frontier: list[str] = []
        for value in frontier:
            candidates = (
                value,
                html_lib.unescape(value),
                unquote(value),
                value.replace(r"\/", "/").replace(r'\"', '"'),
            )
            for candidate in candidates:
                if candidate not in seen:
                    seen[candidate] = None
                    next_frontier.append(candidate)
        frontier = next_frontier
        if not frontier:
            break
    return list(seen)


def hydration_objects(text: str) -> list[Any]:
    objects: list[Any] = []
    for layer in decode_layers(text, rounds=3):
        for match in HYDRATION_RE.finditer(layer):
            encoded = match.group(1)
            for candidate in decode_layers(encoded, rounds=4):
                try:
                    obj = json.loads(candidate)
                except Exception:
                    continue
                objects.append(obj)
                break
    for match in re.finditer(
        r'<script[^>]+type=["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
        text,
        re.I | re.S,
    ):
        try:
            objects.append(json.loads(html_lib.unescape(match.group(1)).strip()))
        except Exception:
            pass
    return objects


def walk(obj: Any, path: str = "$") -> Iterable[tuple[str, str, Any]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            child = f"{path}.{key}"
            yield child, str(key), value
            yield from walk(value, child)
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from walk(value, f"{path}[{idx}]")


def scalar_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalize_date(value: Any) -> str:
    text = scalar_text(value)
    for layer in decode_layers(text, rounds=4):
        for regex, order in ((ISO_RE, "ymd"), (COMPACT_RE, "ymd"), (US_RE, "mdy")):
            match = regex.search(layer)
            if not match:
                continue
            if order == "ymd":
                year, month, day = map(int, match.groups())
            else:
                month, day, year = map(int, match.groups())
            try:
                return date(year, month, day).isoformat()
            except ValueError:
                continue
    return ""


def normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.lower())


def extract_custom_fields(objects: list[Any], raw_text: str) -> dict[str, Any]:
    aliases = {
        "c_openDate": {"copendate", "opendate"},
        "c_approvalDate": {"capprovaldate", "approvaldate"},
    }
    candidates: dict[str, list[dict[str, str]]] = {name: [] for name in aliases}
    ids: list[str] = []

    for obj_index, obj in enumerate(objects):
        for path, key, value in walk(obj):
            normalized = normalize_key(key)
            for field, names in aliases.items():
                if normalized in names:
                    raw = scalar_text(value)
                    candidates[field].append(
                        {
                            "source": f"hydration[{obj_index}]",
                            "path": path,
                            "raw": raw,
                            "date": normalize_date(value),
                        }
                    )
            if normalized in {"entityid", "locationid", "businessid"}:
                raw_id = scalar_text(value).strip()
                if raw_id and raw_id not in ids:
                    ids.append(raw_id)
            elif normalized == "id" and any(token in path.lower() for token in ("document", "entity", "meta")):
                raw_id = scalar_text(value).strip()
                if raw_id and len(raw_id) < 100 and raw_id not in ids:
                    ids.append(raw_id)

    for layer_index, layer in enumerate(decode_layers(raw_text, rounds=6)):
        for field, names in aliases.items():
            for name in names | {field.lower()}:
                for match in re.finditer(re.escape(name), layer, re.I):
                    window = layer[max(0, match.start() - 100): min(len(layer), match.start() + 800)]
                    parsed = normalize_date(window)
                    if parsed:
                        candidates[field].append(
                            {
                                "source": f"raw_layer[{layer_index}]",
                                "path": f"offset:{match.start()}",
                                "raw": window[:500].replace("\n", " "),
                                "date": parsed,
                            }
                        )
                    if len(candidates[field]) >= 50:
                        break

    out: dict[str, Any] = {"entity_ids": ids[:20]}
    for field in aliases:
        dedup: list[dict[str, str]] = []
        seen = set()
        for item in candidates[field]:
            token = (item["source"], item["path"], item["raw"], item["date"])
            if token not in seen:
                seen.add(token)
                dedup.append(item)
        dated = [item for item in dedup if item["date"]]
        chosen = dated[0] if dated else (dedup[0] if dedup else {})
        out[field] = chosen.get("date", "")
        out[field + "_raw"] = chosen.get("raw", "")
        out[field + "_path"] = chosen.get("path", "")
        out[field + "_source"] = chosen.get("source", "")
        out[field + "_all_dates"] = sorted({item["date"] for item in dated})
        out[field + "_candidate_count"] = len(dedup)
        out[field + "_candidates"] = dedup[:12]
    return out


def directory_links(text: str, page_url: str, state: str) -> tuple[set[str], set[str]]:
    location_urls: set[str] = set()
    city_urls: set[str] = set()
    parser = LinkParser()
    try:
        parser.feed(text)
    except Exception:
        pass
    strings = list(parser.links)
    for obj in hydration_objects(text):
        for _, _, value in walk(obj):
            if isinstance(value, str) and ("/" + state + "/") in value.lower():
                strings.append(value)
    for layer in decode_layers(text, rounds=3):
        strings.extend(re.findall(r'(?:https?://locations\.7brew\.com)?/' + state + r'/[a-z0-9%._~-]+(?:/[a-z0-9%._~-]+)?', layer, re.I))

    for raw in strings:
        absolute = urljoin(page_url, html_lib.unescape(raw).replace(r"\/", "/"))
        parsed = urlparse(absolute)
        if parsed.netloc.lower() != "locations.7brew.com":
            continue
        parts = [part for part in parsed.path.split("/") if part]
        if not parts or parts[0].lower() != state:
            continue
        clean = urlunparse(("https", "locations.7brew.com", "/" + "/".join(parts), "", "", ""))
        if len(parts) == 2:
            city_urls.add(clean)
        elif len(parts) == 3:
            location_urls.add(clean)
    return location_urls, city_urls


def discover_locations() -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    found: dict[str, set[str]] = {state: set() for state in STATES}
    audit: list[dict[str, Any]] = []
    for state in STATES:
        root = f"{BASE}/{state}"
        result = fetch(root)
        text = result.body.decode("utf-8", errors="replace")
        locations, cities = directory_links(text, result.final_url, state)
        found[state].update(locations)
        audit.append({"url": root, "status": result.status, "bytes": len(result.body), "location_links": len(locations), "city_links": len(cities), "error": result.error})
        print(f"directory {state.upper()}: status={result.status} locations={len(locations)} cities={len(cities)}", flush=True)
        for idx, city_url in enumerate(sorted(cities), 1):
            city = fetch(city_url)
            city_text = city.body.decode("utf-8", errors="replace")
            child_locations, _ = directory_links(city_text, city.final_url, state)
            found[state].update(child_locations)
            audit.append({"url": city_url, "status": city.status, "bytes": len(city.body), "location_links": len(child_locations), "city_links": 0, "error": city.error})
            print(f"  {state.upper()} city {idx:02d}/{len(cities):02d}: +{len(child_locations)} {city_url}", flush=True)
            time.sleep(0.15)
    return {state: sorted(urls) for state, urls in found.items()}, audit


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    urls_by_state, directory_audit = discover_locations()
    all_urls = [(state, url) for state in STATES for url in urls_by_state[state]]
    print("discovered", {state: len(urls_by_state[state]) for state in STATES}, "total", len(all_urls), flush=True)

    rows: list[dict[str, Any]] = []
    candidate_audit: list[dict[str, Any]] = []
    for index, (state, url) in enumerate(all_urls, 1):
        result = fetch(url)
        text = result.body.decode("utf-8", errors="replace")
        objects = hydration_objects(text)
        fields = extract_custom_fields(objects, text)
        path_parts = [part for part in urlparse(url).path.split("/") if part]
        city = path_parts[1] if len(path_parts) > 1 else ""
        slug = path_parts[2] if len(path_parts) > 2 else ""
        row = {
            "state": state.upper(),
            "city_slug": city,
            "address_slug": slug,
            "locator_url": url,
            "http_status": result.status,
            "final_url": result.final_url,
            "bytes": len(result.body),
            "sha256": hashlib.sha256(result.body).hexdigest() if result.body else "",
            "hydration_object_count": len(objects),
            "entity_ids": "|".join(fields["entity_ids"]),
            "yext_c_openDate_raw": fields["c_openDate_raw"],
            "yext_c_openDate": fields["c_openDate"],
            "yext_c_openDate_path": fields["c_openDate_path"],
            "yext_c_openDate_source": fields["c_openDate_source"],
            "yext_c_openDate_all": "|".join(fields["c_openDate_all_dates"]),
            "yext_c_openDate_candidate_count": fields["c_openDate_candidate_count"],
            "yext_c_approvalDate_raw": fields["c_approvalDate_raw"],
            "yext_c_approvalDate": fields["c_approvalDate"],
            "yext_c_approvalDate_path": fields["c_approvalDate_path"],
            "yext_c_approvalDate_source": fields["c_approvalDate_source"],
            "yext_c_approvalDate_all": "|".join(fields["c_approvalDate_all_dates"]),
            "yext_c_approvalDate_candidate_count": fields["c_approvalDate_candidate_count"],
            "parse_status": "FOUND" if fields["c_openDate"] else ("FIELD_PRESENT_NO_DATE" if fields["c_openDate_candidate_count"] else "FIELD_NOT_FOUND"),
            "error": result.error,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        rows.append(row)
        candidate_audit.append({
            "locator_url": url,
            "open_candidates": fields["c_openDate_candidates"],
            "approval_candidates": fields["c_approvalDate_candidates"],
        })
        print(
            f"{index:03d}/{len(all_urls):03d} {state.upper()} {city}/{slug}: "
            f"status={result.status} payloads={len(objects)} open={fields['c_openDate'] or '-'} "
            f"approval={fields['c_approvalDate'] or '-'}",
            flush=True,
        )
        time.sleep(0.20)

    csv_fields = [
        "state", "city_slug", "address_slug", "locator_url", "http_status", "final_url", "bytes", "sha256",
        "hydration_object_count", "entity_ids", "yext_c_openDate_raw", "yext_c_openDate", "yext_c_openDate_path",
        "yext_c_openDate_source", "yext_c_openDate_all", "yext_c_openDate_candidate_count",
        "yext_c_approvalDate_raw", "yext_c_approvalDate", "yext_c_approvalDate_path", "yext_c_approvalDate_source",
        "yext_c_approvalDate_all", "yext_c_approvalDate_candidate_count", "parse_status", "error", "retrieved_at_utc",
    ]
    write_csv(OUTPUT / "7brew_al_fl_yext_dates.csv", rows, csv_fields)
    write_csv(OUTPUT / "7brew_al_fl_yext_unresolved.csv", [r for r in rows if not r["yext_c_openDate"]], csv_fields)
    (OUTPUT / "7brew_al_fl_yext_candidates.jsonl").write_text(
        "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in candidate_audit), encoding="utf-8"
    )
    summary = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "expected": EXPECTED,
        "discovered": {state: len(urls_by_state[state]) for state in STATES},
        "total_rows": len(rows),
        "http_200": sum(r["http_status"] == 200 for r in rows),
        "open_dates_found": sum(bool(r["yext_c_openDate"]) for r in rows),
        "approval_dates_found": sum(bool(r["yext_c_approvalDate"]) for r in rows),
        "parse_status_counts": {status: sum(r["parse_status"] == status for r in rows) for status in sorted({r["parse_status"] for r in rows})},
        "directory_audit": directory_audit,
    }
    (OUTPUT / "7brew_al_fl_yext_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
