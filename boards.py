#!/usr/bin/env python3
"""
boards.py — board coverage via JobSpy (LinkedIn, Indeed, Glassdoor, Google, ZipRecruiter).

Complements scrape.py: ATS endpoints give you clean per-firm data, this gives you
everything posted by firms that have no scrapable board.

    pip install python-jobspy
    python boards.py                 # all queries, last 7 days
    python boards.py --hours 72      # tighter window
    python boards.py --linkedin-only

Rate limiting matters. LinkedIn will 429 and then temporarily block you if you
hammer it. Defaults here are deliberately slow: one query at a time, a pause
between, modest results_wanted. If you get blocked, add proxies or drop LinkedIn
and lean on Indeed + Google, which are far more tolerant.
"""

import argparse
import sqlite3
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import yaml

from jobspy import scrape_jobs
import store
from scrape import build_filter, db_init

QUERIES = [
    "commodity analyst",
    "commodities trading analyst",
    "market analyst energy",
    "fundamental analyst power gas",
    "trading analyst",
    "junior trader",
    "quantitative analyst commodities",
    "quantitative researcher",
    "data scientist trading",
    "data scientist energy",
    "market data analyst",
    "research analyst commodities",
    "carbon markets analyst",
    "LNG analyst",
    "freight analyst shipping",
    "power trading analyst",
    "energy market analyst",
    "systematic trading analyst",
    # battery storage and flexibility — the fastest-hiring corner of UK power,
    # and it advertises under its own vocabulary rather than "commodities"
    "battery storage analyst",
    "energy flexibility analyst",
    "electricity market analyst",
    "power market modelling",
    "renewable energy analyst",
    "energy trading graduate scheme",
    # the regulatory side, where the REMIT background is the qualification
    "market surveillance analyst",
    "energy regulation analyst",
    "trade surveillance analyst",
    "commodity risk analyst",
    "price reporter commodities",
]

SITES_DEFAULT = ["linkedin", "indeed", "google", "glassdoor"]


def run(sites, hours, per_query, pause):
    frames = []
    for i, q in enumerate(QUERIES, 1):
        print(f"[{i}/{len(QUERIES)}] {q}")
        try:
            df = scrape_jobs(
                site_name=sites,
                search_term=q,
                google_search_term=f"{q} jobs in London since last week",
                location="London, United Kingdom",
                country_indeed="UK",
                results_wanted=per_query,
                hours_old=hours,
                linkedin_fetch_description=False,   # much slower + far more likely to trip limits
                description_format="markdown",
                verbose=0,
            )
            if df is not None and len(df):
                frames.append(df)
                print(f"      {len(df)} rows")
        except Exception as e:
            print(f"      ! {e}", file=sys.stderr)
        time.sleep(pause)

    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True).drop_duplicates(subset=["job_url"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=168, help="how far back (default 7 days)")
    ap.add_argument("--per-query", type=int, default=40)
    ap.add_argument("--pause", type=float, default=6.0, help="seconds between queries")
    ap.add_argument("--linkedin-only", action="store_true")
    ap.add_argument("--no-linkedin", action="store_true", help="use if you've been rate limited")
    args = ap.parse_args()

    sites = SITES_DEFAULT
    if args.linkedin_only:
        sites = ["linkedin"]
    elif args.no_linkedin:
        sites = [s for s in SITES_DEFAULT if s != "linkedin"]

    cfg = yaml.safe_load(open("config.yaml"))
    keep = build_filter(cfg)
    con = db_init()

    df = run(sites, args.hours, args.per_query, args.pause)
    if df.empty:
        print("nothing returned")
        return

    jobs = [{
        "company": str(r.get("company") or ""),
        "title": str(r.get("title") or ""),
        "location": str(r.get("location") or ""),
        "url": str(r.get("job_url") or ""),
        "source": str(r.get("site") or "board"),
        "posted": str(r.get("date_posted") or ""),
    } for _, r in df.iterrows()]

    hits = [j for j in jobs if keep(j)]
    new = store.save_new(con, hits)

    store.report(new, len(jobs), len(hits))


if __name__ == "__main__":
    main()
