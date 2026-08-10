#!/usr/bin/env python3
"""
companies_house.py — find the firms that aren't on any job board.

The 5-30 person shops around the City and Mayfair have no careers page, no
Greenhouse, and never appear on LinkedIn. They exist in exactly one public place:
the Companies House register. This queries it by SIC code and location, filters
to plausible trading firms, and appends them to firms.csv so sniff.py picks them
up on the next run.

Free API key: https://developer.company-information.service.gov.uk/

    export CH_API_KEY=...
    python companies_house.py --preview        # show what it found, write nothing
    python companies_house.py --append         # add new firms to firms.csv

SIC codes used:
    46719  wholesale of other intermediate products (how most physical trading
           houses register — this is the big one)
    64999  financial intermediation not elsewhere classified (funds, prop shops)
    66120  security and commodity contracts dealing
    35140  trade of electricity
    35230  trade of gas through mains
    46711  wholesale of petroleum and petroleum products
"""

import argparse
import csv
import os
import re
import sys
import time

import http_client

BASE = "https://api.company-information.service.gov.uk"
SIC = {
    "46719": "trading_house",
    "46711": "refining",
    "64999": "fund",
    "66120": "broker",
    "35140": "power_gas",
    "35230": "power_gas",
}
LONDON_POSTCODES = re.compile(r"^(EC|WC|E1|E14|W1|SW1|SE1|N1|NW1)", re.I)

# Names that are obviously not what we're after
JUNK = re.compile(r"nominee|trustee|dormant|holdings? (no|number)|property|estate|"
                  r"restaurant|construction|cleaning|recruit|consult(ing|ancy)? services",
                  re.I)

STRIP = re.compile(r"\s+(limited|ltd\.?|llp|plc|uk|holdings|group|international)\b\.?$", re.I)


def search(key, sic, size=100, pages=5):
    out, start = [], 0
    for _ in range(pages):
        r = http_client.get(f"{BASE}/advanced-search/companies", auth=(key, ""),
                            params={"sic_codes": sic, "company_status": "active",
                                    "size": size, "start_index": start})
        if r is None:
            print(f"  ! {sic}: unreachable", file=sys.stderr)
            break
        if r.status_code == 401:
            print("401 — check CH_API_KEY", file=sys.stderr)
            return out
        data = http_client.json_of(r)
        if data is None:
            break
        items = data.get("items", [])
        out += items
        if len(items) < size:
            break
        start += size
        time.sleep(0.6)          # CH allows 600 requests per 5 minutes
    return out


def domain_guess(name):
    """CH doesn't hold websites. Leave blank — sniff.py can't use a wrong guess."""
    return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--append", action="store_true")
    ap.add_argument("--min-year", type=int, default=2005)
    args = ap.parse_args()

    key = os.getenv("CH_API_KEY")
    if not key:
        print("CH_API_KEY not set — free key at "
              "developer.company-information.service.gov.uk", file=sys.stderr)
        return

    existing = set()
    try:
        for r in csv.DictReader(open("firms.csv")):
            existing.add(STRIP.sub("", r["name"]).strip().lower())
    except FileNotFoundError:
        pass

    found = {}
    for sic, category in SIC.items():
        items = search(key, sic)
        print(f"  SIC {sic}: {len(items)} active companies")
        for it in items:
            name = (it.get("company_name") or "").strip()
            addr = it.get("registered_office_address") or {}
            pc = (addr.get("postal_code") or "").strip()
            locality = (addr.get("locality") or "").lower()
            if "london" not in locality and not LONDON_POSTCODES.match(pc):
                continue
            if JUNK.search(name):
                continue
            created = (it.get("date_of_creation") or "")[:4]
            if created and created.isdigit() and int(created) < args.min_year:
                continue
            clean = STRIP.sub("", name.title()).strip()
            if clean.lower() in existing or clean.lower() in found:
                continue
            found[clean.lower()] = {"name": clean, "category": category,
                                    "domain": domain_guess(clean),
                                    "sic": sic, "incorporated": created, "postcode": pc}
        time.sleep(0.6)

    rows = sorted(found.values(), key=lambda r: r["name"])
    print(f"\n{len(rows)} candidate firms not already in firms.csv\n")
    for r in rows[:40]:
        print(f"  {r['name'][:44]:<46} {r['category']:<14} {r['postcode']:<9} {r['incorporated']}")
    if len(rows) > 40:
        print(f"  ... and {len(rows) - 40} more")

    with open("companies_house.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain", "sic",
                                           "incorporated", "postcode"])
        w.writeheader()
        w.writerows(rows)
    print("\nwrote companies_house.csv")

    if args.append:
        with open("firms.csv", "a", newline="") as fh:
            w = csv.writer(fh)
            for r in rows:
                w.writerow([r["name"], r["category"], r["domain"]])
        print(f"appended {len(rows)} firms to firms.csv")
        print("domains are blank — fill the ones you care about, then rerun sniff.py")
    elif not args.preview:
        print("\nrun again with --append to add these to firms.csv")


if __name__ == "__main__":
    main()
