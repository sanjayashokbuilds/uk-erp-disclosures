"""Stage 1: download the latest annual report of every UK-listed company and extract the text.

Why not Companies House? Big companies' accounts there are stored as scanned images (no text).
Instead we use filings.xbrl.org, which collects the digital annual reports (ESEF / Inline XBRL)
that UK main-market companies must file with the FCA. These are full web-page versions of the
annual report, so the text is searchable.

Steps:
1. Page through filings.xbrl.org for GB filings with a period end on or after --since.
2. Keep the latest filing per company (identified by LEI) and drop investment trusts and funds.
3. Download each report (.xhtml), strip embedded images, split into pages, save the text to
   data/text/<LEI>.json. Downloads are deleted afterwards.

Safe to stop and re-run: companies that already have a text file are skipped.

Run:  python -m erpmap.fetch --limit 10     (quick test)
      python -m erpmap.fetch                (everything)
"""

import argparse
import csv
import json
import re
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from lxml import html as lxml_html

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
TEXT_DIR = DATA / "text"
LOG = DATA / "fetch_log.csv"
BASE = "https://filings.xbrl.org"
GAP = 0.5  # seconds between requests, to be polite to a free service
MIN_CHARS = 3000

FUND_NAME = re.compile(
    r"\btrust\b|\binvestment(s)? (trust|company)\b|\bVCT\b|\bfunds?\b",
    re.I,
)

session = requests.Session()
session.headers["User-Agent"] = "uk-erp-disclosures (public research project)"
_last = [0.0]


def get(url, **kw):
    for attempt in range(5):
        wait = GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        try:
            r = session.get(url, timeout=180, **kw)
        except requests.RequestException as e:
            print(f"    network error ({e.__class__.__name__}), retrying...")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(10 * (attempt + 1))
            continue
        r.raise_for_status()
        return r
    raise RuntimeError(f"Gave up on {url}")


def list_filings(since):
    """All GB filings with period_end >= since, plus entity names."""
    filt = json.dumps([
        {"name": "country", "op": "eq", "val": "GB"},
        {"name": "period_end", "op": "ge", "val": since},
    ])
    filings, names, page = [], {}, 1
    while True:
        r = get(f"{BASE}/api/filings", params={
            "filter": filt, "include": "entity", "page[size]": 200, "page[number]": page,
        })
        body = r.json()
        for inc in body.get("included", []):
            if inc.get("type") in ("entity", "entities"):
                names[inc["id"]] = inc.get("attributes", {}).get("name", "")
        for f in body.get("data", []):
            a = f.get("attributes", {})
            if a.get("country") != "GB":  # double-check the filter was applied
                continue
            rel = f.get("relationships", {}).get("entity", {})
            lei = (rel.get("data") or {}).get("id") or rel.get("links", {}).get("related", "").rstrip("/").split("/")[-1]
            filings.append({"lei": lei, **a})
        print(f"  page {page}: {len(filings)} GB filings so far")
        if not body.get("links", {}).get("next") or not body.get("data"):
            break
        page += 1
    return filings, names


def entity_name(lei, names):
    if names.get(lei):
        return names[lei]
    r = get(f"{BASE}/api/entities/{lei}")
    name = r.json().get("data", {}).get("attributes", {}).get("name", "") if r else ""
    names[lei] = name
    return name


def latest_per_company(filings):
    best = {}
    for f in filings:
        if not f.get("report_url"):
            continue
        key = f["lei"]
        rank = (f.get("period_end") or "", f.get("processed") or f.get("date_added") or "")
        if key not in best or rank > best[key][0]:
            best[key] = (rank, f)
    return [v[1] for v in best.values()]


def extract_pages(raw):
    raw = re.sub(rb'(src|href)="data:[^"]{200,}"', b'\\1=""', raw)  # drop embedded images
    doc = lxml_html.fromstring(raw)
    for el in list(doc.iter()):
        if isinstance(el.tag, str) and el.tag.lower().endswith("header") and ":" in el.tag:
            el.drop_tree()  # ix:header holds hidden XBRL data, not report text
    for el in doc.xpath("//script|//style"):
        el.drop_tree()
    xp = ('//div[contains(concat(" ", normalize-space(@class), " "), " page ") or '
          'contains(concat(" ", normalize-space(@class), " "), " pf ")]')
    pages = doc.xpath(xp)
    page_set = set(pages)
    top = [p for p in pages if not any(a in page_set for a in p.iterancestors())]
    if len(top) >= 10:
        return [p.text_content() for p in top]
    return [doc.text_content()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only process the first N companies")
    ap.add_argument("--since", default="2024-06-30", help="earliest period end to include")
    args = ap.parse_args()
    TEXT_DIR.mkdir(parents=True, exist_ok=True)

    print("Listing UK filings on filings.xbrl.org...")
    filings, names = list_filings(args.since)
    latest = latest_per_company(filings)
    print(f"{len(latest)} companies with a report since {args.since}")

    todo = []
    for f in latest:
        name = entity_name(f["lei"], names)
        f["name"] = name
        if FUND_NAME.search(name):
            continue
        todo.append(f)
    todo.sort(key=lambda f: f["name"])
    print(f"{len(latest) - len(todo)} investment trusts / funds skipped, {len(todo)} to process")
    if args.limit:
        todo = todo[: args.limit]

    new_log = not LOG.exists()
    with LOG.open("a", newline="") as logf:
        log = csv.writer(logf)
        if new_log:
            log.writerow(["lei", "company_name", "status", "period_end", "pages", "chars", "report_url"])
        for i, f in enumerate(todo, 1):
            out = TEXT_DIR / f"{f['lei']}.json"
            if out.exists():
                continue
            url = urljoin(BASE + "/", f["report_url"].lstrip("/"))
            print(f"[{i}/{len(todo)}] {f['name']} (period end {f.get('period_end')})")
            r = get(url)
            if r is None:
                log.writerow([f["lei"], f["name"], "download_failed", f.get("period_end"), 0, 0, url])
                continue
            try:
                pages = extract_pages(r.content)
            except Exception as e:
                print(f"    couldn't read report: {e}")
                log.writerow([f["lei"], f["name"], "unreadable", f.get("period_end"), 0, 0, url])
                continue
            chars = sum(len(p) for p in pages)
            status = "ok" if chars >= MIN_CHARS else "no_text_layer"
            viewer = urljoin(BASE + "/", (f.get("viewer_url") or f["report_url"]).lstrip("/"))
            out.write_text(json.dumps({
                "company_number": f["lei"],
                "company_name": f["name"],
                "period_end": f.get("period_end", ""),
                "status": status,
                "report_link": viewer,
                "pages": pages,
            }))
            log.writerow([f["lei"], f["name"], status, f.get("period_end"), len(pages), chars, url])
            logf.flush()
            print(f"    {status}: {len(pages)} pages, {chars:,} characters")
    print("\nDone. Text is in data/text/. Next: python -m erpmap.scan")


if __name__ == "__main__":
    main()
