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

import csv
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import http_client

TIMEOUT = 12
WORKERS = 12
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
}


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
    return None


def probe(session, firm):
    name, category, domain = firm["name"], firm["category"], firm["domain"]
    for token in tokens_for(name, domain):
        for provider, tmpl in PROVIDERS.items():
            url = tmpl.format(t=token)
            r = http_client.get(url, sess=session)
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


def main():
    firms = list(csv.DictReader(open("firms.csv")))
    found, misses = [], []
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
            else:
                misses.append(firm)
                print(f"[{i}/{len(firms)}] ---  {firm['name']}")

    found.sort(key=lambda r: (r["category"], r["name"]))
    with open("endpoints.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "ats", "token", "url", "n_jobs", "checked_at"])
        w.writeheader()
        w.writerows(found)

    with open("no_ats.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain"])
        w.writeheader()
        w.writerows(misses)

    print(f"\n{len(found)} endpoints -> endpoints.csv")
    print(f"{len(misses)} firms with no public ATS -> no_ats.csv (use links.md / Adzuna for these)")


if __name__ == "__main__":
    main()
