#!/usr/bin/env python3
"""
ats_probe.py — which ATS do the firms we CANNOT reach actually use?

sniff.py recognises thirteen applicant tracking systems, and every one of them
is what a startup or a mid-size firm uses: Greenhouse, Lever, Ashby, BambooHR,
Personio, Pinpoint, Recruitee, Teamtailor, SmartRecruiters, Breezy, Rippling,
Comeet, Workable. Coverage follows exactly that shape — 24% of the registry has
a discovered endpoint, but by category it is metals 15%, banks 16%, refining
16%, majors 16%, trading houses 19%. The commodity houses and banks worth the
most are the worst covered, and 23 of 26 named prime targets have no endpoint
at all.

The obvious next move is to add the enterprise systems. The question is WHICH,
and guessing costs a week of building parsers for whatever turns out to be
rare. So this measures it first: sample firms with no known endpoint, fetch
their careers page, and record which ATS fingerprint appears — including the
ones nothing can currently parse.

    python ats_probe.py --limit 200
    python ats_probe.py --limit 200 --out probe.csv

Reports a ranked distribution. Nothing is scraped and nothing is written to the
registry; this only answers "what would we gain by supporting X".
"""

import argparse
import csv
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_client
import sniff

WORKERS = 16

# Fingerprints for what we already handle, so the probe can report how much of
# the unreachable pile is genuinely unsupported versus simply missed.
KNOWN = {
    "greenhouse": r"(?:boards|job-boards)\.greenhouse\.io|greenhouse\.io/embed",
    "lever": r"jobs\.lever\.co",
    "ashby": r"jobs\.ashbyhq\.com",
    "smartrecruiters": r"(?:careers|jobs)\.smartrecruiters\.com",
    "workable": r"apply\.workable\.com",
    "recruitee": r"[a-z0-9-]+\.recruitee\.com",
    "teamtailor": r"[a-z0-9-]+\.teamtailor\.com",
    "personio": r"[a-z0-9-]+\.jobs\.personio\.",
    "breezy": r"[a-z0-9-]+\.breezy\.hr",
    "bamboohr": r"[a-z0-9-]+\.bamboohr\.com",
    "rippling": r"ats\.rippling\.com",
    "pinpoint": r"[a-z0-9-]+\.pinpointhq\.com",
    "comeet": r"comeet\.co",
    "jobvite": r"jobs\.jobvite\.com|[a-z0-9-]+\.jobvite\.com",
    "workday": r"\.myworkdayjobs\.com",
    "bullhorn": r"bullhornstaffing\.com",
}

# The enterprise systems nothing here can read. This is the list the probe
# exists to rank: build the ones that actually turn up, not the ones that
# sound important.
UNSUPPORTED = {
    "successfactors": r"successfactors\.(?:com|eu)|jobs\.sap\.com|/sfcareer/",
    "taleo": r"taleo\.net|tbe\.taleo\.net",
    "oracle_cloud": r"oraclecloud\.com/hcmUI|/hcmUI/CandidateExperience|fa-[a-z]+\.oraclecloud\.com",
    "icims": r"[a-z0-9-]+\.icims\.com",
    "avature": r"[a-z0-9-]+\.avature\.net",
    "eightfold": r"[a-z0-9-]+\.eightfold\.ai|app\.eightfold\.ai",
    "phenom": r"phenompeople\.com|\.phenom\.com",
    "radancy": r"radancy\.(?:com|net)|talentbrew",
    "cornerstone": r"csod\.com",
    "brassring": r"brassring\.com|kenexa\.com",
    "oleeo": r"oleeo\.com|\.wcn\.co\.uk",          # common in UK public sector
    # \b-anchored: unanchored, "tal.net" matched andurandcapi-TAL.NET and
    # digi-TAL.NET, and reported four hedge funds as running NHS recruitment
    # software. Same substring bug as `uk` matching Ukraine.
    "talnet": r"\btal\.net",                        # ditto — NHS, regulators
    "hireserve": r"hireserve\.com",
    "peoplehr": r"peoplehr\.net",
    "zoho_recruit": r"zohorecruit\.(?:com|eu)",
    "jobtrain": r"jobtrain\.co\.uk",
    "networx": r"networxrecruitment\.com",
    "applied": r"beapplied\.com",
    "tribepad": r"tribepad\.com",
    "workua": r"jobs\.workua",
}

ALL = {**{k: v for k, v in KNOWN.items()}, **UNSUPPORTED}
COMPILED = {k: re.compile(v, re.I) for k, v in ALL.items()}


