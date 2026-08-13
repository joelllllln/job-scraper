#!/usr/bin/env python3
"""
sniff.py — fingerprint each firm's ATS by reading its actual careers page.

This replaces token-guessing. discover.py guesses what a company's slug might be;
sniff.py goes to the company's own careers page and reads the ATS link straight
out of the HTML. Much higher hit rate, and it's the only way to get Workday —
Workday boards are keyed by tenant + datacenter + site, which cannot be guessed
from a company name.

    python sniff.py                 # all firms in firms.csv
    python sniff.py no_ats.csv      # only the ones discover.py missed

Writes:
    sniffed.csv   name, ats, token, tenant, dc, site, board_url, found_on
    manual.csv    firms on an ATS with no public API (iCIMS, Taleo, SAP, Avature...)
    unknown.csv   nothing detected — needs a human look
"""

import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_client

TIMEOUT = 15
WORKERS = 24        # each firm is a different host, so the per-host throttle
                    # never binds; the limit is how many sockets we want open
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

PATHS = ["", "/careers", "/careers/", "/jobs", "/about/careers", "/company/careers",
         "/en/careers", "/careers/vacancies", "/careers/opportunities", "/join-us",
         "/work-with-us", "/about-us/careers", "/careers/jobs"]

# Careers SUBDOMAINS, tried when the apex domain gives nothing. Large firms put
# the corporate site behind a WAF and host recruitment separately, very often on
# the ATS vendor's own infrastructure — so careers.example.com answers happily
# while example.com returns 403 to anything that looks automated. Thirteen of
# fifteen unreachable prime targets (Citadel Securities, BNP Paribas, Cantor
# Fitzgerald, Sucden, ScottishPower, Peel Hunt) are bot-blocked on the apex and
# were never tried anywhere else. This is looking in the right place, not
# working around the block: a host that answers is a host willing to serve us.
# "www" is in here because plenty of apex domains have no DNS record at all —
# only www does — and nothing anywhere tried it. That is the likeliest single
# explanation for 401 firms recorded as "no response": a real company with a
# working website that we asked for at an address that does not exist.
SUBDOMAINS = ["www", "careers", "jobs", "recruitment", "apply", "talent", "workfor"]

# Guessing thirteen paths finds the careers page only if it is at one of them.
# Following the link the site itself provides finds it wherever it lives —
# /life-here, /who-we-are/opportunities, /en-gb/careers-and-benefits. This is
# the last resort, after every guess has failed, so it costs one extra fetch
# only for firms that were about to be recorded as unreachable.
ANCHOR = re.compile(r'<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>', re.S | re.I)
CAREERS_WORDS = re.compile(
    r"career|job|vacanc|opportunit|join\s+(?:us|our)|work\s+(?:for|with)\s+us|"
    r"recruit|hiring|opening|life\s+(?:at|here)|working\s+(?:at|here)|"
    r"our\s+people|grow\s+with\s+us|early\s+care|graduate", re.I)
FOLLOW_LIMIT = 4

# ATS with a public API we can scrape
SCRAPABLE = {
    "greenhouse":      [r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]+)",
                        r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]+)"],
    "lever":           [r"jobs\.lever\.co/([a-z0-9-]+)"],
    "ashby":           [r"jobs\.ashbyhq\.com/([a-z0-9-]+)"],
    "smartrecruiters": [r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9-]+)"],
    "workable":        [r"apply\.workable\.com/([a-z0-9-]+)"],
    "recruitee":       [r"([a-z0-9-]+)\.recruitee\.com"],
    "teamtailor":      [r"([a-z0-9-]+)\.teamtailor\.com"],
    "personio":        [r"([a-z0-9-]+)\.jobs\.personio\.(?:com|de)"],
    "breezy":          [r"([a-z0-9-]+)\.breezy\.hr"],
    "bamboohr":        [r"([a-z0-9-]+)\.bamboohr\.com"],
    "rippling":        [r"ats\.rippling\.com/([a-z0-9-]+)",
                        r"rippling\.com/platform/api/ats/v1/board/([a-z0-9-]+)"],
    "pinpoint":        [r"([a-z0-9-]+)\.pinpointhq\.com"],
    "comeet":          [r"comeet\.co/jobs/([a-z0-9-]+)",
                        r"comeet\.co/careers-api/2\.0/company/([A-Za-z0-9.]+)"],
    # scrape.py has been able to fetch Jobvite boards all along, but nothing
    # here could recognise one, so the fetcher was unreachable code.
    "jobvite":         [r"jobs\.jobvite\.com/(?:careers/)?([a-z0-9-]+)",
                        r"([a-z0-9-]+)\.jobvite\.com"],
    # Enterprise boards with a public zero-auth JSON API. These used to be
    # filed under MANUAL as unreachable, which meant discovering one was worth
    # nothing — and Oracle was the most common unsupported ATS in the probe.
    "eightfold":       [r"([a-z0-9-]+)\.eightfold\.ai"],
}

