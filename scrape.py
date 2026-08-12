#!/usr/bin/env python3
"""
scrape.py — pull every discovered ATS endpoint + Adzuna, filter to roles you
want, dedupe, store in SQLite, and print what's new since the last run.

    python scrape.py            # normal run
    python scrape.py --all      # print everything, not just new

Adzuna (optional, official API, free tier) — set:
    export ADZUNA_APP_ID=...
    export ADZUNA_APP_KEY=...
"""

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

import http_client
import yaml

import store

DB = "jobs.db"

# The public GET endpoint for each ATS, keyed by the token sniff.py / discover.py
# found. discover.py bakes the URL into endpoints.csv; sniffed.csv only records
# the ats + token, so we rebuild the URL from this table.
ATS_ENDPOINT = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=false",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=100",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{t}?details=true",
    "recruitee": "https://{t}.recruitee.com/api/offers/",
    "teamtailor": "https://{t}.teamtailor.com/jobs.rss",
    "personio": "https://{t}.jobs.personio.com/xml",
    "breezy": "https://{t}.breezy.hr/json",
    "bamboohr": "https://{t}.bamboohr.com/careers/list",
    "rippling": "https://api.rippling.com/platform/api/ats/v1/board/{t}/jobs",
    "pinpoint": "https://{t}.pinpointhq.com/postings.json",
    "comeet": "https://www.comeet.co/careers-api/2.0/company/{t}/positions",
    "jobvite": "https://jobs.jobvite.com/api/v1/jobs?companyId={t}",
}

# The newer boards all return "a list of job dicts", but disagree about what the
# list is called and what the keys are. One tolerant reader beats five brittle ones.
GENERIC_ATS = ("rippling", "pinpoint", "comeet", "jobvite")
LIST_KEYS = ("data", "jobs", "positions", "results", "items", "postings")
TITLE_KEYS = ("title", "name", "position_name", "jobTitle")
URL_KEYS = ("url", "absolute_url", "apply_url", "careers_url", "hostedUrl", "applyUrl", "link")
LOC_KEYS = ("location", "city", "workplace_city", "locationName", "office")
DATE_KEYS = ("published_at", "created_at", "posted_at", "publishedAt", "date", "updated_at")


# ---------- storage ----------

def db_init():
    """Kept as the shared entry point — schema and dedupe now live in store.py."""
    return store.connect(DB)


def job_id(company, title, location=""):
    return store.canonical_key(company, title)


# ---------- normalisers, one per ATS ----------

def first_of(d, keys):
    """First non-empty value among several possible spellings of the same field."""
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return ""


def norm(ats, company, payload):
    out = []
    def add(title, loc, url, posted=""):
        if title:
            out.append({"company": company, "title": title.strip(),
                        "location": (loc or "").strip(), "url": url or "",
                        "source": ats, "posted": posted or ""})

    if ats == "greenhouse":
        for j in payload.get("jobs", []):
            add(j.get("title"), (j.get("location") or {}).get("name"),
                j.get("absolute_url"), j.get("updated_at", ""))
    elif ats == "lever":
        for j in payload:
            add(j.get("text"), (j.get("categories") or {}).get("location"),
                j.get("hostedUrl"), str(j.get("createdAt", "")))
    elif ats == "ashby":
        for j in payload.get("jobs", []):
            add(j.get("title"), j.get("location"), j.get("jobUrl"), j.get("publishedAt", ""))
            comp = (j.get("compensation") or {}).get("compensationTierSummary")
            if comp and out:
                out[-1]["salary_text"] = comp
    elif ats == "smartrecruiters":
        for j in payload.get("content", []):
            loc = j.get("location") or {}
            add(j.get("name"), f"{loc.get('city','')} {loc.get('country','')}",
                f"https://jobs.smartrecruiters.com/{j.get('company',{}).get('identifier','')}/{j.get('id','')}",
                j.get("releasedDate", ""))
    elif ats == "workable":
        for j in payload.get("jobs", []):
            add(j.get("title"), f"{j.get('city','')} {j.get('country','')}",
                j.get("url") or j.get("application_url"), j.get("published_on", ""))
    elif ats == "breezy":
        for j in payload:
            loc = (j.get("location") or {}).get("name") or ""
            add(j.get("name"), loc, j.get("url") or "", j.get("published_date", ""))
    elif ats == "bamboohr":
        for j in payload.get("result", []):
            loc = j.get("location") or {}
            add((j.get("jobOpeningName") or ""), f"{loc.get('city','')} {loc.get('state','')}",
                f"https://{company}.bamboohr.com/careers/{j.get('id','')}", "")
    elif ats == "recruitee":
        for j in payload.get("offers", []):
            add(j.get("title"), j.get("location"), j.get("careers_url"), j.get("published_at", ""))
    elif ats in GENERIC_ATS:
        items = payload if isinstance(payload, list) else next(
            (payload[k] for k in LIST_KEYS
             if isinstance(payload, dict) and isinstance(payload.get(k), list)), [])
        for j in items:
            if not isinstance(j, dict):
                continue
            loc = first_of(j, LOC_KEYS)
            if isinstance(loc, dict):
                loc = loc.get("name") or loc.get("city") or ""
            add(first_of(j, TITLE_KEYS), str(loc or ""), first_of(j, URL_KEYS),
                str(first_of(j, DATE_KEYS) or ""))
    return out


