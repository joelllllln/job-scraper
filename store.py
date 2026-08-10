#!/usr/bin/env python3
"""
store.py — one place where jobs get written.

Every collector (scrape, boards, workday, feeds, efc) goes through save_new().
That gives three things the collectors used to each get slightly wrong:

1. **A canonical dedupe key.** "Analyst, Crude" and "Crude Analyst" are the same
   job. Exact string keys miss that; this sorts the significant title tokens so
   word order and punctuation stop mattering.

2. **Source preference.** If the same role turns up on both a firm's Greenhouse
   board and on Indeed, the Greenhouse row wins and the stored URL is upgraded to
   the direct one — so you apply direct instead of through an agency, even if the
   aggregator found it first.

3. **Salary, where the source gives it.** Adzuna, Reed and Ashby all return
   compensation. Sparse, but after a few months it's a real picture of what these
   roles pay in London, which is otherwise very hard to find.
"""

import hashlib
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone

import http_client

DB = "jobs.db"

# Higher wins when the same job arrives from two places.
SOURCE_RANK = {
    "greenhouse": 10, "lever": 10, "ashby": 10, "workday": 10,
    "smartrecruiters": 9, "workable": 9, "recruitee": 9, "teamtailor": 9,
    "personio": 9, "breezy": 9, "bamboohr": 9,
    "rippling": 9, "pinpoint": 9, "comeet": 9, "jobvite": 9,
    "efinancialcareers": 4, "bullhorn": 4, "linkedin": 3,
    "reed": 2, "adzuna": 2, "google": 2, "jooble": 2, "careerjet": 2,
    "indeed": 1, "glassdoor": 1,
}

NOISE = {"the", "a", "an", "of", "and", "for", "to", "in", "at", "on", "with",
         "role", "job", "vacancy", "position", "opportunity", "m", "f", "d",
         "ltd", "limited", "llp", "plc", "group", "uk", "london", "inc", "llc",
         "sa", "ag", "bv", "gmbh", "holdings", "international", "partners"}


def _tokens(s):
    return sorted(t for t in re.findall(r"[a-z0-9]+", (s or "").lower())
                  if t not in NOISE and len(t) > 1)


def canonical_key(company, title):
    """Word-order-independent identity for a job."""
    c = "".join(_tokens(company))
    t = " ".join(_tokens(title))
    return hashlib.sha1(f"{c}|{t}".encode()).hexdigest()[:16]


def parse_salary(j):
    """Normalise whatever the source gave us into (min, max, currency)."""
    lo = j.get("salary_min")
    hi = j.get("salary_max")
    cur = j.get("currency") or ("GBP" if (lo or hi) else "")
    if lo is None and hi is None:
        text = j.get("salary_text") or ""
        nums = [float(n.replace(",", "")) for n in re.findall(r"[\d,]{4,9}", text)]
        nums = [n for n in nums if 10000 <= n <= 1000000]
        if nums:
            lo, hi = min(nums), max(nums)
            cur = "GBP" if "£" in text else cur
    return (float(lo) if lo else None, float(hi) if hi else None, cur)


MAX_TITLE = 300
MAX_URL = 1000
# If a single run tries to insert more than this, something upstream broke —
# a filter regex went permissive, or a source started returning a full dump.
SANITY_INSERT_LIMIT = 800