# Oracle Recruiting Cloud needs the pod host AND the site number, which appear
# together in the careers URL:
#   https://<host>.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1001/...
# so the whole API URL is assembled here rather than templated from a token.
ORACLE = re.compile(
    r"https?://([a-z0-9.-]*oraclecloud\.com)/hcmUI/CandidateExperience/[a-z-]+/sites/([A-Za-z0-9_]+)",
    re.I)
ORACLE_API = ("https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
              "?onlyData=true&expand=requisitionList.secondaryLocations"
              "&finder=findReqs;siteNumber={site},limit=200,sortBy=POSTING_DATES_DESC")

# Workday needs three parts, handled separately
WORKDAY = re.compile(
    r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:([a-z]{2}-[A-Z]{2})/)?([A-Za-z0-9_-]+)")

# Recruiter ATS — Bullhorn leaks its cluster + corp token straight into the
# career-portal HTML, which is exactly what the public REST API needs.
BULLHORN = re.compile(r"public-rest(\d*)\.bullhornstaffing\.com/rest-services/([A-Za-z0-9]+)")
RECRUITER = {
    "jobadder": r"([a-z0-9-]+)\.jobadder\.com",
    "vincere":  r"([a-z0-9-]+)\.vincere\.io",
    "idibu":    r"[a-z0-9-]+\.idibu\.com",
    "loxo":     r"[a-z0-9-]+\.loxo\.co",
    "jobvite":  r"jobs\.jobvite\.com",
}

# ATS with no usable public API — flag for manual handling
MANUAL = {
    "icims": r"[a-z0-9-]+\.icims\.com",
    "taleo": r"[a-z0-9-]+\.taleo\.net",   # has a public API but it is POST with a
                                          # column-array response; not built yet
    "successfactors": r"(?:career\d*\.successfactors|jobs\.sap\.com)",
    "avature": r"[a-z0-9-]+\.avature\.net",
    "eploy": r"[a-z0-9-]+\.eploy\.net",
    "oleeo": r"[a-z0-9-]+\.oleeo\.com",
    "tribepad": r"[a-z0-9-]+\.tribepad\.com",
    # pinpoint moved to SCRAPABLE — it publishes postings.json
    "applied": r"app\.beapplied\.com",
    "jobvite": r"jobs\.jobvite\.com",
    "brassring": r"[a-z0-9-]+\.brassring\.com",
    "phenom": r"[a-z0-9-]+\.phenompeople\.com",
}


def candidate_urls(domain):
    """Where a firm's careers page might actually live.

    Apex paths first, because that is where most of them are and it is one
    request. Subdomains after, because a corporate WAF frequently guards
    example.com while careers.example.com is served by the ATS vendor with no
    protection at all — and the second is the page we actually want to read.
    """
    apex = domain.split("/")[0]
    base = apex[4:] if apex.startswith("www.") else apex
    for path in PATHS:
        yield f"https://{domain}{path}"
    # The reverse case: the registry says www.x.com but only x.com answers.
    if apex.startswith("www."):
        yield f"https://{base}"
    for sub in SUBDOMAINS:
        # Skip if the registry domain already IS the careers host, or the two
        # loops would fetch the same URL twice per firm across 2,300 firms.
        if apex.startswith(f"{sub}."):
            continue
        yield f"https://{sub}.{base}"


def careers_links(html, base_url, limit=FOLLOW_LIMIT):
    """Links on this page that the site itself labels as careers or jobs.

    Matched on the link TEXT as well as the href, because plenty of sites point
    at /life-here or /who-we-are and only the words give it away. Stays on the
    same host: an off-site link is either the ATS (already matched by the
    fingerprints above) or somebody else's website.
    """
    import urllib.parse as _u
    host = _u.urlsplit(base_url).netloc
    out, seen = [], set()
    for href, text in ANCHOR.findall(html or ""):
        label = re.sub(r"<[^>]+>", " ", text)
        if not (CAREERS_WORDS.search(href) or CAREERS_WORDS.search(label)):
            continue
        full = _u.urljoin(base_url, href.strip())
        if not full.startswith("http") or _u.urlsplit(full).netloc != host:
            continue
        full = full.split("#")[0].rstrip("/")
        if full.rstrip("/") == base_url.rstrip("/") or full in seen:
            continue
        seen.add(full)
        out.append(full)
        if len(out) >= limit:
            break
    return out


