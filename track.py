#!/usr/bin/env python3
"""
track.py — mark what you've done with a job so it stops resurfacing.

Weekly running only works if the pile shrinks. A role you applied to, or decided
against, should never appear in another digest.

    python track.py list                        # what's open, ranked
    python track.py applied kpler market        # fuzzy match on company + title
    python track.py ignored "goldman"
    python track.py reopen kpler                # back to 'new'
    python track.py stats                       # pipeline summary
"""

import os
import sqlite3
import sys

DB = "jobs.db"
STATUSES = ("new", "applied", "ignored", "interviewing", "rejected", "offer")


def con():
    if not os.path.exists(DB):
        print("no database yet — run the collectors first")
        sys.exit(1)
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ran_at TEXT, n_new INTEGER)")
    cols = [r[1] for r in c.execute("PRAGMA table_info(jobs)")]
    if "status" not in cols:
        c.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
    if "note" not in cols:
        c.execute("ALTER TABLE jobs ADD COLUMN note TEXT DEFAULT ''")
    c.commit()
    return c


def match(c, terms):
    where = " AND ".join(["(LOWER(company) LIKE ? OR LOWER(title) LIKE ?)"] * len(terms))
    params = []
    for t in terms:
        params += [f"%{t.lower()}%", f"%{t.lower()}%"]
    return c.execute(f"SELECT id, company, title, status FROM jobs WHERE {where}", params).fetchall()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    c = con()
    cmd = sys.argv[1].lower()

    if cmd == "list":
        try:
            rows = c.execute("""SELECT company, title, location, url, status FROM jobs
                                WHERE COALESCE(status,'new')='new'
                                ORDER BY first_seen DESC LIMIT 60""").fetchall()
        except sqlite3.Error:
            print("no jobs table yet — run the collectors first")
            return
        if not rows:
            print("nothing open. Either you've worked through everything, or no run "
                  "has collected yet — try ./weekly.sh")
            return
        for r in rows:
            print(f"  {r['company'][:26]:<28} {r['title'][:46]:<48} {r['location'][:20]}")
        return

    if cmd == "stats":
        try:
            c.execute("SELECT 1 FROM jobs LIMIT 1")
        except sqlite3.Error:
            print("no jobs table yet — run the collectors first")
            return
        for r in c.execute("""SELECT COALESCE(status,'new') s, COUNT(*) n FROM jobs
                              GROUP BY s ORDER BY n DESC"""):
            print(f"  {r['n']:>5}  {r['s']}")
        last = c.execute("SELECT ran_at, n_new FROM runs ORDER BY id DESC LIMIT 5").fetchall()
        if last:
            print("\n  recent runs:")
            for r in last:
                print(f"    {r['ran_at'][:16]}  {r['n_new']} new")
        return

    if cmd == "reopen":
        cmd = "new"
    if cmd not in STATUSES:
        print(f"status must be one of: {', '.join(STATUSES)}")
        return

    terms = sys.argv[2:]
    if not terms:
        print("give at least one search term")
        return

    hits = match(c, terms)
    if not hits:
        print("no match")
        return
    if len(hits) > 1:
        print(f"{len(hits)} matches — narrow it down or confirm all:")
        for h in hits:
            print(f"  {h['company'][:26]:<28} {h['title'][:44]:<46} [{h['status'] or 'new'}]")
        if input(f"\nmark all {len(hits)} as '{cmd}'? [y/N] ").strip().lower() != "y":
            return

    c.executemany("UPDATE jobs SET status=? WHERE id=?", [(cmd, h["id"]) for h in hits])
    c.commit()
    print(f"marked {len(hits)} as {cmd}")


if __name__ == "__main__":
    main()
