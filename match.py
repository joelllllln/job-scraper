#!/usr/bin/env python3
"""
match.py — score each job against your actual CV, not just keywords.

score.py ranks on the rubric. This ranks on you: it compares the stored job
description against your CV and reports overlap, what the JD asks for that your
CV doesn't mention, and the specific lines of your CV worth leading with.

    cp your_cv.txt cv.txt          # plain text, whole CV, no formatting needed
    python match.py                # top 15 by fit
    python match.py --job kpler    # detail on one role
    python match.py --min-score 60

Pure local TF-IDF-ish scoring, no API needed. If ANTHROPIC_API_KEY is set,
--draft will also write an opening line per role, grounded in your CV.
"""

import argparse
import math
import os
import env
import re
import sqlite3
from collections import Counter

DB = "jobs.db"
CV = "cv.txt"
MODEL = "claude-sonnet-5"        # --draft only; everything else is local

STOP = set("""a an the and or of to in for with on at by from as is are was were be been being
this that these those it its our your their his her we you they i me my will would can could
should must have has had do does did not no if then than but so such more most other some any
all each both few many much very own same via per within across into out up down over under
role job work working experience team teams year years including include includes required
requirements responsibilities candidate candidates ideal strong good great excellent ability
able across using use used help support ensure new plus etc""".split())


def toks(text):
    raw = re.findall(r"[a-z][a-z+#.]{2,}", (text or "").lower())
    out = []
    for w in raw:
        w = w.rstrip(".")            # "abuse." -> "abuse", but keep "node.js", "c++"
        if len(w) > 2 and w not in STOP:
            out.append(w)
    return out


def vec(text):
    c = Counter(toks(text))
    return c


def cosine(a, b, idf):
    if not a or not b:
        return 0.0
    keys = set(a) & set(b)
    num = sum(a[k] * b[k] * idf.get(k, 1.0) ** 2 for k in keys)
    da = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in a.items()))
    db = math.sqrt(sum((v * idf.get(k, 1.0)) ** 2 for k, v in b.items()))
    return num / (da * db) if da and db else 0.0


def build_idf(docs):
    n = len(docs) or 1
    df = Counter()
    for d in docs:
        df.update(set(d))
    return {w: math.log(1 + n / (1 + c)) for w, c in df.items()}


def best_cv_lines(cv_text, jd_tokens, k=3):
    """Which lines of the CV are most relevant to this JD."""
    lines = [l.strip() for l in cv_text.splitlines() if len(l.strip()) > 30]
    scored = []
    jd = set(jd_tokens)
    for l in lines:
        overlap = len(set(toks(l)) & jd)
        if overlap:
            scored.append((overlap, l))
    scored.sort(reverse=True)
    return [l for _, l in scored[:k]]


def draft_line(cv_text, job, gaps):
    """Optional: one tailored opening sentence via the API."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        return None
    import http_client
    r = http_client.post_json(
        "https://api.anthropic.com/v1/messages",
        {"model": MODEL, "max_tokens": 300,
         "messages": [{"role": "user", "content":
             f"Here is a CV:\n\n{cv_text[:6000]}\n\n"
             f"Here is a job: {job['title']} at {job['company']}.\n"
             f"Description:\n{(job['description'] or '')[:4000]}\n\n"
             "Write two sentences the applicant could open a cover note "
             "with. Ground both in specifics from the CV. No flattery, "
             "no 'I am excited to'. Plain, direct, factual."}]},
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    data = http_client.json_of(r)
    if data is None:
        return None
    return "".join(b.get("text", "") for b in data.get("content", [])
                   if b.get("type") == "text").strip() or None


def main():
    env.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", help="filter to one company or title")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--min-score", type=int, default=0)
    ap.add_argument("--draft", action="store_true", help="also draft an opening line (needs API key)")
    args = ap.parse_args()

    if not os.path.exists(CV):
        print(f"No {CV} found. Save your CV as plain text there and rerun.")
        return
    cv_text = open(CV, encoding="utf-8", errors="ignore").read()
    cv_v = vec(cv_text)

    if not os.path.exists(DB):
        print("no database yet — run the collectors first")
        return
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("""
        SELECT j.company, j.title, j.location, j.url, j.status, v.description
        FROM jobs j JOIN verify v ON v.id = j.id
        WHERE v.live = 1 AND COALESCE(j.status,'new') = 'new'
          AND LENGTH(COALESCE(v.description,'')) > 200
        """)]
    except sqlite3.Error:
        print("no verified jobs yet — run verify.py first")
        return
    if not rows:
        print("no verified jobs with descriptions yet — run verify.py")
        return

    if args.job:
        q = args.job.lower()
        rows = [r for r in rows if q in r["company"].lower() or q in r["title"].lower()]

    docs = [vec(r["description"]) for r in rows]
    idf = build_idf(docs + [cv_v])

    raws = [cosine(cv_v, d, idf) for d in docs]
    best = max(raws) or 1.0
    for r, d, raw in zip(rows, docs, raws):
        # relative to the strongest match in this batch — a ranking, not a percentage
        r["fit"] = round(100 * raw / best)
        jd_top = [w for w, _ in d.most_common(60)]
        r["gaps"] = [w for w in jd_top if w not in cv_v][:8]
        r["leads"] = best_cv_lines(cv_text, jd_top)
        r["_doc"] = d

    rows = [r for r in rows if r["fit"] >= args.min_score]
    rows.sort(key=lambda r: -r["fit"])

    print(f"CV fit across {len(rows)} live roles "
          f"(100 = closest match in this batch, not a percentage)\n")
    for r in rows[:args.top]:
        print(f"  {r['fit']:>3}  {r['company'][:26]:<28} {r['title'][:44]}")
    print()

    detail = rows[:5] if not args.job else rows[:3]
    for r in detail:
        print("─" * 72)
        print(f"{r['fit']}  {r['title']} · {r['company']}")
        print(f"   {r['url']}")
        if r["leads"]:
            print("\n   lead with:")
            for l in r["leads"]:
                print(f"     · {l[:110]}")
        if r["gaps"]:
            print(f"\n   JD emphasises, CV doesn't mention: {', '.join(r['gaps'])}")
        if args.draft:
            d = draft_line(cv_text, r, r["gaps"])
            if d:
                print(f"\n   opening:\n     {d}")
        print()

    if not args.draft:
        print("Add --draft with ANTHROPIC_API_KEY set to also get an opening line per role.")


if __name__ == "__main__":
    main()