def norm_xml(ats, company, text):
    """teamtailor RSS / personio XML — light regex parse, no extra deps."""
    out = []
    if ats == "teamtailor":
        for item in re.findall(r"<item>(.*?)</item>", text, re.S):
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
            u = re.search(r"<link>(.*?)</link>", item, re.S)
            if t:
                out.append({"company": company, "title": t.group(1).strip(), "location": "",
                            "url": u.group(1).strip() if u else "", "source": ats, "posted": ""})
    elif ats == "personio":
        for pos in re.findall(r"<position>(.*?)</position>", text, re.S):
            t = re.search(r"<name>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</name>", pos, re.S)
            o = re.search(r"<office>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</office>", pos, re.S)
            i = re.search(r"<id>(.*?)</id>", pos, re.S)
            if t:
                out.append({"company": company, "title": t.group(1).strip(),
                            "location": o.group(1).strip() if o else "",
                            "url": f"https://{company}.jobs.personio.com/job/{i.group(1)}" if i else "",
                            "source": ats, "posted": ""})
    return out


# ---------- sources ----------

def ats_rows():
    """Every GET-based ATS board we know about, from both discovery paths.

    sniff.py reads the token off the firm's own careers page (high confidence);
    discover.py guesses tokens against the same APIs. Both feed the same reader.
    Workday and Bullhorn are excluded — they need workday.py and feeds.py.
    """
    rows, seen = [], set()
    for path, has_url in (("sniffed.csv", False), ("endpoints.csv", True)):
        if not os.path.exists(path):
            continue
        for row in csv.DictReader(open(path)):
            ats, token = (row.get("ats") or "").strip(), (row.get("token") or "").strip()
            if ats not in ATS_ENDPOINT or not token:
                continue
            if (ats, token.lower()) in seen:
                continue
            seen.add((ats, token.lower()))
            url = row.get("url") if has_url else ""
            rows.append({"name": row["name"], "ats": ats, "token": token,
                         "url": url or ATS_ENDPOINT[ats].format(t=token)})
    return rows


def from_ats(session):
    rows = ats_rows()
    if not rows:
        print("no ATS endpoints known — run sniff.py then discover.py first",
              file=sys.stderr)
        return []
    jobs = []
    for i, row in enumerate(rows, 1):
        try:
            r = http_client.get(row["url"], sess=session)
            if r is None or r.status_code != 200:
                print(f"[{i}/{len(rows)}] {row['name']:<34} unreachable")
                continue
            if row["ats"] in ("teamtailor", "personio"):
                got = norm_xml(row["ats"], row["name"], http_client.text_of(r))
            else:
                payload = http_client.json_of(r)
                if payload is None:
                    print(f"[{i}/{len(rows)}] {row['name']:<34} non-JSON response "
                          f"(endpoint may have changed)")
                    continue
                got = norm(row["ats"], row["name"], payload)
            jobs += got
            print(f"[{i}/{len(rows)}] {row['name']:<34} {len(got):>4} raw")
        except Exception as e:
            print(f"  ! {row['name']}: {e}", file=sys.stderr)
    return jobs


def from_adzuna(cfg):
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        print("(skipping Adzuna — no API keys set)")
        return []
    jobs = []
    queries = ["commodity analyst", "trading analyst", "market analyst", "quantitative analyst",
               "energy analyst", "data scientist trading", "junior trader", "power gas analyst",
               "battery storage analyst", "electricity market analyst", "renewable energy analyst",
               "carbon markets analyst", "energy trading graduate", "market surveillance analyst",
               "price reporter commodities", "freight shipping analyst"]
    for q in queries:
        for page in (1, 2):
            url = f"https://api.adzuna.com/v1/api/jobs/gb/search/{page}"
            r = http_client.get(url, params={
                "app_id": app_id, "app_key": app_key, "what": q,
                "where": "london", "results_per_page": 50,
                "content-type": "application/json"})
            data = http_client.json_of(r)
            if data is None:
                if r is not None and r.status_code == 401:
                    print("  ! adzuna: 401 — check ADZUNA_APP_ID/ADZUNA_APP_KEY",
                          file=sys.stderr)
                break
            for j in data.get("results", []):
                jobs.append({
                    "company": (j.get("company") or {}).get("display_name", ""),
                    "title": j.get("title", ""),
                    "location": (j.get("location") or {}).get("display_name", ""),
                    "url": j.get("redirect_url", ""),
                    "source": "adzuna", "posted": j.get("created", ""),
                    "salary_min": j.get("salary_min"), "salary_max": j.get("salary_max"),
                    "currency": "GBP"})
    return jobs


