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
# Firms already walked, so a stopped sweep resumes instead of restarting.
SEEN = "embedded_scanned.csv"
FIELDS = ["company", "title", "location", "url", "source", "posted"]
WORKERS = 16
CHECKPOINT = 25      # firms between writes to disk

# Where frameworks park their serialised state.
NEXT_DATA = re.compile(
    r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', re.S | re.I)
JSON_SCRIPT = re.compile(
    r'<script[^>]+type=["\']application/json["\'][^>]*>(.*?)</script>', re.S | re.I)
# Next.js 13+ App Router does not emit __NEXT_DATA__ at all. It streams the
# payload as a series of self.__next_f.push([1, "<json as a JS string>"])
# calls, so the data is there but wrapped in one more layer of encoding. Sites
# on modern Next were invisible to the __NEXT_DATA__ reader.
NEXT_FLIGHT = re.compile(r'self\.__next_f\.push\(\s*\[\s*\d+\s*,\s*("(?:[^"\\]|\\.)*")',
                         re.S)

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

# Link text carries the call to action with it, and it ends up in the title:
# 45 rows arrived reading "Quantitative Researcher READ MORE", and one read
# "Application Support Analyst | Energy Trading Operations London, GB Full-Time
# Technology Explore more Explore more". Stripped from the end, repeatedly,
# because sites stack them.
TITLE_TAIL = re.compile(
    r"(?:\s*(?:read|explore|find out|learn|see|view|show)\s+more"
    r"|\s*apply(?:\s+now)?"
    r"|\s*(?:full|part)[- ]time"
    r"|\s*explore\s+opportunities"
    r"|\s*posted\s+\d+\s+\w+\s+ago"
    r"|\s*posted\s+(?:today|yesterday)"
    r"|\s*\d+\s+(?:day|week|month|hour)s?\s+ago"
    r"|\s*permanent|\s*contract|\s*temporary|\s*fixed[- ]term"
    r"|\s*view\s+(?:details|role|job|vacancy))\s*$", re.I)

# A card that repeats the firm's own name mid-title: Dartmouth Partners arrived
# as "Associate – Infrastructure Debt Investment team Dartmouth Partners London
# Full-Time Posted 4 weeks ago". Cutting at the company name recovers the role.
def _cut_at_company(title, company):
    if not company or len(company) < 4:
        return title
    m = re.search(r"\s+" + re.escape(company) + r"\b", title, re.I)
    return title[:m.start()].strip() if m and m.start() >= 3 else title


# A vacancy that says it is closed is not one.
CLOSED_TITLE = re.compile(r"\(\s*closed\s*\)|\bclosed\b\s*$|\bno longer (available|open)\b",
                          re.I)

# A vacancy is a page you apply on. These are documents and media, and a title
# taken from one is never a job — Olam Agri arrived titled
# "/content/dam/olam-agri/assets/webp/careers/careers-pdfs/", and EEX's actual
# vacancy was a PDF that got filed under a different firm entirely.
ASSET_HREF = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|jpe?g|png|gif|svg|mp[34]|webp)"
                        r"(\?|#|$)", re.I)

# Pages about the people who already work there. Natural Power's careers section
# hosts staff profiles under /careers/staff-stories/, so "Aaron Dickinson Energy
# analyst" — a named employee — was extracted as a vacancy and scored 90.
NOT_A_VACANCY_PATH = re.compile(
    r"/(staff|employee|people|team|colleague|our-people|meet-the-team|"
    r"stor(y|ies)|profiles?|testimonial|blog|news|insights?|press|"
    r"case-stud\w*|events?|podcasts?|webinars?)/", re.I)

# Titles that are page furniture rather than vacancies. The call-to-action
# phrases matter as much as the page names: "Apply now" sits under a perfectly
# job-shaped /jobs/apply-now href on a lot of listings.
NOT_A_JOB = re.compile(
    r"^(home|about|contact|search|login|sign in|register|menu|careers?|"
    r"privacy|cookies?|terms|news|blog|events?|team|our people|life at|"
    r"benefits|culture|diversity|graduates?|students?|all jobs|view all|"
    r"(apply|register|sign up|join)( now| here| today)?|read more|learn more|"
    r"find out more|view (details|role|job|more)|see (all|more)|more info\w*|"
    r"our culture|why (join )?us|open positions?|current (vacancies|openings))$", re.I)

# A title that is really a URL path. Sites sometimes render an asset link with
# its own href as the anchor text.
PATH_TITLE = re.compile(r"^/|^https?://|/content/|/assets?/", re.I)

