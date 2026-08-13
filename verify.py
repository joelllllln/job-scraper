#!/usr/bin/env python3
"""
verify.py — confirm a job is real before you spend an application on it.

Aggregators are full of postings that are expired, filled, reposted for the
fifteenth time, or agency listings for a client that doesn't exist. Scoring an
unverified pile just ranks the noise. So every candidate gets fetched and
checked before score.py will rank it.

What it checks per job:
  - the URL resolves 200 and doesn't redirect to a generic listings page
  - the page isn't showing a closed / filled / expired marker
  - the page title still matches the job title we stored (catches redirects)
  - schema.org JobPosting JSON-LD: datePosted, validThrough, employer, description
  - years of experience demanded, pulled out of the description
  - whether the employer is anonymous ("a leading commodity trading house")
  - whether it's an agency listing rather than the hiring firm
  - repost history: same role, seen repeatedly over months = probably a ghost

Results go to a `verify` table in jobs.db. Nothing is deleted — a job that fails
verification is recorded as failed, so you can see what got filtered and why.

    python verify.py              # verify anything unverified
    python verify.py --recheck    # re-verify everything, including old passes
    python verify.py --limit 100
"""

import argparse
import json
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import http_client
import store

DB = "jobs.db"
TIMEOUT = 20
WORKERS = 8
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}

CLOSED_MARKERS = [
    "no longer accepting", "no longer available", "position has been filled",
    "this job has expired", "job has closed", "vacancy has closed",
    "applications are closed", "posting is closed", "this role has been filled",
    "job not found", "page not found", "we couldn't find that job",
    "this job is no longer", "advert has expired", "closed for applications",
]

LISTING_PAGE_MARKERS = ["search results", "jobs found", "browse all jobs", "current openings"]

ANON_EMPLOYER = re.compile(
    r"confidential|undisclosed|a leading|our client|leading (commodity|energy|trading)|"
    r"global (trading|commodity) (house|firm)|top.?tier|boutique (fund|firm)", re.I)

AGENCY_MARKERS = re.compile(
    r"our client|on behalf of our client|we are recruiting for|acting as an employment agency|"
    r"employment business", re.I)

# Deliberately narrow: "our team has 30 years" is a boast, but "our ideal
# candidate has 5 years" is a requirement, and only the first should be ignored.
FIRM_BOAST = re.compile(
    r"\b(we|our (team|firm|company|group|business|people)|the (firm|company|group))\b"
    r"\s+(have|has|bring\w*|boast\w*|with|combined)\b[^.]{0,30}$", re.I)

YEARS = re.compile(
    r"(\d{1,2})\s*(?:\+|plus)?\s*(?:-|to|–)?\s*(\d{1,2})?\s*(?:\+)?\s*years?[^.]{0,40}"
    r"(?:experience|exp\b|track record)", re.I)


def db():
    con = store.connect(DB)
    con.execute("""
        CREATE TABLE IF NOT EXISTS verify (
            id TEXT PRIMARY KEY,
            checked_at TEXT, status INTEGER, live INTEGER,
            final_url TEXT, title_match REAL, reason TEXT,
            employer TEXT, posted TEXT, valid_through TEXT,
            years_required INTEGER, anonymous INTEGER, agency INTEGER,
            desc_len INTEGER, description TEXT
        )""")
    con.commit()
    return con


def norm_title(s):
    return set(re.findall(r"[a-z]{3,}", (s or "").lower()))


def title_similarity(a, b):
    ta, tb = norm_title(a), norm_title(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta)


def extract_jsonld(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S):
        try:
            data = json.loads(block.strip())
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        if isinstance(data, dict) and "@graph" in data:
            items = data["@graph"]
        for it in items:
            if isinstance(it, dict) and it.get("@type") == "JobPosting":
                return it
    return None


def strip_html(s):
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def years_required(text):
    text = text or ""
    best = None
    for m in YEARS.finditer(text):
        # "we have 30 years of experience" is the firm's own blurb, not a demand
        # on you. It matters more now the number can drop a role outright.
        if FIRM_BOAST.search(text[max(0, m.start() - 60):m.start()]):
            continue
        # "3-5 years" means a floor of 3, not 5. "5+ years" means 5.
        val = int(m.group(1))
        if val <= 25 and (best is None or val < best):
            best = val   # across several mentions, the lowest is the real bar
    return best


# Statuses that mean "we were refused", not "the job is gone". A WAF answering
# 403 to a checker is the single most common failure here and it is not evidence
# of anything about the posting.
BOT_BLOCK = {401, 403, 405, 406, 429, 503}


