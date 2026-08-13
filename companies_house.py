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

# The whole London finance and energy register, by SIC code. This is the only
# complete list that exists — no job board covers the 5-30 person shops, and
# most of them have no careers page at all. They come out with a BLANK domain
# on purpose: Companies House does not hold websites, and a guessed one is
# worse than none, because sniff.py would then go and read somebody else's site.
SIC = {
    # --- energy: extraction, refining, generation, supply, trade ---
    "06100": "major",          # extraction of crude petroleum
    "06200": "major",          # extraction of natural gas
    "09100": "major",          # support activities for petroleum and gas
    "19201": "refining",       # mineral oil refining
    "19209": "refining",       # other petroleum products
    "35110": "power_gas",      # production of electricity
    "35120": "power_gas",      # transmission of electricity
    "35130": "power_gas",      # distribution of electricity
    "35140": "power_gas",      # trade of electricity
    "35210": "power_gas",      # manufacture of gas
    "35220": "power_gas",      # distribution of gaseous fuels through mains
    "35230": "power_gas",      # trade of gas through mains
    "35300": "power_gas",      # steam and air conditioning supply
    "46120": "trading_house",  # agents in fuels, ores, metals, chemicals
    "46711": "refining",       # wholesale of petroleum products
    "46719": "trading_house",  # wholesale of other intermediate products
    "49500": "power_gas",      # transport via pipeline
    # --- finance: markets, dealing, funds, management ---
    "64191": "bank",           # banks
    "64205": "fund",           # financial services holding companies
    "64301": "fund",           # activities of investment trusts
    "64302": "fund",           # activities of unit trusts
    "64303": "fund",           # activities of venture and development capital
    "64304": "fund",           # activities of open-ended investment companies
    "64305": "fund",           # activities of property unit trusts
    "64306": "fund",           # activities of investment trusts, other
    "64992": "fund",           # factoring and other credit
    "64999": "fund",           # financial intermediation not elsewhere classified
    "66110": "exchange",       # administration of financial markets
    "66120": "broker",         # security and commodity contracts dealing
    "66190": "broker",         # other auxiliary to financial services
    "66300": "fund",           # fund management activities
}
# Every London postcode area, not just the few central ones. The digit is what
# keeps EX (Exeter), NE (Newcastle) and WA (Warrington) out of an "E/N/W" match.
LONDON_POSTCODES = re.compile(r"^(?:EC|WC|NW|SE|SW|E|W|N)\d", re.I)

# Names that are obviously not what we're after. At register scale most of what
# comes back is holding shells and special-purpose vehicles that have never
# employed anyone — they are the bulk of the noise, so they go first.
JUNK = re.compile(r"nominee|trustee|dormant|holdings? (no|number)|property|estate|"
                  r"restaurant|construction|cleaning|recruit|consult(ing|ancy)? services|"
                  r"\b(bidco|midco|topco|holdco|newco|finco|spv|gp|lp)\b|"
                  r"\bno\.?\s*\d+\b|\b(i{1,3}|iv|v|vi{1,3}|ix|x)\b\s*(limited|ltd)|"
                  r"pension|charit|foundation|church|residents|management company",
                  re.I)

STRIP = re.compile(r"\s+(limited|ltd\.?|llp|plc|uk|holdings|group|international)\b\.?$", re.I)


def search(key, sic, size=100, pages=60):
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
    ap.add_argument("--pages", type=int, default=60,
                    help="pages of 100 per SIC code (60 = up to 6000 each)")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many new firms (0 = no limit)")
    args = ap.parse_args()

    key = os.getenv("CH_API_KEY")
    if not key:
        print("CH_API_KEY not set — free key at "
              "developer.company-information.service.gov.uk", file=sys.stderr)
        return

    existing = set()
    try:
        for r in csv.DictReader(open("firms.csv", encoding="utf-8")):
            existing.add(STRIP.sub("", r["name"]).strip().lower())
    except FileNotFoundError:
        pass

    found = {}
    for sic, category in SIC.items():
        if args.limit and len(found) >= args.limit:
            print(f"  reached --limit {args.limit}, stopping")
            break
        items = search(key, sic, pages=args.pages)
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

    with open("companies_house.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain", "sic",
                                           "incorporated", "postcode"])
        w.writeheader()
        w.writerows(rows)
    print("\nwrote companies_house.csv")

    if args.append:
        with open("firms.csv", "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for r in rows:
                w.writerow([r["name"], r["category"], r["domain"]])
        print(f"appended {len(rows)} firms to firms.csv")
        print("domains are blank — fill the ones you care about, then rerun sniff.py")
    elif not args.preview:
        print("\nrun again with --append to add these to firms.csv")


if __name__ == "__main__":
    main()