# ---------- filtering ----------

# Rank words that a junior marker is allowed to override, and the markers that
# do it. Kept narrow on purpose: "Head of Trading" must stay out even though
# "Graduate" appearing anywhere in the blurb would be tempting to honour.
RANK = re.compile(r"manager|lead|\bstaff\b|\bexpert\b|principal", re.I)
JUNIOR = re.compile(r"\b(junior|jnr|trainee|graduate|entry.?level|assistant|"
                    r"apprentice\w*|intern|placement)\b", re.I)


def build_filter(cfg):
    inc = re.compile("|".join(cfg["include"]), re.I)
    exc = re.compile("|".join(cfg["exclude"]), re.I)
    loc = re.compile("|".join(cfg["locations"]), re.I)
    # Optional, so an older config.yaml still loads.
    role = re.compile("|".join(cfg["role_words"]), re.I) if cfg.get("role_words") else None
    dom = re.compile("|".join(cfg["domain_words"]), re.I) if cfg.get("domain_words") else None
    notloc = (re.compile("|".join(cfg["location_exclude"]), re.I)
              if cfg.get("location_exclude") else None)

    def matches(t):
        """Two ways in, because titles are written both ways round.

        The `include` phrases spell "<domain> analyst" and nothing else, so
        "Analyst, Global Markets" — the house style at most banks — fell
        straight through. The pair test catches the inverted form without
        needing a phrase for every combination: one role word plus one domain
        word, in any order. Neither half alone is enough.
        """
        if inc.search(t):
            return True
        if not (role and dom):
            return False
        r, d = role.search(t), dom.search(t)
        # Distinct spans, or a word appearing in both lists would satisfy the
        # pair on its own and quietly turn the test into a single-word match.
        return bool(r and d and r.span() != d.span())

    def keep(j):
        t = re.sub(r"<[^>]+>", " ", j["title"])
        if not matches(t):
            return False
        # A junior marker outranks a rank word: "Junior Portfolio Manager" and
        # "Trainee Broker Manager" are entry-level roles that the blanket
        # `manager` / `lead` excludes were throwing away. Only the rank words
        # are overridden — the subject-matter excludes (marketing, recruit,
        # back office) still apply, because those are the wrong job whoever
        # is doing it.
        hit = exc.search(t)
        if hit and not (JUNIOR.search(t) and RANK.fullmatch(hit.group(0).strip().lower())):
            return False
        # Somewhere-else wins over anywhere. Checked against title AND location:
        # "remote"/"hybrid" are legitimate keeps for UK roles, but they matched
        # "Remote - India" too, and postings with no location field routinely
        # name the city in the title instead.
        if notloc and notloc.search(f"{t} {j['location']}"):
            return False
        if j["location"] and not loc.search(j["location"]):
            return False
        return True

    keep.matches = matches          # so audits can separate title from location
    return keep


# ---------- audit ----------

def write_rejects(raw, hits, keep, path="rejects.csv"):
    """Record what the filter threw away, and why.

    This exists because of a real failure: 15,000 postings were scanned and
    ~215 kept, and there was no way to tell whether the other 14,785 were
    genuinely irrelevant or whether the patterns were too narrow. They were
    too narrow — half of a 103-title sample of ordinary front-office roles was
    being rejected. A filter with no record of its rejections cannot be
    audited, and an unauditable filter is one you end up trusting on faith.

    Deduped by title so it stays readable: the same title from forty firms is
    one line with a count, not forty lines.
    """
    kept_ids = {id(j) for j in hits}
    counts, why = {}, {}
    for j in raw:
        if id(j) in kept_ids:
            continue
        t = re.sub(r"<[^>]+>", " ", j.get("title", "")).strip()
        if not t:
            continue
        counts[t] = counts.get(t, 0) + 1
        if t not in why:
            why[t] = "title: no pattern matched" if not keep.matches(t) else "location"
    try:
        with open(path, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["count", "title", "reason"])
            for t, n in sorted(counts.items(), key=lambda x: (-x[1], x[0])):
                w.writerow([n, t, why[t]])
        print(f"wrote {path}: {len(counts)} distinct rejected titles "
              f"({sum(counts.values())} postings)")
    except OSError as e:
        print(f"  ! could not write {path}: {e}", file=sys.stderr)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(open("config.yaml"))
    keep = build_filter(cfg)
    con = db_init()
    session = http_client.session()

    raw = from_ats(session) + from_adzuna(cfg)
    hits = [j for j in raw if keep(j)]
    write_rejects(raw, hits, keep)
    new = store.save_new(con, hits)

    if args.all:
        store.report(hits, len(raw), len(hits))
    else:
        store.report(new, len(raw), len(hits))

    with open("latest.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "title", "location", "url", "source", "posted"])
        w.writeheader()
        w.writerows(hits)
    print("\nwrote latest.csv and jobs.db")


if __name__ == "__main__":
    main()
