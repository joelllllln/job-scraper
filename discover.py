#!/usr/bin/env python3
"""
discover.py — find each firm's real ATS endpoint.

Nobody publishes a list of which company uses which applicant tracking system,
so we brute-force it: for every firm, generate plausible tokens from the name
and domain, probe each ATS provider's public JSON API, and keep whatever
answers with real jobs.

Output: endpoints.csv  (name, category, ats, token, url, n_jobs, checked_at)

Run this weekly-ish. It's slow (a few minutes) but you only need it occasionally.
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import http_client

TIMEOUT = 12
WORKERS = 24
UA = {"User-Agent": "Mozilla/5.0 (compatible; job-registry/1.0)"}

# Public, documented-ish job board APIs. {t} = company token.
PROVIDERS = {
    "greenhouse":     "https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=false",
    "lever":          "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby":          "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "smartrecruiters":"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
    "workable":       "https://apply.workable.com/api/v1/widget/accounts/{t}?details=true",
    "recruitee":      "https://{t}.recruitee.com/api/offers/",
    "teamtailor":     "https://{t}.teamtailor.com/jobs.rss",
    "personio":       "https://{t}.jobs.personio.com/xml",
    "breezy":         "https://{t}.breezy.hr/json",
    "bamboohr":       "https://{t}.bamboohr.com/careers/list",
    # Newer boards, common at the smaller firms that have no Greenhouse. A guess
    # that turns out wrong costs one cheap request and is discarded — nothing is
    # recorded unless the response parses into a non-zero job count.
    "rippling":       "https://api.rippling.com/platform/api/ats/v1/board/{t}/jobs",
    "pinpoint":       "https://{t}.pinpointhq.com/postings.json",
    "comeet":         "https://www.comeet.co/careers-api/2.0/company/{t}/positions",
    "jobvite":        "https://jobs.jobvite.com/api/v1/jobs?companyId={t}",
}
# Workday is deliberately absent: its boards are keyed by tenant + datacentre +
# site and need a POST, so they cannot be guessed. sniff.py reads them instead.

# Shapes vary; these are the keys the newer boards actually use for a job list.
LIST_KEYS = ("data", "jobs", "positions", "results", "items", "postings")


def tokens_for(name: str, domain: str):
    """Candidate company tokens, most likely first."""
    base = domain.split(".")[0]
    slug = re.sub(r"[^a-z0-9]+", "", name.lower())
    hyph = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    first = hyph.split("-")[0]
    out = [base, slug, hyph, first]
    seen, uniq = set(), []
    for t in out:
        if t and t not in seen and len(t) > 2:
            seen.add(t)
            uniq.append(t)
    return uniq


def count_jobs(provider: str, body: str):
    """Return job count if the response looks like a real board, else None."""
    try:
        if provider in ("teamtailor", "personio"):
            n = body.count("<item") + body.count("<position>")
            return n if n else None
        data = json.loads(body)
    except Exception:
        return None

    if provider == "greenhouse":
        return len(data.get("jobs", [])) if isinstance(data, dict) else None
    if provider == "lever":
        return len(data) if isinstance(data, list) else None
    if provider == "ashby":
        return len(data.get("jobs", [])) if isinstance(data, dict) else None
    if provider == "smartrecruiters":
        return data.get("totalFound") if isinstance(data, dict) else None
    if provider == "workable":
        return len(data.get("jobs", [])) if isinstance(data, dict) else None
    if provider == "breezy":
        return len(data) if isinstance(data, list) else None
    if provider == "bamboohr":
        return len(data.get("result", [])) if isinstance(data, dict) else None
    if provider == "recruitee":
        return len(data.get("offers", [])) if isinstance(data, dict) else None

    # newer boards: a bare list, or one list under a predictable key
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for key in LIST_KEYS:
            if isinstance(data.get(key), list):
                return len(data[key])
    return None


def probe(session, firm):
    name, category, domain = firm["name"], firm["category"], firm["domain"]
    for token in tokens_for(name, domain):
        for provider, tmpl in PROVIDERS.items():
            url = tmpl.format(t=token)
            # retries=0 on purpose. Most of these probes are guesses at
            # subdomains that do not exist, and "no such host" is a final answer,
            # not a blip worth three backoffs — which cost 7s each and made a
            # full sweep longer than the stage is allowed to run.
            r = http_client.get(url, sess=session, retries=0)
            if r is None or r.status_code != 200:
                continue
            n = count_jobs(provider, http_client.text_of(r))
            if n is None or n == 0:
                continue
            return {
                "name": name,
                "category": category,
                "ats": provider,
                "token": token,
                "url": url,
                "n_jobs": n,
                "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
    return None


FIELDS = ["name", "category", "ats", "token", "url", "n_jobs", "checked_at"]


def write_endpoints(found):
    with open("endpoints.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(found, key=lambda r: (r["category"], r["name"])))


def write_misses(misses):
    with open("no_ats.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(misses)


def names_in(path, keep=lambda r: True):
    try:
        return {(r.get("name") or "").strip().lower()
                for r in csv.DictReader(open(path, encoding="utf-8")) if keep(r)}
    except OSError:
        return set()


def settled():
    """Firms we already have an answer for, good or bad.

    Which ATS a firm uses changes about never, so re-deriving it every week is
    pure cost — and at several thousand firms it is the single most expensive
    thing the pipeline does. Both the hits and the misses are remembered, so an
    ordinary run only looks at firms that are genuinely new. `--recheck` (or
    deleting the CSVs) forces the full sweep again.
    """
    return (names_in("sniffed.csv", lambda r: (r.get("ats") or "") not in ("", "workday", "bullhorn"))
            | names_in("endpoints.csv")
            | names_in("no_ats.csv"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recheck", action="store_true",
                    help="probe every firm again, including ones already answered")
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv", encoding="utf-8")))
    total = len(firms)
    found = [r for r in csv.DictReader(open("endpoints.csv", encoding="utf-8"))] if os.path.exists("endpoints.csv") else []
    misses = [r for r in csv.DictReader(open("no_ats.csv", encoding="utf-8"))] if os.path.exists("no_ats.csv") else []

    if not args.recheck:
        known = settled()
        firms = [f for f in firms if (f["name"] or "").strip().lower() not in known]
        print(f"{total} firms, {len(known)} already answered -> probing {len(firms)}\n")
    else:
        found, misses = [], []
        print(f"rechecking all {total} firms\n")

    if not firms:
        print("nothing new to probe — use --recheck to sweep everything again")
        return

    session = http_client.session()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(probe, session, f): f for f in firms}
        for i, fut in enumerate(as_completed(futs), 1):
            firm = futs[fut]
            try:
                hit = fut.result()
            except Exception as e:
                print(f"  ! {firm['name']}: {e}", file=sys.stderr)
                hit = None
            if hit:
                found.append(hit)
                print(f"[{i}/{len(firms)}] HIT  {hit['name']:<34} {hit['ats']}/{hit['token']} ({hit['n_jobs']} jobs)")
                # Written as we go. This stage runs under a timeout, and being
                # killed at firm 300 used to throw away all 300 answers.
                write_endpoints(found)
            else:
                misses.append(firm)
                print(f"[{i}/{len(firms)}] ---  {firm['name']}")
                # Misses are an answer too, and remembering them is what stops
                # the next run re-probing thousands of firms with no board.
                if i % 25 == 0:
                    write_misses(misses)

    write_endpoints(found)
    write_misses(misses)

    print(f"\n{len(found)} endpoints -> endpoints.csv")
    print(f"{len(misses)} firms with no public ATS -> no_ats.csv (use links.md / Adzuna for these)")


if __name__ == "__main__":
    main()