# The landing page for an early-careers scheme, which is marketing rather than a
# vacancy: "Graduate Programme Podcast" scored 84 and reached the digest, as did
# "Our graduate programmes" and six banks' "Graduate Programs".
SCHEME_PAGE = re.compile(
    r"\b(graduate|internship|intern|early care\w*|apprentice\w*|summer)\b.*"
    r"\b(programme|program|scheme|opportunit\w*)s?\b|\bpodcast\b", re.I)

# A role a person is actually hired as. Deliberately excludes graduate, intern,
# trainee and apprentice — those are what SCHEME_PAGE is made of, so using
# ROLE_NOUN here would let every scheme page back through. This is what keeps
# "Global Trainee Broker Programme", a real vacancy, out of that bucket.
CONCRETE_ROLE = re.compile(
    r"\b(analyst|trader|broker|engineer|developer|scientist|economist|"
    r"specialist|controller|scheduler|officer|adviser|advisor|consultant|"
    r"accountant|auditor|actuary|actuarial|strategist|researcher|quant\w*|"
    r"dealer|underwriter|technician|operator|architect|manager|associate|"
    r"assistant|executive|coordinator|administrator|reporter|model\w*)\b", re.I)


def blobs(html):
    """Every JSON object serialised into the page, parsed."""
    out = []
    for rx in (NEXT_DATA, JSON_SCRIPT):
        for m in rx.findall(html or ""):
            try:
                out.append(json.loads(m.strip()))
            except (ValueError, TypeError):
                continue
    # App Router: each push carries a JSON string. Unwrap the string literal
    # first, then look for JSON objects inside what it yields.
    for m in NEXT_FLIGHT.findall(html or ""):
        try:
            inner = json.loads(m)
        except (ValueError, TypeError):
            continue
        for frag in re.findall(r"\{.*\}", inner or "", re.S):
            try:
                out.append(json.loads(frag))
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


# An href that names a job detail page. Deliberately explicit: "/careers/" on
# its own also matches /careers/benefits and /careers/our-culture, which is how
# a careers landing page turns into six imaginary vacancies.
JOB_HREF = re.compile(
    r"/(?:jobs?|vacanc\w*|positions?|opportunit\w*|roles?|openings?|"
    r"job-(?:detail|description)|apply)/[\w%-]{2,}", re.I)
# Same idea for the /careers/<slug> shape, which is real but needs the slug to
# look like a role rather than a page of perks.
CAREERS_SLUG = re.compile(r"/careers?/[\w%-]+", re.I)
# What makes a string a job title rather than a page name. Almost every real
# vacancy contains one of these; "Our culture", "Benefits" and "Why join us"
# contain none, which is exactly the distinction /careers/<slug> needs.
ROLE_NOUN = re.compile(
    r"\b(analyst|trader|broker|manager|engineer|developer|scientist|economist|"
    r"associate|assistant|specialist|controller|scheduler|officer|adviser|advisor|"
    r"consultant|accountant|auditor|actuary|actuarial|strategist|researcher|"
    r"research|quant\w*|intern|graduate|trainee|apprentice|lead|head|director|"
    r"supervisor|administrator|coordinator|executive|partner|counsel|paralegal|"
    r"technician|operator|dealer|underwriter|architect|designer|marketer)\b", re.I)
ANCHOR_TAG = re.compile(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.S | re.I)
# The text node immediately after a link, where a listing usually puts the
# location. Bounded by the next tag: without that it ran on through the closing
# </li> and swallowed the following job's title as part of the location.
TRAILING = re.compile(r"^[\s\u2014\u2013,|·-]*([A-Za-z][\w .,'-]{2,40})")


def clean_title(title, company=""):
    """Strip the call-to-action the link text dragged in, however many times."""
    title = _cut_at_company(title, company)
    prev = None
    while title != prev:
        prev = title
        title = TITLE_TAIL.sub("", title).strip(" –—-|,·").strip()
    return title


def usable_title(title):
    """One gate, so every extractor rejects the same furniture."""
    if not title or not (3 <= len(title) <= 140):
        return False
    if NOT_A_JOB.match(title) or PATH_TITLE.match(title) or CLOSED_TITLE.search(title):
        return False
    # A scheme page is only a vacancy if it names the role being hired.
    if SCHEME_PAGE.search(title) and not CONCRETE_ROLE.search(title):
        return False
    return True


