#!/usr/bin/env python3
"""Search and preserve supplemental opening-date evidence for 36 AL/FL 7 Brew pages.

The script:
1. Generates multiple targeted queries per location.
2. Searches Bing web, Bing News RSS, Google News RSS, and DuckDuckGo HTML.
3. Scores and de-duplicates candidate sources.
4. Fetches the strongest accessible pages and extracts publication metadata and
   opening-related sentences/date expressions.
5. Saves every search result, top candidates, page evidence, and a 36-row
   location summary for human review.

No candidate-site model scores or labels are consumed.
"""
from __future__ import annotations

import csv
import hashlib
import html
import json
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
SEED = ROOT / "data/7brew_field_absent_36.csv"
OUT = ROOT / "research_outputs"
RAW = OUT / "raw_candidate_pages"

STATE_NAMES = {"AL": "Alabama", "FL": "Florida"}
UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
]
EVENT_WORDS = (
    "grand opening", "soft opening", "now open", "opened", "opening", "ribbon cutting",
    "ribbon-cutting", "officially open", "doors open", "first day", "swag day",
    "community hours", "friends and family"
)
MONTH_RE = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Sept(?:ember)?|Oct(?:ober)?|"
    r"Nov(?:ember)?|Dec(?:ember)?)"
)
DATE_PATTERNS = [
    re.compile(rf"\b{MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,\s*|\s+)\d{{4}}\b", re.I),
    re.compile(rf"\b{MONTH_RE}\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.I),
    re.compile(rf"\b{MONTH_RE}\s+\d{{4}}\b", re.I),
    re.compile(r"\b20\d{2}-\d{2}-\d{2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/20\d{2}\b"),
]
BLOCKED_DOMAINS = {
    "locations.7brew.com", "7brew.com", "wikipedia.org", "linkedin.com",
    "mapquest.com", "yellowpages.com", "loc8nearme.com", "restaurantji.com",
}
SOCIAL_DOMAINS = {
    "facebook.com", "instagram.com", "tiktok.com", "x.com", "twitter.com",
}
OUTLET_HINTS = {
    "Axios": ["axios.com"],
    "OA News": ["oanow.com", "Opelika-Auburn News"],
    "Shelby County Reporter": ["shelbycountyreporter.com"],
    "Gulf Coast Media": ["gulfcoastmedia.com"],
    "WKRG": ["wkrg.com"],
    "Hville Blast": ["hvilleblast.com"],
    "Bham Now": ["bhamnow.com"],
    "Get The Coast": ["getthecoast.com"],
    "PNJ": ["pnj.com", "Pensacola News Journal"],
    "Hometown News": ["hometownnewsvolusia.com", "Hometown News"],
    "Space Coast Daily": ["spacecoastdaily.com"],
}
SOURCE_PRIORITY = {
    "government": 8,
    "chamber": 7,
    "local_news": 6,
    "official_brand_or_franchise": 6,
    "social": 4,
    "review_or_directory": 2,
    "other": 1,
}

session = requests.Session()
session.headers.update({
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})


def rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, data: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = fields or (list(data[0].keys()) if data else [])
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(data)


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", html.unescape(text or "").lower()).strip()


def domain(url: str) -> str:
    try:
        return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.")
    except Exception:
        return ""


def root_domain(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def request(url: str, *, params: dict[str, str] | None = None, method: str = "GET",
            data: dict[str, str] | None = None, timeout: int = 25) -> requests.Response:
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            headers = {"User-Agent": random.choice(UA_POOL)}
            response = session.request(
                method, url, params=params, data=data, headers=headers,
                timeout=timeout, allow_redirects=True,
            )
            if response.status_code in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"HTTP {response.status_code}")
            return response
        except Exception as exc:
            last_error = exc
            time.sleep((attempt + 1) * 1.5 + random.random())
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def clean_url(url: str) -> str:
    url = html.unescape(url or "").strip()
    if url.startswith("//"):
        url = "https:" + url
    parsed = urllib.parse.urlparse(url)
    if "duckduckgo.com" in parsed.netloc and parsed.path.startswith("/l/"):
        target = urllib.parse.parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            url = urllib.parse.unquote(target)
    parsed = urllib.parse.urlparse(url)
    query = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    query = [
        (k, v) for k, v in query
        if k.lower() not in {"utm_source", "utm_medium", "utm_campaign", "utm_term",
                             "utm_content", "gclid", "fbclid", "ocid", "cvid"}
    ]
    return urllib.parse.urlunparse((
        parsed.scheme or "https", parsed.netloc, parsed.path.rstrip("/"),
        "", urllib.parse.urlencode(query), ""
    ))


def source_type(url: str, title: str = "", snippet: str = "") -> str:
    host = root_domain(domain(url))
    text = norm(f"{title} {snippet}")
    if host.endswith(".gov") or host.endswith(".us") or any(x in host for x in ("cityof", "county")):
        return "government"
    if "chamber" in host or "chamber" in text or "ribbon cutting" in text:
        return "chamber"
    if host in SOCIAL_DOMAINS or any(host.endswith("." + x) for x in SOCIAL_DOMAINS):
        return "social"
    if any(x in host for x in ("yelp", "roadtrippers", "tripadvisor", "google.com/maps",
                               "restaurantguru", "foursquare")):
        return "review_or_directory"
    if any(x in host for x in (
        "news", "daily", "times", "journal", "reporter", "herald", "observer", "post",
        "axios", "blast", "coast", "wkrg", "wctv", "wtvy", "wesh", "fox", "cbs", "nbc",
        "abc", "hometown", "spacecoast", "pnj", "gulfcoastmedia", "oanow", "bhamnow"
    )):
        return "local_news"
    if "7brew" in host or "7 brew" in text:
        return "official_brand_or_franchise"
    return "other"


def outlet_terms(evidence: str) -> list[str]:
    found: list[str] = []
    for label, aliases in OUTLET_HINTS.items():
        if label.lower() in evidence.lower():
            found.extend(aliases)
    return found


def query_set(row: dict[str, str]) -> list[str]:
    state_name = STATE_NAMES[row["state"]]
    city = row["city"]
    address = row["address"]
    years = sorted(set(re.findall(r"20\d{2}", row["original_opened_summary"] + " " +
                                  row["original_evidence_summary"])))
    year_text = " ".join(years)
    queries = [
        f'"7 Brew" "{city}" "{state_name}" opening {year_text}'.strip(),
        f'"7 Brew" "{address}" "{city}"',
        f'"7 Brew" "{city}" ("grand opening" OR "soft opening" OR "ribbon cutting" OR "now open")',
    ]
    hints = outlet_terms(row["original_evidence_summary"])
    if hints:
        queries.append(f'"7 Brew" "{city}" ' + " OR ".join(f'"{h}"' for h in hints))
    else:
        queries.append(f'"7 Brew Coffee" "{city}" "{state_name}" {year_text}'.strip())
    return list(dict.fromkeys(queries))


def parse_bing_html(query: str) -> list[dict[str, str]]:
    response = request("https://www.bing.com/search", params={"q": query, "count": "20", "setlang": "en-US"})
    soup = BeautifulSoup(response.text, "lxml")
    results: list[dict[str, str]] = []
    for rank, item in enumerate(soup.select("li.b_algo"), 1):
        anchor = item.select_one("h2 a")
        if not anchor:
            continue
        snippet_node = item.select_one(".b_caption p") or item.select_one("p")
        results.append({
            "engine": "bing_web",
            "rank": str(rank),
            "title": anchor.get_text(" ", strip=True),
            "url": clean_url(anchor.get("href", "")),
            "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
            "published_hint": "",
        })
    return results


def parse_duckduckgo(query: str) -> list[dict[str, str]]:
    response = request(
        "https://html.duckduckgo.com/html/",
        method="POST",
        data={"q": query, "kl": "us-en"},
    )
    soup = BeautifulSoup(response.text, "lxml")
    results: list[dict[str, str]] = []
    for rank, item in enumerate(soup.select(".result"), 1):
        anchor = item.select_one(".result__a")
        if not anchor:
            continue
        snippet_node = item.select_one(".result__snippet")
        results.append({
            "engine": "duckduckgo",
            "rank": str(rank),
            "title": anchor.get_text(" ", strip=True),
            "url": clean_url(anchor.get("href", "")),
            "snippet": snippet_node.get_text(" ", strip=True) if snippet_node else "",
            "published_hint": "",
        })
    return results


def parse_rss(query: str, engine: str) -> list[dict[str, str]]:
    if engine == "bing_news":
        url = "https://www.bing.com/news/search"
        params = {"q": query, "format": "rss", "setlang": "en-US"}
    else:
        url = "https://news.google.com/rss/search"
        params = {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"}
    response = request(url, params=params)
    results: list[dict[str, str]] = []
    try:
        root = ET.fromstring(response.content)
    except ET.ParseError:
        return results
    for rank, item in enumerate(root.findall(".//item"), 1):
        title = (item.findtext("title") or "").strip()
        link = clean_url((item.findtext("link") or "").strip())
        description = BeautifulSoup(item.findtext("description") or "", "lxml").get_text(" ", strip=True)
        pub = (item.findtext("pubDate") or "").strip()
        results.append({
            "engine": engine,
            "rank": str(rank),
            "title": title,
            "url": link,
            "snippet": description,
            "published_hint": pub,
        })
    return results


def search_all(query: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for engine, fn in (
        ("bing_web", lambda: parse_bing_html(query)),
        ("duckduckgo", lambda: parse_duckduckgo(query)),
        ("bing_news", lambda: parse_rss(query, "bing_news")),
        ("google_news", lambda: parse_rss(query, "google_news")),
    ):
        try:
            output.extend(fn())
        except Exception as exc:
            output.append({
                "engine": engine,
                "rank": "",
                "title": "",
                "url": "",
                "snippet": "",
                "published_hint": "",
                "error": f"{type(exc).__name__}: {exc}",
            })
        time.sleep(0.4 + random.random() * 0.35)
    return output


def score_result(row: dict[str, str], result: dict[str, str]) -> int:
    text = norm(f"{result.get('title', '')} {result.get('snippet', '')} {result.get('url', '')}")
    host = root_domain(domain(result.get("url", "")))
    score = 0
    if "7 brew" in text or "7brew" in text:
        score += 8
    if norm(row["city"]) in text:
        score += 7
    state_name = norm(STATE_NAMES[row["state"]])
    if state_name in text or f" {row['state'].lower()} " in f" {text} ":
        score += 2
    address_number = re.match(r"\d+", row["address"])
    if address_number and address_number.group() in text:
        score += 5
    street_tokens = [
        t for t in norm(row["address"]).split()
        if len(t) > 3 and not t.isdigit() and t not in {"street", "road", "drive", "boulevard",
                                                        "parkway", "highway", "north", "south",
                                                        "east", "west", "northeast", "southeast"}
    ]
    score += min(4, sum(1 for token in street_tokens if token in text))
    if any(word in text for word in EVENT_WORDS):
        score += 5
    for year in re.findall(r"20\d{2}", row["original_opened_summary"] + " " +
                           row["original_evidence_summary"]):
        if year in text:
            score += 2
    hints = outlet_terms(row["original_evidence_summary"])
    for hint in hints:
        if norm(hint) in text or norm(hint) in norm(host):
            score += 3
    stype = source_type(result.get("url", ""), result.get("title", ""), result.get("snippet", ""))
    score += SOURCE_PRIORITY.get(stype, 0)
    if host in BLOCKED_DOMAINS or any(host.endswith("." + x) for x in BLOCKED_DOMAINS):
        score -= 12
    if result.get("url", "").startswith("https://news.google.com/"):
        score -= 1
    if not result.get("url"):
        score -= 20
    return score


def date_from_meta(soup: BeautifulSoup) -> str:
    candidates: list[str] = []
    for selector, attr in (
        ('meta[property="article:published_time"]', "content"),
        ('meta[name="article:published_time"]', "content"),
        ('meta[name="date"]', "content"),
        ('meta[name="pubdate"]', "content"),
        ('meta[itemprop="datePublished"]', "content"),
        ('time[datetime]', "datetime"),
    ):
        for node in soup.select(selector):
            value = (node.get(attr) or "").strip()
            if value:
                candidates.append(value)
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            obj = json.loads(script.get_text(strip=True))
        except Exception:
            continue
        objs = obj if isinstance(obj, list) else [obj]
        for item in objs:
            if isinstance(item, dict):
                for key in ("datePublished", "dateCreated", "uploadDate"):
                    if item.get(key):
                        candidates.append(str(item[key]))
    for value in candidates:
        match = re.search(r"\b20\d{2}-\d{2}-\d{2}\b", value)
        if match:
            return match.group()
        try:
            parsed = parsedate_to_datetime(value)
            return parsed.date().isoformat()
        except Exception:
            pass
    return ""


def extract_event_sentences(text: str, row: dict[str, str]) -> list[str]:
    clean = re.sub(r"\s+", " ", text)
    sentences = re.split(r"(?<=[.!?])\s+|\s{2,}", clean)
    city = norm(row["city"])
    address_match = re.match(r"\d+", row["address"])
    address_number = address_match.group() if address_match else ""
    chosen: list[str] = []
    for sentence in sentences:
        sentence_norm = norm(sentence)
        if len(sentence) < 25 or len(sentence) > 600:
            continue
        event_match = any(word in sentence_norm for word in EVENT_WORDS)
        date_match = any(pattern.search(sentence) for pattern in DATE_PATTERNS)
        location_match = city in sentence_norm or (address_number and address_number in sentence_norm)
        brand_match = "7 brew" in sentence_norm or "7brew" in sentence_norm
        if event_match and (date_match or (location_match and brand_match)):
            chosen.append(sentence.strip())
    return list(dict.fromkeys(chosen))[:12]


def extracted_date_mentions(sentences: list[str]) -> list[str]:
    values: list[str] = []
    for sentence in sentences:
        for pattern in DATE_PATTERNS:
            values.extend(match.group(0) for match in pattern.finditer(sentence))
    return list(dict.fromkeys(values))


def fetch_candidate(url: str, row: dict[str, str]) -> dict[str, Any]:
    record: dict[str, Any] = {
        "url": url, "fetch_status": "", "final_url": "", "http_status": "",
        "content_type": "", "page_sha256": "", "raw_path": "", "page_title": "",
        "publication_date": "", "event_sentences": "", "date_mentions": "",
        "page_text_excerpt": "", "fetch_error": "",
    }
    if not url:
        return record
    host = root_domain(domain(url))
    if url.startswith("https://news.google.com/"):
        record["fetch_status"] = "SEARCH_RESULT_ONLY_GOOGLE_NEWS"
        return record
    if host in SOCIAL_DOMAINS or any(host.endswith("." + x) for x in SOCIAL_DOMAINS):
        record["fetch_status"] = "SOCIAL_SEARCH_RESULT_ONLY"
        return record
    try:
        response = request(url, timeout=30)
        record["http_status"] = str(response.status_code)
        record["final_url"] = clean_url(response.url)
        record["content_type"] = response.headers.get("content-type", "")
        if response.status_code != 200:
            record["fetch_status"] = "HTTP_ERROR"
            return record
        body = response.content[:3_000_000]
        digest = hashlib.sha256(body).hexdigest()
        RAW.mkdir(parents=True, exist_ok=True)
        suffix = ".html" if "html" in record["content_type"].lower() else ".bin"
        raw_path = RAW / f"{digest}{suffix}"
        if not raw_path.exists():
            raw_path.write_bytes(body)
        record["page_sha256"] = digest
        record["raw_path"] = str(raw_path.relative_to(ROOT))
        if "html" not in record["content_type"].lower():
            record["fetch_status"] = "NON_HTML"
            return record
        soup = BeautifulSoup(body, "lxml")
        record["page_title"] = (
            (soup.title.get_text(" ", strip=True) if soup.title else "")
            or ((soup.select_one('meta[property="og:title"]') or {}).get("content", ""))
        )
        record["publication_date"] = date_from_meta(soup)
        for tag in soup(["script", "style", "noscript", "svg", "nav", "footer"]):
            tag.decompose()
        text = soup.get_text(" ", strip=True)
        sentences = extract_event_sentences(text, row)
        record["event_sentences"] = " || ".join(sentences)
        record["date_mentions"] = " | ".join(extracted_date_mentions(sentences))
        record["page_text_excerpt"] = text[:2500]
        record["fetch_status"] = "FETCHED_HTML"
    except Exception as exc:
        record["fetch_status"] = "FETCH_ERROR"
        record["fetch_error"] = f"{type(exc).__name__}: {exc}"
    return record


def main() -> None:
    locations = rows(SEED)
    OUT.mkdir(parents=True, exist_ok=True)
    search_rows: list[dict[str, Any]] = []
    top_candidates: list[dict[str, Any]] = []
    page_rows: list[dict[str, Any]] = []
    summaries: list[dict[str, Any]] = []

    for index, row in enumerate(locations, 1):
        location_id = f"{row['state']}_{row['city']}_{row['address']}"
        print(f"[{index:02d}/36] searching {row['state']} {row['city']} {row['address']}", flush=True)
        dedup: dict[str, dict[str, Any]] = {}
        for query_number, query in enumerate(query_set(row), 1):
            results = search_all(query)
            for result in results:
                record = {
                    "location_id": location_id,
                    "state": row["state"],
                    "city": row["city"],
                    "address": row["address"],
                    "query_number": query_number,
                    "query": query,
                    **result,
                }
                record["source_domain"] = root_domain(domain(record.get("url", "")))
                record["source_type"] = source_type(
                    record.get("url", ""), record.get("title", ""), record.get("snippet", "")
                )
                record["score"] = score_result(row, record)
                search_rows.append(record)
                url = record.get("url", "")
                if not url:
                    continue
                previous = dedup.get(url)
                if previous is None or int(record["score"]) > int(previous["score"]):
                    dedup[url] = record

        ranked = sorted(
            dedup.values(),
            key=lambda item: (
                int(item["score"]),
                SOURCE_PRIORITY.get(item["source_type"], 0),
                -int(item.get("rank") or 999),
            ),
            reverse=True,
        )
        relevant = [item for item in ranked if int(item["score"]) >= 14][:12]
        if not relevant:
            relevant = ranked[:8]

        fetched_for_location: list[dict[str, Any]] = []
        for rank, candidate in enumerate(relevant, 1):
            candidate = dict(candidate)
            candidate["candidate_rank"] = rank
            top_candidates.append(candidate)
            page = fetch_candidate(candidate["url"], row)
            combined = {
                "location_id": location_id,
                "state": row["state"],
                "city": row["city"],
                "address": row["address"],
                "candidate_rank": rank,
                "search_score": candidate["score"],
                "search_engine": candidate["engine"],
                "search_title": candidate["title"],
                "search_snippet": candidate["snippet"],
                "search_published_hint": candidate["published_hint"],
                "source_domain": candidate["source_domain"],
                "source_type": candidate["source_type"],
                **page,
            }
            page_rows.append(combined)
            fetched_for_location.append(combined)
            time.sleep(0.3 + random.random() * 0.3)

        page_ranked = sorted(
            fetched_for_location,
            key=lambda item: (
                bool(item["event_sentences"]),
                bool(item["publication_date"]),
                int(item["search_score"]),
                SOURCE_PRIORITY.get(item["source_type"], 0),
            ),
            reverse=True,
        )
        best = page_ranked[0] if page_ranked else {}
        second = page_ranked[1] if len(page_ranked) > 1 else {}
        evidence_count = sum(
            bool(item.get("event_sentences") or item.get("search_snippet"))
            for item in page_ranked
        )
        summaries.append({
            "location_id": location_id,
            "state": row["state"],
            "city": row["city"],
            "address": row["address"],
            "zip": row["zip"],
            "stand_num": row["stand_num"],
            "prior_opened_summary": row["original_opened_summary"],
            "prior_evidence_summary": row["original_evidence_summary"],
            "queries_run": len(query_set(row)),
            "unique_search_results": len(dedup),
            "candidate_pages_reviewed": len(page_ranked),
            "candidate_evidence_count": evidence_count,
            "best_source_type": best.get("source_type", ""),
            "best_source_domain": best.get("source_domain", ""),
            "best_title": best.get("page_title") or best.get("search_title", ""),
            "best_url": best.get("final_url") or best.get("url", ""),
            "best_publication_date": best.get("publication_date") or best.get("search_published_hint", ""),
            "best_event_sentences": best.get("event_sentences", ""),
            "best_date_mentions": best.get("date_mentions", ""),
            "best_search_snippet": best.get("search_snippet", ""),
            "best_page_sha256": best.get("page_sha256", ""),
            "second_title": second.get("page_title") or second.get("search_title", ""),
            "second_url": second.get("final_url") or second.get("url", ""),
            "second_publication_date": second.get("publication_date") or second.get("search_published_hint", ""),
            "second_event_sentences": second.get("event_sentences", ""),
            "second_date_mentions": second.get("date_mentions", ""),
            "locator_url": row["locator_url"],
        })

    write_csv(OUT / "search_results_all.csv", search_rows)
    write_csv(OUT / "top_candidates_by_location.csv", top_candidates)
    write_csv(OUT / "candidate_page_evidence.csv", page_rows)
    write_csv(OUT / "location_research_summary_36.csv", summaries)

    audit = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "locations": len(locations),
        "search_results": len(search_rows),
        "top_candidates": len(top_candidates),
        "pages_attempted": len(page_rows),
        "pages_fetched_html": sum(r["fetch_status"] == "FETCHED_HTML" for r in page_rows),
        "locations_with_event_sentences": sum(bool(r["best_event_sentences"]) for r in summaries),
        "locations_with_any_candidate": sum(bool(r["best_url"]) for r in summaries),
        "engines": sorted(set(r["engine"] for r in search_rows if r.get("engine"))),
    }
    (OUT / "audit_summary.json").write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(json.dumps(audit, indent=2))


if __name__ == "__main__":
    main()
