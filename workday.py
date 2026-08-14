#!/usr/bin/env python3
"""
workday.py — pull jobs from every Workday tenant discovered by sniff.py.

Workday is the biggest single gap in the ATS coverage: BP, Shell, Vitol, Trafigura,
Goldman, Macquarie and most banks are on it. There's no maintained Python library
for it (the best repo on GitHub has 17 stars), so this is hand-rolled against the
documented cxs endpoint.

    python sniff.py          # first — finds the tenants
    python workday.py        # then this

Feeds the same jobs.db as scrape.py and boards.py.
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timezone

import http_client
import yaml

import store
from scrape import build_filter, db_init

TIMEOUT = 25
PAGE = 20
UA = {"User-Agent": "Mozilla/5.0 (compatible; job-registry/1.0)",
      "Content-Type": "application/json", "Accept": "application/json"}


def fetch_tenant(session, row, search_text="", max_pages=40):
    """Page through one Workday board. Returns normalised job dicts."""
    tenant, dc, site = row["tenant"], row["dc"], row["site"]
    locale = row.get("locale") or ""
    api = f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    prefix = f"https://{tenant}.{dc}.myworkdayjobs.com" + (f"/{locale}" if locale else "") + f"/{site}"

    # Why the walk stopped matters as much as what it collected. Dozens of
    # tenants returned exactly 40 — two pages — in one run, including BP, Shell
    # and Citi, which do not have forty openings between them. Stopping on a
    # refused third page looked identical to having read everything.
    jobs, offset, stopped = [], 0, ""
    for page in range(max_pages):
        body = {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": search_text}
        r = http_client.post_json(api, body, sess=session)
        if r is None:
            stopped = f"no response at page {page + 1}"
            break
        if r.status_code != 200:
            stopped = f"http {r.status_code} at page {page + 1}"
            break
        data = http_client.json_of(r)
        if data is None:
            stopped = f"unparseable response at page {page + 1}"
            break

        postings = data.get("jobPostings") or []
        for p in postings:
            path = p.get("externalPath") or ""
            jobs.append({
                "company": row["name"],
                "title": (p.get("title") or "").strip(),
                "location": (p.get("locationsText") or "").strip(),
                "url": prefix + path if path else prefix,
                "source": "workday",
                "posted": (p.get("postedOn") or "").strip(),
            })

        total = data.get("total", 0)
        offset += PAGE
        if not postings or offset >= total:
            break
        if page == max_pages - 1:
            stopped = f"hit the {max_pages}-page ceiling with {total} advertised"
        time.sleep(0.4)
    return jobs, stopped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--search", default="", help="server-side keyword filter, e.g. analyst")
    ap.add_argument("--src", default="sniffed.csv")
    args = ap.parse_args()

    rows = [r for r in csv.DictReader(open(args.src, encoding="utf-8")) if r["ats"] == "workday"]
    if not rows:
        print("no Workday tenants in sniffed.csv — run sniff.py first")
        return

    cfg = yaml.safe_load(open("config.yaml", encoding="utf-8"))
    keep = build_filter(cfg)
    con = db_init()
    session = http_client.session()

    raw, truncated = [], []
    for i, row in enumerate(rows, 1):
        got, stopped = fetch_tenant(session, row, args.search)
        raw += got
        if stopped:
            truncated.append(f"{row['name']} ({len(got)} rows, {stopped})")
        print(f"[{i}/{len(rows)}] {row['name']:<34} {len(got):>4} raw"
              f"{'  ! ' + stopped if stopped else ''}")

    if truncated:
        print(f"\n{len(truncated)} of {len(rows)} tenants stopped early — these are "
              f"under-reported, not empty:")
        for t in truncated[:15]:
            print(f"    {t}")
        if len(truncated) > 15:
            print(f"    ... and {len(truncated) - 15} more")

    hits = [j for j in raw if keep(j)]
    new = store.save_new(con, hits)

    store.report(new, len(raw), len(hits))


if __name__ == "__main__":
    main()