def jobs_from_links(company, html, page_url):
    """Vacancies from a plain HTML listing — no JSON anywhere on the page.

    Plenty of firms, especially smaller ones, still publish a <ul> of links.
    The link text is the title and the href is a real URL taken off the page
    rather than guessed, which matters: a guessed 404 is now read as proof the
    job is gone.

    Precision comes from requiring the href to name a job detail page. Anchor
    text alone is far too weak — "Our culture" and "Benefits" are two-word
    phrases sitting under /careers/ on almost every careers landing page.
    """
    out, seen = [], set()
    for href, inner in ANCHOR_TAG.findall(html or ""):
        title = re.sub(r"<[^>]+>", " ", inner)
        title = clean_title(re.sub(r"\s+", " ", title).strip(), company)
        if not usable_title(title) or ASSET_HREF.search(href) \
                or NOT_A_VACANCY_PATH.search(href):
            continue
        explicit = JOB_HREF.search(href)
        if not (explicit or CAREERS_SLUG.search(href)):
            continue
        # Under /careers/<slug> the slug could be anything — /careers/benefits and
        # /careers/our-culture sit there on almost every site — so the link text
        # has to read like a job title, meaning it names a role.
        if not explicit and not ROLE_NOUN.search(title):
            continue
        full = absolute(href, "href", page_url)
        if not full or full in seen:
            continue
        seen.add(full)
        # Whatever follows the link before the next tag is usually the location.
        after = html.split(f">{inner}</a>", 1)[-1] if f">{inner}</a>" in html else ""
        tail = after.split("<", 1)[0][:80]
        m = TRAILING.match(tail)
        out.append({"company": company, "title": title,
                    "location": (m.group(1).strip() if m else ""),
                    "url": full, "source": "html", "posted": ""})
    return out


def looks_like_job(obj):
    """A title, plus something that locates or links it. Both, or it is furniture."""
    if not isinstance(obj, dict):
        return False
    # The same gate the link reader uses. Page state carries the same furniture:
    # "Graduate Programs" sits in a jobs array on plenty of bank career sites.
    if not usable_title(clean_title(_first(obj, TITLE_KEYS))):
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
    """Every job this firm serves in its page markup, from any page we can read.

    Uses sniff.pages so it reaches exactly what ATS discovery reaches — the
    careers subdomain, the www variant, and the link the site labels as
    careers. Before this it walked its own shorter list and missed all three.
    """
    domain = (firm.get("domain") or "").strip()
    if not domain:
        return []
    for html, url in sniff.pages(session, domain):
        # JSON-LD first: a published contract with a fixed shape, so more
        # reliable than anything inferred from a framework's page state.
        jobs = jobs_from_jsonld(firm["name"], html)
        if jobs:
            return jobs
        jobs = jobs_from_html(firm["name"], html, url)
        if jobs:
            return jobs
        # Last: a plain HTML listing, which is what smaller firms still publish
        # and what nothing else here can read.
        jobs = jobs_from_links(firm["name"], html, url)
        if jobs:
            return jobs
    return []


def append(path, rows, fields):
    if not rows:
        return
    new = not os.path.exists(path) or os.path.getsize(path) == 0
    with open(path, "a", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerows(rows)


def scanned(path=SEEN):
    """Firms already walked, so an interrupted sweep resumes where it stopped."""
    if not os.path.exists(path):
        return set()
    with open(path, newline="", encoding="utf-8") as fh:
        return {(r.get("name") or "").strip().lower() for r in csv.DictReader(fh)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", help="comma-separated firm names")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--rescan", action="store_true",
                    help="walk every firm again, including ones already scanned")
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv", encoding="utf-8")))
    if args.only:
        want = {n.strip().lower() for n in args.only.split(",") if n.strip()}
        todo = [f for f in firms if f["name"].lower() in want]
    else:
        done = sniff.answered("sniffed.csv", "manual.csv", "endpoints.csv")
        if not args.rescan:
            done |= scanned()
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
    # Checkpointed every CHECKPOINT firms. This sweep walks 1,500 sites over the
    # best part of an hour, and it used to hold every row in memory until the
    # last one finished — so a laptop going to sleep threw away the whole stage.
    # The cost of an interruption is now at most CHECKPOINT firms, and a rerun
    # skips what is already recorded rather than starting again.
    seen, total_jobs = [], 0

    def flush():
        nonlocal rows, seen
        append(args.out, rows, FIELDS)
        append(SEEN, seen, ["name", "jobs"])
        rows, seen = [], []

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(scan_firm, session, f): f for f in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                name = futs[fut]["name"]
                try:
                    jobs = fut.result()
                except Exception as e:
                    print(f"  ! {name}: {type(e).__name__}", file=sys.stderr)
                    continue
                seen.append({"name": name, "jobs": len(jobs)})
                if jobs:
                    hit_firms += 1
                    total_jobs += len(jobs)
                    rows += jobs
                    print(f"[{i}/{len(todo)}] {name[:32]:<34} {len(jobs)} jobs")
                if i % CHECKPOINT == 0:
                    flush()
    except KeyboardInterrupt:
        print("\ninterrupted — keeping what completed", file=sys.stderr)
    finally:
        flush()

    print(f"\n{hit_firms} of {len(todo)} firms had jobs in their page state "
          f"({total_jobs} postings) -> {args.out}")
    print(f"ingest with: python inbox.py --file {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