def probe_one(session, firm):
    """Fetch a firm's careers pages and report every ATS fingerprint seen."""
    domain = (firm.get("domain") or "").strip()
    out = {"name": firm["name"], "category": firm.get("category", ""),
           "domain": domain, "found": "", "supported": "", "url": ""}
    if not domain:
        out["found"] = "no domain"
        return out
    # Same paths sniff.py walks, so the probe measures the pages sniff would
    # have read rather than a different set — otherwise "unsupported" could
    # just mean "we looked somewhere else".
    # "read the page and found no ATS" and "never got a page at all" need
    # completely different answers — the first needs a browser, the second means
    # the careers page is somewhere we are not looking, or the host is blocking
    # us. The first version reported both as "no fingerprint", which made 94% of
    # the sample look like one problem when it is at least two.
    fetched = False
    for path in sniff.PATHS:
        url = f"https://{domain}{path}"
        r = http_client.get(url, sess=session, retries=0, timeout=12)
        if r is None or r.status_code >= 400:
            continue
        fetched = True
        html = r.text or ""
        hits = sorted({k for k, rx in COMPILED.items() if rx.search(html)})
        if hits:
            out["found"] = ",".join(hits)
            out["supported"] = ",".join(h for h in hits if h in KNOWN) or "none"
            out["url"] = url
            return out
        out["url"] = url
    out["found"] = "no fingerprint" if fetched else "never fetched"
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=200,
                    help="how many unreachable firms to sample (default 200)")
    ap.add_argument("--out", default="ats_probe.csv")
    ap.add_argument("--category", help="restrict to one firms.csv category")
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv")))
    reached = set()
    for path in ("endpoints.csv", "sniffed.csv", "manual.csv"):
        try:
            reached |= {r["name"] for r in csv.DictReader(open(path))}
        except OSError:
            pass
    # Only firms whose domain is believed good — probing a domain we already
    # know is dead measures nothing about ATS distribution.
    try:
        ok = {r["name"] for r in csv.DictReader(open("firm_check.csv"))
              if r.get("verdict") in ("ok", "thin", "blocked")}
    except OSError:
        ok = {f["name"] for f in firms}

    todo = [f for f in firms if f["name"] not in reached and f["name"] in ok
            and (f.get("domain") or "").strip()]
    if args.category:
        todo = [f for f in todo if f.get("category") == args.category]
    # Spread the sample across the registry rather than taking the first N,
    # which would be alphabetical and would over-weight whatever was added first.
    if args.limit and len(todo) > args.limit:
        step = len(todo) / args.limit
        todo = [todo[int(i * step)] for i in range(args.limit)]

    print(f"probing {len(todo)} firms with no known endpoint\n")
    session = http_client.session()
    results = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(probe_one, session, f): f for f in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                res = fut.result()
            except Exception as e:
                print(f"  ! {futs[fut]['name']}: {type(e).__name__}", file=sys.stderr)
                continue
            results.append(res)
            if res["found"] not in ("no fingerprint", "no domain", ""):
                print(f"[{i}/{len(todo)}] {res['name'][:30]:<32} {res['found']}")

    with open(args.out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain", "found",
                                           "supported", "url"])
        w.writeheader()
        w.writerows(sorted(results, key=lambda r: (r["found"], r["name"])))

    tally, none_found, never = {}, 0, 0
    for r in results:
        systems = [s for s in r["found"].split(",") if s in ALL]
        if not systems:
            if r["found"] == "never fetched":
                never += 1
            else:
                none_found += 1
        for s in systems:
            tally[s] = tally.get(s, 0) + 1

    print(f"\n{'=' * 58}\nATS distribution across {len(results)} unreachable firms\n{'=' * 58}")
    for s, n in sorted(tally.items(), key=lambda x: -x[1]):
        mark = "supported" if s in KNOWN else "NOT SUPPORTED"
        print(f"  {n:>4}  ({100 * n / max(1, len(results)):>4.1f}%)  {s:<16} {mark}")
    print(f"  {none_found:>4}  ({100 * none_found / max(1, len(results)):>4.1f}%)  "
          f"{'no fingerprint':<16} page READ, no ATS link in it — needs a browser")
    print(f"  {never:>4}  ({100 * never / max(1, len(results)):>4.1f}%)  "
          f"{'never fetched':<16} no careers path answered — wrong path, or blocked")

    gain = sum(n for s, n in tally.items() if s in UNSUPPORTED)
    print(f"\nSupporting every unsupported system found would reach {gain} more of "
          f"these {len(results)} ({100 * gain / max(1, len(results)):.0f}%).")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
