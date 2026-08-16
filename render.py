#!/usr/bin/env python3
"""
render.py — read the careers pages that only exist after JavaScript runs.

Measured, not guessed: of 300 firms with no discoverable ATS, 86% served their
careers page fine and simply had no ATS link in the HTML, because the listings
are drawn by JavaScript after load. No pattern fixes that. This runs a real
browser, waits for the page to settle, and reads what a person would see.

It is the expensive stage, so it is built to be run once and then not again:

  * The main prize is finding the ATS. One render that turns up
    "boards.greenhouse.io/vitol" is written to sniffed.csv, and from then on
    every weekly run reads that firm through the cheap JSON API instead. The
    browser is a discovery tool, not a collection tool.
  * Failing that, it reads schema.org JobPosting blocks straight off the
    rendered page and writes them to an inbox CSV for inbox.py to ingest.

    pip install playwright && playwright install chromium
    python render.py                  # every firm with no known ATS
    python render.py --limit 50       # a taste
    python render.py --only "Vitol,Glencore"
    python render.py --blocked-only   # just the firms whose site refuses us

Incremental and checkpointed: a firm with an answer is never rendered again,
and results are written every few firms, so stopping it loses nothing.

robots.txt is honoured. A browser can trivially ignore it, which is exactly why
this asks first — the point is to read what firms publish, not to get round
anyone. A disallowed careers path is skipped and reported.
"""

import argparse
import csv
import json
import os
import env
import re
import sys
import threading
import urllib.parse
import urllib.request
import urllib.robotparser as robotparser

import embedded
import sniff

# One implementation, used by the static pass and the browser pass alike.
jobs_from_jsonld = embedded.jobs_from_jsonld

OUT_INBOX = "rendered_inbox.csv"
INBOX_FIELDS = ["company", "title", "location", "url", "source", "posted"]

# A browser is heavy: each page is a process, not a socket. Four at a time is
# comfortable on a laptop and still gets through 1,500 firms in an evening.
#
# This constant, and the "4 at a time" it printed, were both fiction until now:
# the loop below opened a single page and walked the list one firm at a time,
# managing three firms a minute. At 1,523 firms that is eight hours against a
# four-hour stage ceiling, so a third of the registry was never going to be
# reached. Playwright's sync API is bound to the thread that created it, so
# each worker builds its own browser rather than sharing one.
WORKERS = 4
PAGE_TIMEOUT_MS = 12000   # a careers page that has not answered in 12s will not
SETTLE_MS = 1200          # after load, give the listing widget time to draw
MAX_URLS_PER_FIRM = 5     # candidate_urls can offer a dozen; the later ones
                          # almost never hit and each costs a full timeout

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def browser_path():
    """Where chromium lives, when Playwright's own copy is not the one present.

    Returns None to let Playwright use its default, which is the normal case on
    a laptop after `playwright install chromium`.
    """
    explicit = os.getenv("CHROMIUM_PATH")
    if explicit and os.path.exists(explicit):
        return explicit
    root = os.getenv("PLAYWRIGHT_BROWSERS_PATH")
    if root and os.path.isdir(root):
        for entry in sorted(os.listdir(root), reverse=True):
            cand = os.path.join(root, entry, "chrome-linux", "chrome")
            if os.path.exists(cand):
                return cand
    return None


_ROBOTS = {}
ROBOTS_TIMEOUT = 8


def may_fetch(url):
    """robots.txt, cached per host. Unreachable robots means allowed.

    Fetched by hand rather than with RobotFileParser.read(), which calls
    urlopen with NO timeout: a host that accepts the connection and then never
    answers blocks the worker forever. One did — a real run sat on a single
    firm for forty minutes with no page load in flight and nothing to kill it,
    because the hang was in the politeness check rather than in the browser.
    """
    host = urllib.parse.urlsplit(url)
    root = f"{host.scheme}://{host.netloc}"
    rp = _ROBOTS.get(root)
    if rp is None:
        rp = robotparser.RobotFileParser()
        try:
            req = urllib.request.Request(f"{root}/robots.txt",
                                         headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=ROBOTS_TIMEOUT) as fh:
                rp.parse(fh.read(65536).decode("utf-8", "replace").splitlines())
        except Exception:
            rp = False          # unreachable, slow or absent — nothing forbids us
        _ROBOTS[root] = rp
    if rp is False:
        return True
    try:
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def ats_from_html(name, html, final_url):
    """The same fingerprints sniff.py uses, against the RENDERED page."""
    blob = f"{html} {final_url}"

    m = sniff.ORACLE.search(blob)
    if m:
        host, site = m.group(1), m.group(2)
        return {"name": name, "ats": "oracle", "token": f"{host}/{site}",
                "tenant": host, "dc": "", "site": site, "locale": "",
                "board_url": sniff.ORACLE_API.format(host=host, site=site),
                "found_on": final_url}

    m = sniff.WORKDAY.search(blob)
    if m and (m.group(4) or "").lower() not in ("wday", "en-us"):
        tenant, dc, locale, site = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        return {"name": name, "ats": "workday", "token": f"{tenant}/{site}",
                "tenant": tenant, "dc": dc, "site": site, "locale": locale,
                "board_url": f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                "found_on": final_url}

    for ats, pats in sniff.SCRAPABLE.items():
        for pat in pats:
            m = re.search(pat, blob, re.I)
            if m:
                return {"name": name, "ats": ats, "token": m.group(1), "tenant": "",
                        "dc": "", "site": "", "locale": "", "board_url": final_url,
                        "found_on": final_url}
    return None


