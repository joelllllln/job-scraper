#!/usr/bin/env python3
"""
add_firms.py — append firms to firms.csv without corrupting it.

Adding by hand is how the registry grows, and there are exactly two ways to get
it wrong, both of which are quiet:

  a repeated name    — the same firm scraped twice
  a repeated domain  — worse. One domain is one careers board, so two names on
                       it means the same vacancies collected twice and filed
                       under two companies. Because the dedupe key includes the
                       company, the role then appears twice in the digest.

Both are refused here rather than discovered later.

    python add_firms.py new.csv           # name,category,domain per line
    python add_firms.py new.csv --dry-run
"""

import argparse
import csv
import os
import sys

FIELDS = ["name", "category", "domain"]


def load(path="firms.csv"):
    if not os.path.exists(path):
        return []
    return list(csv.DictReader(open(path)))


def add(candidates, existing):
    """Returns (accepted, rejected) — rejected carries the reason."""
    names = {(r["name"] or "").strip().lower() for r in existing}
    domains = {(r["domain"] or "").strip().lower() for r in existing if (r["domain"] or "").strip()}
    accepted, rejected = [], []
    for row in candidates:
        name = (row.get("name") or "").strip()
        domain = (row.get("domain") or "").strip().lower()
        category = (row.get("category") or "unknown").strip()
        if not name:
            rejected.append((row, "no name"))
            continue
        if name.lower() in names:
            rejected.append((row, "duplicate name"))
            continue
        if domain and domain in domains:
            rejected.append((row, "domain already claimed"))
            continue
        names.add(name.lower())
        if domain:
            domains.add(domain)
        accepted.append({"name": name, "category": category, "domain": domain})
    return accepted, rejected


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="CSV of name,category,domain (header optional)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = [r for r in csv.reader(open(args.source)) if r and r[0].strip()]
    if rows and rows[0][0].strip().lower() == "name":
        rows = rows[1:]
    candidates = [{"name": r[0], "category": r[1] if len(r) > 1 else "unknown",
                   "domain": r[2] if len(r) > 2 else ""} for r in rows]

    existing = load()
    accepted, rejected = add(candidates, existing)

    print(f"{len(candidates)} candidates: {len(accepted)} new, {len(rejected)} rejected")
    for row, why in rejected[:15]:
        print(f"  - {row.get('name', '')[:38]:<40} {why}")
    if len(rejected) > 15:
        print(f"  ... and {len(rejected) - 15} more")

    if args.dry_run or not accepted:
        return
    with open("firms.csv", "a", newline="") as fh:
        csv.DictWriter(fh, fieldnames=FIELDS).writerows(accepted)
    print(f"\nfirms.csv: {len(existing)} -> {len(existing) + len(accepted)}")
    print("run check_firms.py to confirm the new domains really belong to them")


if __name__ == "__main__":
    main()
