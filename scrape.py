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
}


# ---------- storage ----------

def db_init():
    """Kept as the shared entry point — schema and dedupe now live in store.py."""
    return store.connect(DB)


def job_id(company, title, location=""):
    return store.canonical_key(company, title)


# ---------- normalisers, one per ATS ----------

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
               "energy analyst", "data scientist trading", "junior trader", "power gas analyst"]
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

def build_filter(cfg):
    inc = re.compile("|".join(cfg["include"]), re.I)
    exc = re.compile("|".join(cfg["exclude"]), re.I)
    loc = re.compile("|".join(cfg["locations"]), re.I)

    def keep(j):
        t = re.sub(r"<[^>]+>", " ", j["title"])
        if not inc.search(t) or exc.search(t):
            return False
        if j["location"] and not loc.search(j["location"]):
            return False
        return True
    return keep


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