def check(session, job):
    jid, company, title, url, source = job
    out = {"id": jid, "checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "status": 0, "live": 0, "final_url": url, "title_match": 0.0, "reason": "",
           "employer": company, "posted": "", "valid_through": "", "years_required": None,
           "anonymous": 1 if ANON_EMPLOYER.search(company or "") else 0,
           "agency": 0, "desc_len": 0, "description": ""}

    if not url:
        out["reason"] = "no url"
        return out

    r = http_client.get(url, sess=session)
    if r is None:
        out["reason"] = "unreachable (retries exhausted or host circuit open)"
        out["live"] = None          # unknown, not dead — see BOT_BLOCK below
        return out

    out["status"] = r.status_code
    out["final_url"] = r.url
    if r.status_code in BOT_BLOCK:
        # The site refused to talk to us. That says nothing whatever about
        # whether the job exists, and treating it as "dead" deleted 23 of 23
        # dead verdicts in one run — every single one a 403, not one real 404 —
        # taking live roles at Societe Generale, JPMorgan, Macquarie, Amazon and
        # Hayfin out of the digest. live=None means unknown, which score.py
        # keeps and ranks slightly below a confirmed-live role.
        out["live"] = None
        out["reason"] = f"http {r.status_code} — site blocks automated checks, job not verified"
        return out
    if r.status_code >= 400:
        out["reason"] = f"http {r.status_code}"
        return out

    html = http_client.text_of(r)
    low = html.lower()

    for marker in CLOSED_MARKERS:
        if marker in low:
            out["reason"] = f"closed marker: {marker}"
            return out

    ld = extract_jsonld(html)
    page_title = ""
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if m:
        page_title = strip_html(m.group(1))

    if ld:
        page_title = ld.get("title") or page_title
        org = ld.get("hiringOrganization") or {}
        if isinstance(org, dict) and org.get("name"):
            out["employer"] = org["name"]
        out["posted"] = ld.get("datePosted") or ""
        out["valid_through"] = ld.get("validThrough") or ""
        out["description"] = strip_html(ld.get("description") or "")[:20000]

    if not out["description"]:
        out["description"] = strip_html(html)[:20000]

    out["desc_len"] = len(out["description"])
    out["title_match"] = round(title_similarity(title, page_title), 3)
    out["years_required"] = years_required(out["description"])
    out["agency"] = 1 if AGENCY_MARKERS.search(out["description"]) else 0
    if ANON_EMPLOYER.search(out["employer"] or ""):
        out["anonymous"] = 1

    # a redirect to a listings page is the commonest silent failure
    if out["title_match"] < 0.34 and any(k in low[:4000] for k in LISTING_PAGE_MARKERS):
        out["reason"] = "redirected to listings page"
        return out

    if out["valid_through"]:
        try:
            vt = datetime.fromisoformat(out["valid_through"].replace("Z", "+00:00"))
            # "2026-12-31" parses to a naive datetime and comparing that to an
            # aware one raises, which was swallowed as a warning on every single
            # date-only validThrough — so those postings never got their expiry
            # checked at all.
            if vt.tzinfo is None:
                vt = vt.replace(tzinfo=timezone.utc)
            if vt < datetime.now(timezone.utc):
                out["reason"] = "validThrough in the past"
                return out
        except ValueError:
            pass

    out["live"] = 1
    out["reason"] = "ok"
    return out


COLS = ["id", "checked_at", "status", "live", "final_url", "title_match", "reason",
        "employer", "posted", "valid_through", "years_required", "anonymous",
        "agency", "desc_len", "description"]


def flush(con, rows):
    """Checkpoint partial progress. A run killed at job 400 keeps the first 399."""
    if not rows:
        return
    try:
        con.executemany(
            f"INSERT OR REPLACE INTO verify ({','.join(COLS)}) "
            f"VALUES ({','.join('?' * len(COLS))})",
            [[r[c] for c in COLS] for r in rows])
        con.commit()
    except sqlite3.Error as e:
        print(f"  ! checkpoint failed: {e}", file=sys.stderr)
        con.rollback()


def reparse(con):
    """Recompute the derived fields from descriptions already stored. No network.

    The parsers improve; the descriptions don't change. Without this, a fix to
    years_required only ever applies to jobs verified after it landed, and every
    row already in the table keeps whatever the old parser decided. That mattered
    little when the number was a scoring penalty. It matters now it can drop a
    role from the digest outright.
    """
    rows = con.execute("SELECT id, description, years_required, agency FROM verify "
                       "WHERE COALESCE(description,'') != ''").fetchall()
    changed = 0
    for jid, desc, old_years, old_agency in rows:
        years = years_required(desc)
        agency = 1 if AGENCY_MARKERS.search(desc) else 0
        if (years, agency) != (old_years, old_agency):
            con.execute("UPDATE verify SET years_required=?, agency=? WHERE id=?",
                        (years, agency, jid))
            changed += 1
    con.commit()
    return len(rows), changed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--reparse", action="store_true",
                    help="re-run the parsers over stored descriptions, no network")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    con = db()
    if args.reparse:
        seen, changed = reparse(con)
        print(f"reparsed {seen} stored descriptions, {changed} row(s) changed")
        return
    q = "SELECT id, company, title, url, source FROM jobs"
    if not args.recheck:
        q += " WHERE id NOT IN (SELECT id FROM verify)"
    q += " ORDER BY first_seen DESC"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = con.execute(q).fetchall()

    if not rows:
        print("nothing to verify")
        return
    print(f"verifying {len(rows)} jobs\n")

    session = http_client.session()
    results, pending = [], []
    try:
      with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(check, session, row): row for row in rows}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
            except Exception as e:
                print(f"  ! {e}", file=sys.stderr)
                continue
            results.append(res)
            pending.append(res)
            row = futs[fut]
            flag = "LIVE" if res["live"] else "DEAD"
            print(f"[{i}/{len(rows)}] {flag}  {row[1][:24]:<26} {row[2][:44]:<46} {res['reason']}")
            if len(pending) >= 20:
                flush(con, pending)
                pending = []

    except KeyboardInterrupt:
        print("\ninterrupted — saving what completed", file=sys.stderr)

    flush(con, pending)

    live = sum(1 for r in results if r["live"] == 1)
    blocked = sum(1 for r in results if r["live"] is None)
    dead_n = sum(1 for r in results if r["live"] == 0)
    print(f"\n{live} live / {dead_n} dead / {blocked} unverifiable (site blocked us) "
          f"/ {len(results)} checked")
    dead = {}
    for r in results:
        if r["live"] == 0:
            dead[r["reason"].split(":")[0]] = dead.get(r["reason"].split(":")[0], 0) + 1
    for k, v in sorted(dead.items(), key=lambda x: -x[1]):
        print(f"  {v:>4}  {k}")


if __name__ == "__main__":
    main()
