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
import os
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

LOCATION = "London, United Kingdom"
# Glassdoor's location lookup is a GET with the search term interpolated straight
# into the URL: findPopularLocationAjax.htm?...&term=London, United Kingdom. The
# unescaped comma and space make it a malformed request and Glassdoor answers 400,
# so _get_location returns None and every single query aborts with "location not
# parsed" before it searches for anything. It ran that way for six runs and
# contributed zero rows. A bare city name is what it wants.
SITE_LOCATION = {"glassdoor": "London"}


def proxies():
    """Optional residential proxies, comma separated, from JOBSPY_PROXIES.

        export JOBSPY_PROXIES="user:pass@host:port,user:pass@host2:port"

    Only worth setting for LinkedIn, and only a RESIDENTIAL proxy helps: what
    LinkedIn blocks is the IP class, so routing a datacentre request through
    another datacentre changes nothing. Indeed and Google are tolerant enough
    that they do not need one.
    """
    raw = os.getenv("JOBSPY_PROXIES", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] or None


def run(sites, hours, per_query, pause):
    """One site at a time, counted.

    Previously all four sites went into a single scrape_jobs call and only the
    combined row count was printed. That hid two different failures at once:
    Glassdoor erroring on every query, and LinkedIn — which returns an EMPTY
    FRAME rather than raising when it is blocked — quietly contributing nothing.
    Neither showed up as a failure. Per-site totals are the whole point here.
    """
    frames, per_site = [], {s: 0 for s in sites}
    for site in sites:
        for i, q in enumerate(QUERIES, 1):
            print(f"[{site} {i}/{len(QUERIES)}] {q}")
            try:
                df = scrape_jobs(
                    site_name=[site],
                    search_term=q,
                    google_search_term=f"{q} jobs in London since last week",
                    location=SITE_LOCATION.get(site, LOCATION),
                    country_indeed="UK",
                    results_wanted=per_query,
                    hours_old=hours,
                    linkedin_fetch_description=False,   # much slower + far more likely to trip limits
                    description_format="markdown",
                    proxies=proxies(),
                    verbose=0,
                )
                if df is not None and len(df):
                    frames.append(df)
                    per_site[site] += len(df)
                    print(f"      {len(df)} rows")
            except Exception as e:
                print(f"      ! {site}: {e}", file=sys.stderr)
            time.sleep(pause)

    print("\nper-site raw rows:")
    for s in sites:
        print(f"  {s:<12} {per_site[s]}")
    # An empty site is not a quiet result, it is a broken one: these are all
    # general job boards and every one of them has London finance roles today.
    dead = [s for s in sites if per_site[s] == 0]
    if dead:
        print(f"  !! returned nothing at all: {', '.join(dead)} — blocked, "
              f"rate limited, or the endpoint changed", file=sys.stderr)

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
    # The env var exists because the GitHub workflow used to strip LinkedIn with
    # a sed against the exact text of the boards.py line in weekly.sh. Editing
    # either file would have made that sed match nothing and silently stop
    # applying, and a silent no-op on a rate-limit guard is how you get blocked.
    elif args.no_linkedin or os.getenv("NO_LINKEDIN") == "1":
        sites = [s for s in SITES_DEFAULT if s != "linkedin"]
        print("linkedin: skipped (datacentre IPs are blocked — run this at home "
              "for LinkedIn coverage)")
    print(f"sites: {', '.join(sites)}\n")

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
