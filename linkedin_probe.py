#!/usr/bin/env python3
"""
linkedin_probe.py — does LinkedIn actually answer from THIS machine?

LinkedIn blocks datacentre IPs. GitHub's runners are blanket-blocked, which is
why the weekly workflow runs with NO_LINKEDIN=1. Whether any other host is
blocked is an empirical question, not something to reason about from first
principles — so this asks, cheaply, and says plainly what came back.

It exists because the failure is silent. JobSpy's LinkedIn scraper returns an
EMPTY DATAFRAME when it is blocked rather than raising, so "LinkedIn is banned
here" and "no London commodity jobs were posted this week" look identical from
the calling code. This separates them by running the same query against a
control site and comparing.

    python linkedin_probe.py            # one query, ~30 seconds
    python linkedin_probe.py --full     # every site, for a full picture

Exit code 0 if LinkedIn returned rows, 1 if it did not. That makes it usable
as a deployment healthcheck, which is the point on Railway.
"""

import argparse
import os
import sys

# A query that is guaranteed to have London results on any working board. If
# THIS returns nothing, the site is blocked — it is not a quiet week.
CONTROL_QUERY = "analyst"
PROBE_QUERY = "trading analyst"


def proxies():
    """Residential proxies, if configured. See boards.py for the format."""
    raw = os.getenv("JOBSPY_PROXIES", "").strip()
    return [p.strip() for p in raw.split(",") if p.strip()] or None


def probe(site, query, hours=336, wanted=10):
    """Returns (rows, error). rows=0 with no error is the silent-block case."""
    from jobspy import scrape_jobs
    try:
        df = scrape_jobs(
            site_name=[site],
            search_term=query,
            google_search_term=f"{query} jobs in London",
            location="London" if site == "glassdoor" else "London, United Kingdom",
            country_indeed="UK",
            results_wanted=wanted,
            hours_old=hours,
            linkedin_fetch_description=False,
            proxies=proxies(),
            verbose=0,
        )
        return (0 if df is None else len(df)), None
    except Exception as e:
        return 0, f"{type(e).__name__}: {str(e)[:160]}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="probe every board, not just LinkedIn")
    args = ap.parse_args()

    where = os.getenv("RAILWAY_ENVIRONMENT_NAME") or os.getenv("GITHUB_ACTIONS") and "github-actions" or "local"
    print(f"probing from: {where}")
    print(f"proxies: {'configured' if proxies() else 'none'}\n")

    sites = ["linkedin", "indeed", "google", "glassdoor"] if args.full else ["linkedin", "indeed"]
    results = {}
    for s in sites:
        n, err = probe(s, PROBE_QUERY)
        if n == 0 and not err:
            # Retry on the broadest possible query before calling it blocked —
            # "trading analyst" genuinely can return nothing on a thin week.
            n, err = probe(s, CONTROL_QUERY)
        results[s] = (n, err)
        state = f"{n} rows" if n else (f"ERROR {err}" if err else "0 rows (no error raised)")
        print(f"  {s:<12} {state}")

    li, li_err = results.get("linkedin", (0, None))
    control = max((n for s, (n, _) in results.items() if s != "linkedin"), default=0)

    print()
    if li > 0:
        print(f"LINKEDIN WORKS HERE — {li} rows. Run the collector from this host.")
        return 0
    if control == 0:
        print("INCONCLUSIVE — every board returned nothing, so this looks like a "
              "network or egress problem on this host, not a LinkedIn block.")
        return 1
    detail = f" ({li_err})" if li_err else " and raised no error, which is what a block looks like"
    print(f"LINKEDIN IS BLOCKED HERE — other boards answered, LinkedIn returned nothing{detail}.")
    print("Fix: set JOBSPY_PROXIES to a residential proxy and re-run. A "
          "datacentre proxy will not help — it is the IP class that is blocked.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
