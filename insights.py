#!/usr/bin/env python3
"""
insights.py — what the accumulated data tells you that no job board will.

After a couple of months the database stops being a job list and starts being a
dataset about the London commodities hiring market. This reads it.

    python insights.py

Four views:

  Hiring velocity   which firms post continuously vs which posted once and went
                    quiet. Continuous posters are where to concentrate — they
                    have real churn and will have another opening soon.
  Salary picture    what these roles actually pay, from whatever sources
                    disclosed it. Sparse but honest.
  Title drift       which titles are actually used for the work you want, which
                    is how you should be phrasing your CV and searches.
  Funnel            what you're applying to versus what you're seeing.
"""

import os
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

DB = "jobs.db"


def age_days(s):
    if not s:
        return None
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return None


def bar(n, total, width=22):
    if not total:
        return ""
    return "█" * max(1, round(width * n / total)) if n else ""


def main():
    if not os.path.exists(DB):
        print("no database yet — run the collectors first")
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("SELECT * FROM jobs")]
    except sqlite3.Error:
        print("database has no jobs table yet — run the collectors first")
        return
    if not rows:
        print("no data yet — run the pipeline first")
        return

    span = [age_days(r["first_seen"]) for r in rows]
    span = [d for d in span if d is not None]
    weeks = max(1, (max(span) - min(span)) // 7) if span else 1
    print(f"{len(rows)} jobs collected over ~{weeks} week(s)\n")

    # --- hiring velocity ---
    per_firm = defaultdict(list)
    for r in rows:
        d = age_days(r["first_seen"])
        if d is not None:
            per_firm[r["company"]].append(d)

    def spread(days):
        return (max(days) - min(days)) if len(days) > 1 else 0

    ranked = sorted(per_firm.items(),
                    key=lambda kv: (-len(kv[1]), -spread(kv[1])))[:18]
    top = ranked[0][1] if ranked else []
    print("HIRING VELOCITY — posts, and over how many days")
    for firm, days in ranked:
        window = spread(days)
        tag = "continuous" if len(days) >= 3 and window >= 30 else \
              "burst" if len(days) >= 3 else ""
        print(f"  {firm[:30]:<32} {len(days):>3}  {bar(len(days), len(top) or 1):<24} {tag}")

    # --- salary ---
    sal = [(r["salary_min"], r["salary_max"], r["title"]) for r in rows
           if r["salary_min"]]
    print(f"\nSALARY — disclosed on {len(sal)} of {len(rows)} ({100*len(sal)//max(1,len(rows))}%)")
    if sal:
        mids = sorted((lo + (hi or lo)) / 2 for lo, hi, _ in sal)
        def pct(p):
            return mids[min(len(mids) - 1, int(len(mids) * p))]
        print(f"  median   £{pct(0.5):,.0f}")
        print(f"  p25-p75  £{pct(0.25):,.0f} - £{pct(0.75):,.0f}")
        print(f"  range    £{mids[0]:,.0f} - £{mids[-1]:,.0f}")
        junior = [m for (lo, hi, t), m in zip(sal, mids)
                  if re.search(r"junior|graduate|trainee|analyst", t or "", re.I)]
        if junior:
            print(f"  analyst/junior titles only: median £{sorted(junior)[len(junior)//2]:,.0f}")
    else:
        print("  nothing disclosed yet — Reed and Adzuna are the sources most likely to")

    # --- title drift ---
    words = Counter()
    for r in rows:
        for w in re.findall(r"[A-Za-z]{4,}", r["title"] or ""):
            wl = w.lower()
            if wl not in {"analyst", "with", "team", "role", "join", "london"}:
                words[wl] += 1
    print("\nTITLE LANGUAGE — how these roles are actually named")
    for w, n in words.most_common(14):
        print(f"  {w:<20} {n:>4}  {bar(n, words.most_common(1)[0][1])}")

    # --- funnel ---
    print("\nFUNNEL")
    for r in con.execute("""SELECT COALESCE(status,'new') s, COUNT(*) n FROM jobs
                            GROUP BY s ORDER BY n DESC"""):
        print(f"  {r['n']:>5}  {r['s']}")

    dupes = con.execute("SELECT COUNT(*) FROM jobs WHERE COALESCE(seen_count,1) > 1").fetchone()[0]
    print(f"\n  {dupes} roles were found by more than one source "
          f"(dedupe kept the best apply link)")

    print("\nWhat to do with this: concentrate on the continuous posters — they have")
    print("real churn and will have another opening within weeks. The one-off posters")
    print("are worth a speculative approach instead of waiting for a listing.")


if __name__ == "__main__":
    main()
