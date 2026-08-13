#!/usr/bin/env python3
"""
embedded.py — read jobs out of the JSON that modern sites ship inside the page.

The probe said 86% of unreachable firms serve their careers page fine but have
no ATS link in the HTML, and the obvious reading was "the listings are drawn by
JavaScript, so we need a browser". That is only half true. Next.js, Nuxt, Remix
and most Redux apps SERIALISE THE PAGE DATA INTO THE HTML so the client can
hydrate without a second round trip:

    <script id="__NEXT_DATA__" type="application/json">{...every job...}</script>
    <script>window.__NUXT__ = {...}</script>
    <script>window.__INITIAL_STATE__ = {...}</script>

So for a large share of those sites the jobs are sitting in the response we
already fetched, and no browser is needed. This is the cheap half of the 86%.

    python embedded.py                 # every firm with no known ATS
    python embedded.py --limit 200
    python embedded.py --only "Vitol,Glencore"

Writes embedded_inbox.csv, which inbox.py ingests into the same database with
the same filter as every other source.

What it does NOT do is guess. A blob is only mined if it contains objects that
look like job postings — a title, plus a link or a location — and the container
they sit in is named like a job list. Everything else in the page state (nav
menus, blog posts, office addresses) is left alone.
"""

import argparse
import csv
import json
import os
import re
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_client
import sniff

OUT = "embedded_inbox.csv"
FIELDS = ["company", "title", "location", "url", "source", "posted"]
WORKERS = 16

# Where frameworks park their serialised state.
NEXT_DATA = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
JSON_SCRIPT = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I)
ASSIGNED = re.compile(
    r'window\.(?:__NUXT__|__INITIAL_STATE__|__APOLLO_STATE__|__PRELOADED_STATE__|'
    r'__remixContext|__staticRouterHydrationData)\s*=\s*(\{.*?\})\s*[;<]', re.S)

# A container whose name says it holds jobs. Requiring this is what keeps blog
# posts, nav items and office addresses out of the results.
JOB_CONTAINER = re.compile(
    r"job|position|vacanc|opening|posting|role|career|requisition|opportunit", re.I)

TITLE_KEYS = ("title", "jobTitle", "name", "positionName", "displayJobTitle",
              "job_title", "position_title", "roleTitle", "headline")
URL_KEYS = ("url", "absolute_url", "applyUrl", "apply_url", "jobUrl", "link",
            "permalink", "slug", "path", "canonicalPositionUrl", "href")
LOC_KEYS = ("location", "city", "jobLocation", "primaryLocation", "locationName",
            "office", "locations", "workLocation", "location_name")
DATE_KEYS = ("datePosted", "postedDate", "publishedAt", "published_at", "createdAt",
             "created_at", "date", "postingDate", "live_date")

# Titles that are page furniture rather than vacancies.
NOT_A_JOB = re.compile(
    r"^(home|about|contact|search|login|sign in|register|apply|menu|careers?|"
    r"privacy|cookies?|terms|news|blog|events?|team|our people|life at|"
    r"benefits|culture|diversity|graduates?|students?|all jobs|view all)$", re.I)


def blobs(html):
    """Every JSON object serialised into the page, parsed."""
    out = []
    for rx in (NEXT_DATA, JSON_SCRIPT):
        for m in rx.findall(html or ""):
            try:
                out.append(json.loads(m.strip()))
            except (ValueError, TypeError):
                continue
    for m in ASSIGNED.findall(html or ""):
        try:
            out.append(json.loads(m))
        except (ValueError, TypeError):
            # Nuxt often assigns a function call rather than a literal; not
            # worth evaluating, and the browser stage covers those.
            continue
    return out


def _first_kv(d, keys):
    """Like _first, but says WHICH key matched — a slug and a full path need
    different joining, and getting the apply link wrong is worse than having
    none: verify.py would fetch it, get a 404, and mark a real job dead."""
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return k, str(v).strip()
        if isinstance(v, dict):
            inner = v.get("name") or v.get("city") or v.get("label")
            if inner:
                return k, str(inner).strip()
        if isinstance(v, list) and v:
            head = v[0]
            if isinstance(head, str):
                return k, head.strip()
            if isinstance(head, dict):
                inner = head.get("name") or head.get("city") or head.get("label")
                if inner:
                    return k, str(inner).strip()
    return "", ""


def _first(d, keys):
    for k in keys:
        v = d.get(k)
        if isinstance(v, (str, int, float)) and str(v).strip():
            return str(v).strip()
        if isinstance(v, dict):
            inner = v.get("name") or v.get("city") or v.get("label")
            if inner:
                return str(inner).strip()
        if isinstance(v, list) and v:
            head = v[0]
            if isinstance(head, str):
                return head.strip()
            if isinstance(head, dict):
                inner = head.get("name") or head.get("city") or head.get("label")
                if inner:
                    return str(inner).strip()
    return ""


def absolute(href, key, page_url):
    """Turn whatever the page called a link into one that actually resolves.

    A bare slug hangs off the careers page ("/careers/" + "gas-analyst"); an
    absolute path hangs off the origin. Guessing wrong produces a 404, and a
    404 is now read as proof the job is gone — so anything that does not look
    like a usable link returns empty and the caller falls back to the careers
    page, which is at least real.
    """
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return urllib.parse.urljoin(page_url, href)
    if key == "slug" or (" " not in href and "/" not in href):
        return page_url.rstrip("/") + "/" + href.lstrip("/")
    if " " in href:                      # a sentence, not a link
        return ""
    return urllib.parse.urljoin(page_url.rstrip("/") + "/", href)


