#!/usr/bin/env python3
"""
feeds.py — two more sources, both official and both free.

1. REED — reed.co.uk's jobseeker API. Free key at reed.co.uk/developers.
   Biggest UK board by volume; a lot of commodity/trading roles are posted
   through recruiters who list on Reed and nowhere else. Basic auth, API key
   as username with an empty password.

       export REED_API_KEY=...
       python feeds.py --reed

2. BULLHORN — the specialist commodity recruiters (HC Group, Proco, Mondrian,
   Selby Jennings, Commodity Appointments) run their job portals on Bullhorn,
   which exposes a public read-only REST API for published jobs. sniff.py pulls
   the cluster + corp token straight out of the portal HTML, so no keys needed.

       python sniff.py          # first — finds the bullhorn rows
       python feeds.py --bullhorn

   This matters more than it sounds: roles at the 5-30 person shops usually
   never appear anywhere except a recruiter's portal.

       python feeds.py --all
"""

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timezone

import http_client
import yaml

import store
from scrape import build_filter, db_init

TIMEOUT = 25
UA = {"User-Agent": "Mozilla/5.0 (compatible; job-registry/1.0)"}

REED_SEARCH = "https://www.reed.co.uk/api/1.0/search"
REED_QUERIES = [
    "commodity analyst", "commodities trading", "trading analyst", "market analyst energy",
    "power trading analyst", "gas analyst", "quantitative analyst", "quantitative researcher",
    "data scientist trading", "energy analyst", "junior trader", "carbon analyst",
    "LNG analyst", "freight analyst", "research analyst commodities",
]

BULLHORN_FIELDS = "id,title,address,employmentType,dateLastPublished,publishedCategory"


def from_reed():
    key = os.getenv("REED_API_KEY")
    if not key:
        print("REED_API_KEY not set — get a free one at reed.co.uk/developers", file=sys.stderr)
        return []
    jobs = []
    for q in REED_QUERIES:
        for skip in (0, 100):
            r = http_client.get(REED_SEARCH, auth=(key, ""),
                                params={"keywords": q, "locationName": "London",
                                        "distanceFromLocation": 25,
                                        "resultsToTake": 100, "resultsToSkip": skip})
            data = http_client.json_of(r)
            if data is None:
                if r is not None and r.status_code == 401:
                    print("  ! reed: 401 — check REED_API_KEY", file=sys.stderr)
                break
            results = data.get("results", [])
            for j in results:
                jobs.append({
                    "company": j.get("employerName") or "",
                    "title": j.get("jobTitle") or "",
                    "location": j.get("locationName") or "",
                    "url": j.get("jobUrl") or "",
                    "source": "reed",
                    "posted": j.get("date") or "",
                    "salary_min": j.get("minimumSalary"),
                    "salary_max": j.get("maximumSalary"),
                    "currency": j.get("currency") or "GBP",
                })
            if len(results) < 100:
                break
            time.sleep(0.5)
        print(f"  reed: {q}")
    return jobs


def from_bullhorn(src="sniffed.csv"):
    if not os.path.exists(src):
        print(f"{src} missing — run sniff.py first", file=sys.stderr)
        return []
    rows = [r for r in csv.DictReader(open(src)) if r["ats"] == "bullhorn"]
    if not rows:
        print("no Bullhorn portals found by sniff.py")
        return []

    jobs = []
    for row in rows:
        cls, token = row.get("tenant") or "", row["token"]
        base = f"https://public-rest{cls}.bullhornstaffing.com/rest-services/{token}"
        start, got = 0, 0
        while start < 500:
            r = http_client.get(f"{base}/search/JobOrder",
                                params={"fields": BULLHORN_FIELDS, "query": "isOpen:true",
                                        "count": 100, "start": start,
                                        "sort": "-dateLastPublished"})
            data = http_client.json_of(r)
            if data is None:
                break
            batch = data.get("data") or []
            for j in batch:
                addr = j.get("address") or {}
                jobs.append({
                    "company": row["name"],
                    "title": j.get("title") or "",
                    "location": f"{addr.get('city','')} {addr.get('countryName','')}".strip(),
                    "url": f"{row.get('found_on','').rstrip('/')}/#/job/{j.get('id','')}",
                    "source": "bullhorn",
                    "posted": str(j.get("dateLastPublished") or ""),
                })
                got += 1
            if len(batch) < 100:
                break
            start += 100
            time.sleep(0.4)
        print(f"  {row['name']:<32} {got:>4} raw")
    return jobs


def save(jobs, cfg):
    # deliberately not named store() — that would shadow the store module below
    keep = build_filter(cfg)
    con = db_init()
    hits = [j for j in jobs if keep(j)]
    new = store.save_new(con, hits)
    store.report(new, len(jobs), len(hits))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reed", action="store_true")
    ap.add_argument("--bullhorn", action="store_true")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()
    if not (args.reed or args.bullhorn or args.all):
        args.all = True

    cfg = yaml.safe_load(open("config.yaml"))
    jobs = []
    if args.reed or args.all:
        jobs += from_reed()
    if args.bullhorn or args.all:
        jobs += from_bullhorn()
    save(jobs, cfg)


if __name__ == "__main__":
    main()