def fingerprint(name, blob, final_url):
    """Every ATS test, against one page. Returns a row or None."""
    m = BULLHORN.search(blob)
    if m:
        cls, token = m.group(1) or "", m.group(2)
        return {"name": name, "ats": "bullhorn", "token": token, "tenant": cls,
                "dc": "", "site": "", "locale": "",
                "board_url": f"https://public-rest{cls}.bullhornstaffing.com/rest-services/{token}/search/JobOrder",
                "found_on": final_url}

    for ats, pat in RECRUITER.items():
        m = re.search(pat, blob, re.I)
        if m:
            tok = m.group(1) if m.groups() else ""
            return {"name": name, "ats": ats, "token": tok, "tenant": "", "dc": "",
                    "site": "", "locale": "", "board_url": final_url,
                    "found_on": final_url, "_manual": True}

    m = ORACLE.search(blob)
    if m:
        host, site = m.group(1), m.group(2)
        return {"name": name, "ats": "oracle", "token": f"{host}/{site}",
                "tenant": host, "dc": "", "site": site, "locale": "",
                "board_url": ORACLE_API.format(host=host, site=site),
                "found_on": final_url}

    m = WORKDAY.search(blob)
    if m and (m.group(4) or "").lower() not in ("wday", "en-us"):
        tenant, dc, locale, site = m.group(1), m.group(2), m.group(3) or "", m.group(4)
        return {"name": name, "ats": "workday", "token": f"{tenant}/{site}",
                "tenant": tenant, "dc": dc, "site": site, "locale": locale,
                "board_url": f"https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                "found_on": final_url}

    for ats, patterns in SCRAPABLE.items():
        for pat in patterns:
            m = re.search(pat, blob, re.I)
            if m:
                tok = m.group(1)
                if tok.lower() in ("www", "jobs", "careers", "api", "apply", "boards"):
                    continue
                return {"name": name, "ats": ats, "token": tok, "tenant": "", "dc": "",
                        "site": "", "locale": "", "board_url": "", "found_on": final_url}

    for ats, pat in MANUAL.items():
        if re.search(pat, blob, re.I):
            return {"name": name, "ats": ats, "token": "", "tenant": "", "dc": "",
                    "site": "", "locale": "", "board_url": final_url,
                    "found_on": final_url, "_manual": True}
    return None


def sniff_one(session, firm):
    name, domain = firm["name"], (firm.get("domain") or "").strip()
    # No domain, nothing to read. Companies House supplies thousands of firms
    # with no website, and "https:///careers" still costs a full DNS timeout —
    # 13 of those per firm is hours of the run spent proving nothing.
    if not domain:
        return None
    # A WAF that answers 403 to the homepage answers 403 to every path on that
    # host, so walking the remaining twelve is twelve guaranteed failures per
    # firm — and it is exactly the blocked firms that get re-probed every week,
    # because they never record an answer. Give up on the host at the first bot
    # block and spend the budget on the careers subdomain instead, which is
    # usually a different machine entirely.
    walled, tried_host = set(), set()
    fallback = []          # (html, url) of pages read but not fingerprinted
    for url in candidate_urls(domain):
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
            # No answer at all on the FIRST request to this host means the host
            # does not resolve, and /careers will not resolve either. Abandoning
            # it here saves twelve DNS timeouts per firm and, more usefully, gets
            # to the www variant while there is still time in the stage.
            if first_touch:
                walled.add(host)
            continue
        if r.status_code >= 400:
            continue
        html = http_client.text_of(r)
        blob = html + " " + r.url

        hit = fingerprint(name, blob, r.url)
        if hit:
            return hit
        # Nothing on this page, but the page itself may point at the real one.
        if len(fallback) < 2:
            fallback.append((html, r.url))

    # Last resort: follow the link the site labels as careers. Guessing paths
    # only works if the careers page is at a path we guessed; every firm that
    # calls it /life-here or /who-we-are/opportunities was invisible.
    tried = set()
    for html, base in fallback:
        for link in careers_links(html, base):
            if link in tried:
                continue
            tried.add(link)
            r = http_client.get(link, sess=session)
            if r is None or r.status_code >= 400:
                continue
            hit = fingerprint(name, http_client.text_of(r) + " " + r.url, r.url)
            if hit:
                return hit
    return None


COLS = ["name", "ats", "token", "tenant", "dc", "site", "locale", "board_url", "found_on"]


def write_all(hits, manual, unknown):
    for path, rows, fields in [("sniffed.csv", hits, COLS),
                               ("manual.csv", manual, COLS),
                               ("unknown.csv", unknown, ["name", "category", "domain"])]:
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)