def render_firm(page, firm):
    """Returns (ats_row, [job_rows], note)."""
    name = firm["name"]
    domain = (firm.get("domain") or "").strip()
    if not domain:
        return None, [], "no domain"

    walled, blocked_by_robots = set(), False
    for url in list(sniff.candidate_urls(domain))[:MAX_URLS_PER_FIRM]:
        host = url.split("/")[2]
        if host in walled:
            continue
        if not may_fetch(url):
            blocked_by_robots = True
            continue
        try:
            resp = page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        except Exception:
            continue
        if resp is None:
            continue
        if resp.status in (401, 403, 405, 406, 429, 503):
            walled.add(host)
            continue
        if resp.status >= 400:
            continue
        # The whole point: let the listing widget draw before reading.
        try:
            page.wait_for_timeout(SETTLE_MS)
            html = page.content()
        except Exception:
            continue

        ats = ats_from_html(name, html, page.url)
        if ats:
            return ats, [], f"ats via {page.url}"
        jobs = jobs_from_jsonld(name, html)
        if jobs:
            return None, jobs, f"{len(jobs)} JobPosting blocks at {page.url}"
        # After hydration the framework state is still in the DOM, and often
        # richer than the JSON-LD — which many sites emit only on the detail
        # page, not the listing.
        jobs = embedded.jobs_from_html(name, html, page.url)
        if jobs:
            return None, jobs, f"{len(jobs)} jobs in page state at {page.url}"
        jobs = embedded.jobs_from_links(name, html, page.url)
        if jobs:
            return None, jobs, f"{len(jobs)} jobs in the rendered markup at {page.url}"
    return None, [], "robots disallowed" if blocked_by_robots else "nothing found"


def load_targets(args):
    firms = list(csv.DictReader(open("firms.csv", encoding="utf-8")))
    if args.only:
        want = {n.strip().lower() for n in args.only.split(",") if n.strip()}
        return [f for f in firms if f["name"].lower() in want]

    done = sniff.answered("sniffed.csv", "manual.csv", "endpoints.csv")
    rendered = sniff.answered("rendered.csv")
    todo = [f for f in firms
            if f["name"].strip().lower() not in done
            and f["name"].strip().lower() not in rendered
            and (f.get("domain") or "").strip()]

    if args.blocked_only:
        try:
            walled = {r["name"] for r in csv.DictReader(open("firm_check.csv", encoding="utf-8"))
                      if r.get("verdict") == "blocked"}
            todo = [f for f in todo if f["name"] in walled]
        except OSError:
            pass
    return todo[:args.limit] if args.limit else todo


def append(path, rows, fields):
    if not rows:
        return
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def main():
    env.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="comma-separated firm names")
    ap.add_argument("--blocked-only", action="store_true",
                    help="only firms whose own site refuses plain requests")
    ap.add_argument("--headed", action="store_true", help="watch it work")
    ap.add_argument("--workers", type=int, default=WORKERS,
                    help=f"browsers in parallel (default {WORKERS})")
    args = ap.parse_args()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. This stage needs a real browser:\n"
              "    pip install playwright\n"
              "    playwright install chromium", file=sys.stderr)
        return 2

    todo = load_targets(args)
    if not todo:
        print("nothing to render — every firm already has an answer")
        return 0
    workers = max(1, args.workers)
    print(f"rendering {len(todo)} firms, {workers} browsers at a time\n")

    lock = threading.Lock()
    counter = {"i": 0}
    buf = {"ats": [], "jobs": [], "notes": []}
    exe = browser_path()

    def flush_locked():
        append("sniffed.csv", buf["ats"], sniff.COLS)
        append(OUT_INBOX, buf["jobs"], INBOX_FIELDS)
        append("rendered.csv", buf["notes"], ["name", "domain", "result"])
        buf["ats"], buf["jobs"], buf["notes"] = [], [], []

    def worker(chunk):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed,
                                         **({"executable_path": exe} if exe else {}))
            ctx = browser.new_context(user_agent=UA,
                                      viewport={"width": 1280, "height": 900})
            ctx.route("**/*.{png,jpg,jpeg,gif,webp,svg,woff,woff2,ttf,mp4}",
                      lambda route: route.abort())
            page = ctx.new_page()
            # Covers page.content(), which takes no timeout argument of its own
            # and will happily wait out a page whose scripts never settle.
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
            try:
                for firm in chunk:
                    try:
                        ats, jobs, note = render_firm(page, firm)
                    except Exception as e:
                        ats, jobs, note = None, [], f"error: {type(e).__name__}"
                    with lock:
                        counter["i"] += 1
                        i = counter["i"]
                        if ats:
                            buf["ats"].append(ats)
                        buf["jobs"] += jobs
                        buf["notes"].append({"name": firm["name"],
                                             "domain": firm.get("domain", ""),
                                             "result": note})
                        flag = "ATS" if ats else (f"{len(jobs)} jobs" if jobs else "   ")
                        print(f"[{i}/{len(todo)}] {flag:<9} "
                              f"{firm['name'][:32]:<34} {note[:52]}", flush=True)
                        # Checkpointed: a browser run is long, and interrupting
                        # it must never throw away the firms already done.
                        if i % 10 == 0:
                            flush_locked()
            finally:
                browser.close()

    # Interleaved, not sliced: a contiguous block would put every slow foreign
    # domain in one worker while another finishes early and idles.
    chunks = [todo[n::workers] for n in range(workers)]
    threads = [threading.Thread(target=worker, args=(c,), daemon=True)
               for c in chunks if c]
    try:
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    except KeyboardInterrupt:
        print("\ninterrupted — keeping what completed", file=sys.stderr)
    finally:
        with lock:
            flush_locked()

    print(f"\nwrote any ATS found to sniffed.csv — next run reads those firms "
          f"through their API, no browser needed")
    print(f"wrote any directly-read postings to {OUT_INBOX} "
          f"(python inbox.py --file {OUT_INBOX})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