def looks_like_job(obj):
    """A title, plus something that locates or links it. Both, or it is furniture."""
    if not isinstance(obj, dict):
        return False
    title = _first(obj, TITLE_KEYS)
    if not title or not (3 <= len(title) <= 140) or NOT_A_JOB.match(title):
        return False
    return bool(_first(obj, URL_KEYS) or _first(obj, LOC_KEYS))


def harvest(node, key_path="", depth=0, found=None):
    """Walk the parsed state, collecting objects from job-named containers."""
    if found is None:
        found = []
    if depth > 12:
        return found
    if isinstance(node, dict):
        for k, v in node.items():
            harvest(v, k, depth + 1, found)
    elif isinstance(node, list):
        # A list of job-shaped objects under a job-shaped key is the signal.
        # Both halves are required: "items" full of nav links is not a job list,
        # and one stray dict with a "title" is not either.
        hits = [x for x in node if looks_like_job(x)]
        if hits and JOB_CONTAINER.search(key_path or ""):
            found.extend(hits)
        else:
            for x in node:
                harvest(x, key_path, depth + 1, found)
    return found


def jobs_from_html(company, html, page_url):
    """Every job posting serialised into this page."""
    seen, out = set(), []
    for blob in blobs(html):
        for obj in harvest(blob):
            title = _first(obj, TITLE_KEYS)
            key, href = _first_kv(obj, URL_KEYS)
            href = absolute(href, key, page_url)
            key = (title.lower(), href)
            if key in seen:
                continue
            seen.add(key)
            out.append({"company": company, "title": title,
                        "location": _first(obj, LOC_KEYS),
                        "url": href or page_url, "source": "embedded",
                        "posted": _first(obj, DATE_KEYS)[:10]})
    return out


def jobs_from_jsonld(company, html):
    """schema.org JobPosting blocks, which many bespoke sites still emit.

    Published specifically to be machine-read — it is what puts these listings
    into Google for Jobs — so reading it is using the page as intended.
    """
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
                            html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        for node in (data if isinstance(data, list) else [data]):
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph")
            for item in (graph if isinstance(graph, list) else [node]):
                if not isinstance(item, dict):
                    continue
                if "JobPosting" not in str(item.get("@type", "")):
                    continue
                loc = item.get("jobLocation") or {}
                if isinstance(loc, list):
                    loc = loc[0] if loc else {}
                addr = (loc or {}).get("address") or {}
                where = " ".join(str(addr.get(k, "")) for k in
                                 ("addressLocality", "addressRegion", "addressCountry")).strip()
                out.append({
                    "company": (item.get("hiringOrganization") or {}).get("name") or company,
                    "title": (item.get("title") or "").strip(),
                    "location": where,
                    "url": item.get("url") or "",
                    # Named for where it was READ, not how: this same function
                    # now serves the static pass and the browser pass.
                    "source": "jsonld",
                    "posted": (item.get("datePosted") or "")[:10],
                })
    return [j for j in out if j["title"] and j["url"]]


def scan_firm(session, firm):
    domain = (firm.get("domain") or "").strip()
    if not domain:
        return []
    walled, tried_host = set(), set()
    for url in sniff.candidate_urls(domain):
        host = url.split("/")[2]
        if host in walled:
            continue
        first_touch = host not in tried_host
        tried_host.add(host)
        r = http_client.get(url, sess=session)
        if r is not None and r.status_code in (401, 403, 405, 406, 429, 503):
            walled.add(host)
            continue
        if r is None:
            if first_touch:      # host does not resolve — see sniff.sniff_one
                walled.add(host)
            continue
        if r.status_code >= 400:
            continue
        html = http_client.text_of(r)
        # JSON-LD first: it is a published contract with a fixed shape, so it is
        # more reliable than anything inferred from a framework's page state.
        jobs = jobs_from_jsonld(firm["name"], html)
        if jobs:
            return jobs
        jobs = jobs_from_html(firm["name"], html, r.url)
        if jobs:
            return jobs
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="comma-separated firm names")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv")))
    if args.only:
        want = {n.strip().lower() for n in args.only.split(",") if n.strip()}
        todo = [f for f in firms if f["name"].lower() in want]
    else:
        done = sniff.answered("sniffed.csv", "manual.csv", "endpoints.csv")
        todo = [f for f in firms
                if f["name"].strip().lower() not in done
                and (f.get("domain") or "").strip()]
        if args.limit:
            todo = todo[:args.limit]
    if not todo:
        print("nothing to scan")
        return 0

    print(f"scanning {len(todo)} firms for jobs embedded in page state\n")
    session = http_client.session()
    rows, hit_firms = [], 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(scan_firm, session, f): f for f in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                jobs = fut.result()
            except Exception as e:
                print(f"  ! {futs[fut]['name']}: {type(e).__name__}", file=sys.stderr)
                continue
            if jobs:
                hit_firms += 1
                rows += jobs
                print(f"[{i}/{len(todo)}] {futs[fut]['name'][:32]:<34} {len(jobs)} jobs")

    new = not os.path.exists(args.out) or os.path.getsize(args.out) == 0
    with open(args.out, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)
    print(f"\n{hit_firms} of {len(todo)} firms had jobs in their page state "
          f"({len(rows)} postings) -> {args.out}")
    print(f"ingest with: python inbox.py --file {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