def answered(path="sniffed.csv", *extra):
    """Firms already fingerprinted, so a rerun only looks at what is new.

    Reading a firm's careers page is the slowest thing the pipeline does and the
    answer changes about never. Unincremental, it re-read all 2303 every run,
    overran the stage timeout, and — because the CSVs were only written at the
    very end — threw away the entire 40 minutes, which also left workday.py with
    no tenants to page through.
    """
    seen = set()
    for p in (path,) + extra:
        try:
            seen |= {(r.get("name") or "").strip().lower() for r in csv.DictReader(open(p))}
        except OSError:
            pass
    return seen


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "firms.csv"
    recheck = "--recheck" in sys.argv
    firms = list(csv.DictReader(open(src)))
    total = len(firms)

    # Always read what is already known, even on --recheck. Two reasons: a site
    # being down on the day must not delete a good answer, and render.py writes
    # entries here that sniff.py CANNOT re-derive by definition — they were
    # found by running JavaScript. Wiping those would silently throw away hours
    # of browser work with no way to tell it had happened.
    previous = {}
    for path in ("sniffed.csv", "manual.csv"):
        try:
            for row in csv.DictReader(open(path)):
                previous[(row.get("name") or "").strip().lower()] = (path, row)
        except OSError:
            pass

    hits, manual, unknown = [], [], []
    if not recheck:
        for path, bucket in (("sniffed.csv", hits), ("manual.csv", manual)):
            try:
                bucket += list(csv.DictReader(open(path)))
            except OSError:
                pass
        try:
            unknown += list(csv.DictReader(open("unknown.csv")))
        except OSError:
            pass
        known = answered("sniffed.csv", "manual.csv", "unknown.csv")
        firms = [f for f in firms if (f["name"] or "").strip().lower() not in known]
        print(f"{total} firms, {len(known)} already fingerprinted -> reading {len(firms)}\n")
        if not firms:
            print("nothing new to sniff — pass --recheck to read every careers page again")
            return

    session = http_client.session()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(sniff_one, session, f): f for f in firms}
        for i, fut in enumerate(as_completed(futs), 1):
            firm = futs[fut]
            try:
                res = fut.result()
            except Exception:
                res = None
            if res and res.pop("_manual", False):
                manual.append(res)
                print(f"[{i}/{len(firms)}] MAN  {res['name']:<34} {res['ats']}")
            elif res:
                hits.append(res)
                extra = f" ({res['tenant']}/{res['site']})" if res["ats"] == "workday" else ""
                print(f"[{i}/{len(firms)}] HIT  {res['name']:<34} {res['ats']}/{res['token']}{extra}")
            else:
                # Nothing found this time — but if we knew something before,
                # keep it rather than demoting the firm to unknown.
                was = previous.get((firm["name"] or "").strip().lower())
                if was:
                    path, row = was
                    (manual if path == "manual.csv" else hits).append(row)
                    print(f"[{i}/{len(firms)}] keep {firm['name']:<34} "
                          f"{row.get('ats', '?')} (nothing found today)")
                else:
                    unknown.append(firm)
                    print(f"[{i}/{len(firms)}] ---  {firm['name']}")
            # Checkpointed, because this stage runs under a timeout and being
            # killed at firm 1800 used to discard all 1800 answers.
            if i % 25 == 0:
                write_all(hits, manual, unknown)

    write_all(hits, manual, unknown)

    wd = sum(1 for h in hits if h["ats"] == "workday")
    print(f"\n{len(hits)} scrapable ({wd} Workday) -> sniffed.csv")
    print(f"{len(manual)} on closed ATS -> manual.csv (use links.md / boards.py)")
    print(f"{len(unknown)} undetected -> unknown.csv")


if __name__ == "__main__":
    main()
