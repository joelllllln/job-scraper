#!/usr/bin/env python3
"""
inbox.py — ingest roles collected somewhere else.

Railway scrapes LinkedIn (which GitHub Actions cannot reach) and pushes what it
finds to linkedin_inbox.csv. This reads that file into the one real database,
where dedupe, verification, scoring and the digest all happen exactly as they
do for every other source. Nothing here is LinkedIn-specific beyond the default
filename — any out-of-band collector can drop a CSV with the same columns.

    python inbox.py                       # ingest linkedin_inbox.csv
    python inbox.py --file other.csv
    python inbox.py --keep                # do not clear the file afterwards

The file is truncated once its rows are in, so it cannot grow without bound and
a role cannot be re-ingested every week for the rest of time. store.save_new
would collapse the duplicates anyway, but "already known" bumps seen_count,
which is a real signal and should not be inflated by re-reading the same file.
"""

import argparse
import csv
import os
import env
import sys

import yaml

import store
from scrape import build_filter, db_init

FIELDS = ["company", "title", "location", "url", "source", "posted"]


def read(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if (r.get("url") or "").strip()]
    return [{k: (r.get(k) or "").strip() for k in FIELDS} for r in rows]


def main():
    env.load()
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="linkedin_inbox.csv")
    ap.add_argument("--keep", action="store_true",
                    help="leave the file in place instead of clearing it")
    args = ap.parse_args()

    rows = read(args.file)
    if not rows:
        print(f"{args.file}: nothing to ingest")
        return 0

    # Filter again here even though the collector already did. The file arrives
    # from another machine running another checkout, and config.yaml moves: a
    # role that passed when it was collected is not necessarily one you want by
    # the time it lands. Whatever wrote the file, this is the gate into the
    # database, so this is where the current rules have to be applied.
    keep = build_filter(yaml.safe_load(open("config.yaml", encoding="utf-8")))
    hits = [j for j in rows if keep(j)]
    if len(hits) != len(rows):
        print(f"{len(rows) - len(hits)} of {len(rows)} inbox rows no longer match the filter")

    con = db_init()
    new = store.save_new(con, hits)
    store.report(new, len(rows), len(hits))

    if not args.keep:
        try:
            with open(args.file, "w", newline="", encoding="utf-8") as fh:
                csv.DictWriter(fh, fieldnames=FIELDS).writeheader()
        except OSError as e:
            print(f"  ! could not clear {args.file}: {e}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