def backup(path=DB, keep=5):
    """Copy the db before a run. SQLite corruption is rare; a bad write isn't."""
    if not os.path.exists(path):
        return None
    os.makedirs("backups", exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    dest = os.path.join("backups", f"jobs-{stamp}.db")
    try:
        src = sqlite3.connect(path)
        dst = sqlite3.connect(dest)
        with dst:
            src.backup(dst)          # consistent even if another process is writing
        src.close()
        dst.close()
    except Exception:
        shutil.copy2(path, dest)
    olds = sorted(f for f in os.listdir("backups") if f.startswith("jobs-"))
    for f in olds[:-keep]:
        try:
            os.remove(os.path.join("backups", f))
        except OSError:
            pass
    return dest


def valid_job(j):
    """Reject rows that would poison the database. Returns (ok, reason)."""
    title = (j.get("title") or "").strip()
    if not title:
        return False, "empty title"
    if len(title) > MAX_TITLE:
        return False, "title absurdly long"
    if re.search(r"<[a-z/!]", title, re.I):
        return False, "title contains markup"
    if not re.search(r"[a-zA-Z]{3}", title):
        return False, "title has no words"
    url = (j.get("url") or "").strip()
    if url and (len(url) > MAX_URL or not url.startswith(("http://", "https://"))):
        return False, "bad url"
    if not (j.get("company") or "").strip():
        return False, "no company"
    return True, ""


def connect(path=DB):
    con = sqlite3.connect(path, timeout=60)
    # WAL lets the weekly run read while a collector writes, and survives a kill
    # mid-write far better than the default rollback journal.
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA synchronous=NORMAL")
        con.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    con.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            company TEXT, title TEXT, location TEXT,
            url TEXT, source TEXT, posted TEXT, first_seen TEXT
        )""")
    con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ran_at TEXT, n_new INTEGER)")
    have = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    for col, decl in [("status", "TEXT DEFAULT 'new'"), ("note", "TEXT DEFAULT ''"),
                      ("salary_min", "REAL"), ("salary_max", "REAL"),
                      ("currency", "TEXT DEFAULT ''"), ("seen_count", "INTEGER DEFAULT 1")]:
        if col not in have:
            con.execute(f"ALTER TABLE jobs ADD COLUMN {col} {decl}")
    con.execute("CREATE INDEX IF NOT EXISTS idx_jobs_first_seen ON jobs(first_seen)")
    con.commit()
    return con


def save_new(con, jobs, sanity_limit=SANITY_INSERT_LIMIT):
    """Insert jobs we haven't got, upgrade the ones we have. Returns the new ones.

    Never raises: a malformed row is dropped with a reason, not allowed to kill a
    run that has already done twenty minutes of work.
    """
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new, rejected = [], {}

    for j in jobs:
        # normalise before anything else — sources return entities and stray tags
        j = dict(j)
        # clean at a generous limit so valid_job can still see and reject the
        # absurdly long ones rather than silently truncating them to look fine
        for f in ("company", "title", "location"):
            j[f] = http_client.clean_text(j.get(f), 600)
        j["url"] = (j.get("url") or "").strip()

        ok, why = valid_job(j)
        if not ok:
            rejected[why] = rejected.get(why, 0) + 1
            continue

        jid = canonical_key(j.get("company"), j.get("title"))
        lo, hi, cur = parse_salary(j)
        try:
            row = con.execute("SELECT source, url, salary_min FROM jobs WHERE id=?",
                              (jid,)).fetchone()
        except sqlite3.Error as e:
            rejected[f"db: {e}"] = rejected.get(f"db: {e}", 0) + 1
            continue

        if row is None:
          try:
            con.execute("""INSERT INTO jobs
                (id, company, title, location, url, source, posted, first_seen,
                 status, note, salary_min, salary_max, currency, seen_count)
                VALUES (?,?,?,?,?,?,?,?,'new','',?,?,?,1)""",
                (jid, j.get("company", ""), j.get("title", ""), j.get("location", ""),
                 j.get("url", ""), j.get("source", ""), j.get("posted", ""), now, lo, hi, cur))
            new.append(dict(j, id=jid))
          except sqlite3.Error as e:
            rejected[f"insert: {type(e).__name__}"] = rejected.get(f"insert: {type(e).__name__}", 0) + 1
          continue

        # already known — keep the better provenance, and fill any gaps
        old_source, old_url, old_sal = row
        try:
          con.execute("UPDATE jobs SET seen_count = COALESCE(seen_count,1) + 1 WHERE id=?", (jid,))
          if SOURCE_RANK.get(j.get("source", ""), 0) > SOURCE_RANK.get(old_source or "", 0):
            con.execute("UPDATE jobs SET source=?, url=? WHERE id=?",
                        (j.get("source", ""), j.get("url", "") or old_url, jid))
          if old_sal is None and lo is not None:
            con.execute("UPDATE jobs SET salary_min=?, salary_max=?, currency=? WHERE id=?",
                        (lo, hi, cur, jid))
        except sqlite3.Error:
            rejected["update failed"] = rejected.get("update failed", 0) + 1
    try:
        con.commit()
    except sqlite3.Error as e:
        print(f"  ! commit failed: {e}")
        con.rollback()
        return []

    if rejected:
        detail = ", ".join(f"{v} {k}" for k, v in sorted(rejected.items(), key=lambda x: -x[1]))
        print(f"  dropped {sum(rejected.values())} malformed rows ({detail})")
    if len(new) > sanity_limit:
        print(f"  !! {len(new)} new rows in one run — that is far above normal.")
        print("     Check config.yaml include patterns before trusting this digest.")
    return new


def report(new, raw_count, matched_count):
    print(f"\n{raw_count} scraped -> {matched_count} matched -> {len(new)} new\n")
    for j in sorted(new, key=lambda x: x.get("company", "")):
        print(f"  {j.get('company','')[:30]:<32} {j.get('title','')[:56]:<58} "
              f"{j.get('location','')[:22]}")
        if j.get("url"):
            print(f"    {j['url']}")
