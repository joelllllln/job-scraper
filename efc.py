#!/usr/bin/env python3
"""
efc.py — eFinancialCareers.

There is no usable open-source scraper for eFC. Every repo on GitHub for it has
zero stars and most are dead. eFC's own APIs (core.ws.efinancialcareers.com,
candidate-search-api) are recruiter-side and token-gated — they're for posting
jobs and searching CVs, not for pulling listings.

So this takes the polite route: read the published XML sitemaps, then parse the
schema.org JobPosting JSON-LD that eFC embeds in each job page for Google Jobs.
That data is published specifically to be machine-read. It checks robots.txt
first and refuses to fetch anything disallowed.

    python efc.py --check          # show robots.txt rules, fetch nothing
    python efc.py --limit 200      # parse up to 200 job pages

If robots disallows the paths, don't force it — use boards.py instead. JobSpy's
Google source surfaces a lot of eFC listings via Google Jobs, which is the
sanctioned path to the same data.
"""

import argparse
import json
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone

import http_client
import yaml

import store
from scrape import build_filter, db_init

BASE = "https://www.efinancialcareers.co.uk"
UA_NAME = "job-registry"
UA = {"User-Agent": f"Mozilla/5.0 (compatible; {UA_NAME}/1.0)"}
TIMEOUT = 20
DELAY = 2.0          # seconds between page fetches. Do not lower this.


def robots():
    rp = urllib.robotparser.RobotFileParser()
    rp.set_url(f"{BASE}/robots.txt")
    try:
        rp.read()
    except Exception as e:
        print(f"could not read robots.txt: {e}", file=sys.stderr)
        return None
    return rp


def sitemaps(session, rp):
    """Find job sitemaps from robots.txt Sitemap: lines, then expand indexes."""
    out = []
    r = http_client.get(f"{BASE}/robots.txt", sess=session)
    if r is None:
        return out
    txt = http_client.text_of(r)
    roots = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)
    for root in roots:
        rr = http_client.get(root, sess=session)
        if rr is None:
            continue
        body = http_client.text_of(rr)
        locs = re.findall(r"<loc>\s*(.*?)\s*</loc>", body)
        if "<sitemapindex" in body:
            out += [l for l in locs if "job" in l.lower()]
        else:
            out.append(root)
        time.sleep(DELAY)
    return out


def job_urls(session, sm_urls, limit):
    urls = []
    for sm in sm_urls:
        rr = http_client.get(sm, sess=session)
        if rr is None:
            continue
        body = http_client.text_of(rr)
        urls += re.findall(r"<loc>\s*(.*?)\s*</loc>", body)
        time.sleep(DELAY)
        if len(urls) >= limit:
            break
    # eFC's job URLs used to be /jobs-<slug>. The last run pulled 3 sitemaps and
    # then kept 0 of their URLs, which is what a changed URL format looks like
    # from here — so match the shapes they actually use, and if the filter still
    # rejects everything, SAY SO and show what was really in the sitemap rather
    # than reporting a confident zero.
    seen, out = set(), []
    for u in urls:
        if u in seen:
            continue
        if re.search(r"/jobs?[-/]|/job/|/vacanc|-\d{6,}(?:\.|/|$)", u, re.I):
            seen.add(u)
            out.append(u)
    if urls and not out:
        print(f"  ! none of {len(urls)} sitemap URLs looked like a job page. Samples:",
              file=sys.stderr)
        for u in urls[:3]:
            print(f"      {u}", file=sys.stderr)
        print("    (eFC changed its URL format — update the pattern in efc.py)",
              file=sys.stderr)
    return out[:limit]


def parse_jsonld(html, url):
    """Pull schema.org JobPosting out of a job page."""
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict) or it.get("@type") != "JobPosting":
                continue
            org = it.get("hiringOrganization") or {}
            loc = it.get("jobLocation") or {}
            if isinstance(loc, list):
                loc = loc[0] if loc else {}
            addr = (loc or {}).get("address") or {}
            return {
                "company": (org.get("name") if isinstance(org, dict) else str(org)) or "",
                "title": it.get("title") or "",
                "location": addr.get("addressLocality") or addr.get("addressRegion") or "",
                "url": url,
                "source": "efinancialcareers",
                "posted": it.get("datePosted") or "",
            }
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=150)
    ap.add_argument("--check", action="store_true", help="report robots rules and exit")
    args = ap.parse_args()

    session = http_client.session()
    rp = robots()
    allowed = rp.can_fetch(UA_NAME, f"{BASE}/jobs-example") if rp else False
    print(f"robots.txt allows job pages for '{UA_NAME}': {allowed}")
    if args.check:
        return
    if not allowed:
        print("robots.txt disallows this. Stopping — use boards.py (Google source) instead.")
        return

    sm = sitemaps(session, rp)
    print(f"{len(sm)} job sitemaps")
    urls = job_urls(session, sm, args.limit)
    print(f"{len(urls)} job URLs\n")

    cfg = yaml.safe_load(open("config.yaml"))
    keep = build_filter(cfg)
    con = db_init()

    jobs = []
    for i, u in enumerate(urls, 1):
        if rp and not rp.can_fetch(UA_NAME, u):
            continue
        rr = http_client.get(u, sess=session)
        if rr is None:
            continue
        html = http_client.text_of(rr)
        j = parse_jsonld(html, u)
        if j:
            jobs.append(j)
        if i % 25 == 0:
            print(f"  {i}/{len(urls)}")
        time.sleep(DELAY)

    hits = [j for j in jobs if keep(j)]
    new = store.save_new(con, hits)

    store.report(new, len(jobs), len(hits))


if __name__ == "__main__":
    main()
