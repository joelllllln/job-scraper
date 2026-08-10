#!/usr/bin/env python3
"""
check_firms.py — prove every domain in firms.csv really belongs to that firm.

A wrong domain is the one genuinely harmful error in the registry. It does not
fail loudly: sniff.py cheerfully reads whoever does own the domain, finds their
applicant tracking system, and files their vacancies under your firm's name. You
end up applying to the wrong company.

So each domain is fetched and the page is checked against the firm's name. A
domain that cannot be reached, or that clearly belongs to somebody else, is
reported — and with --fix it is blanked, which is the safe state: sniff.py skips
firms with no domain, and links.py gives you a name search instead.

    python check_firms.py              # report only
    python check_firms.py --fix        # also blank the domains that failed
    python check_firms.py --only-new   # skip firms already recorded as ok

Writes firm_check.csv: name, domain, verdict, detail, final_url.

Verdicts:
    ok          the page names the firm
    thin        reachable, but nothing on the page confirms it — left alone
    moved       redirects to a different company's domain (acquired, renamed)
    mismatch    the page is plainly somebody else
    unreachable no response after retries
"""

import argparse
import csv
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_client
import store

WORKERS = 24
FIELDS = ["name", "domain", "verdict", "detail", "final_url"]

# Words that carry no identity — the same set the dedupe key ignores, plus the
# corporate furniture that appears on every second company in the City.
EXTRA_NOISE = {"capital", "asset", "management", "investment", "investments",
               "energy", "trading", "markets", "market", "resources", "commodities",
               "securities", "financial", "finance", "bank", "banking", "advisors",
               "advisers", "global", "united", "kingdom", "company", "corporation",
               "corp", "co", "and", "the", "of"}


def name_tokens(name):
    """The parts of a firm's name distinctive enough to identify it on a page."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    toks = [t for t in words if t not in store.NOISE and t not in EXTRA_NOISE and len(t) > 2]
    # Short, all-noise names still need something to match on — BP, ICE, SSE,
    # MOL. Falling through to an empty list would fail every one of them.
    return toks or [t for t in words if len(t) > 2] or words


def page_identity(html):
    """The bits of a page that state who owns it."""
    parts = []
    for pat in (r"<title[^>]*>(.*?)</title>",
                r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']application-name["\'][^>]+content=["\']([^"\']+)',
                r'<meta[^>]+name=["\']description["\'][^>]+content=["\']([^"\']+)',
                r"<h1[^>]*>(.*?)</h1>"):
        for m in re.findall(pat, html, re.S | re.I)[:3]:
            parts.append(http_client.clean_text(m, 300))
    # the copyright line is the single most reliable statement of ownership
    for m in re.findall(r"(?:©|&copy;|copyright)[^<]{0,80}", html, re.I)[:3]:
        parts.append(http_client.clean_text(m, 120))
    return " ".join(parts).lower()


def check_one(session, firm):
    name, domain = firm["name"], (firm.get("domain") or "").strip()
    out = {"name": name, "domain": domain, "verdict": "", "detail": "", "final_url": ""}
    if not domain:
        out["verdict"] = "ok"
        out["detail"] = "no domain claimed"
        return out

    r = http_client.get(f"https://{domain}", sess=session, retries=1)
    if r is None or r.status_code >= 400:
        out["verdict"] = "unreachable"
        out["detail"] = f"http {getattr(r, 'status_code', 'no response')}"
        return out

    out["final_url"] = r.url
    html = http_client.text_of(r, 400_000)
    ident = page_identity(html)
    toks = name_tokens(name)
    hit = [t for t in toks if t in ident]

    final_host = http_client.host_of(r.url).replace("www.", "")
    same_site = final_host.endswith(domain) or domain.endswith(final_host)

    if hit:
        out["verdict"] = "ok"
        out["detail"] = f"page names {'/'.join(hit[:3])}"
    elif not same_site:
        # redirected off the claimed domain and the destination doesn't name the
        # firm either — usually an acquisition, occasionally a domain squatter
        out["verdict"] = "moved"
        out["detail"] = f"redirects to {final_host}"
    elif any(t in ident for t in ("domain", "for sale", "parked", "godaddy", "namecheap")):
        out["verdict"] = "mismatch"
        out["detail"] = "parked or for-sale page"
    elif len(ident) < 40:
        out["verdict"] = "thin"
        out["detail"] = "reachable, page says little"
    else:
        out["verdict"] = "mismatch"
        out["detail"] = f"page does not name the firm: {ident[:60]}"
    return out


def load_previous(path="firm_check.csv"):
    try:
        return {r["name"]: r for r in csv.DictReader(open(path))}
    except OSError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="blank the domains that failed — the safe state")
    ap.add_argument("--only-new", action="store_true",
                    help="skip firms already recorded ok in firm_check.csv")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv")))
    previous = load_previous()
    todo = firms
    if args.only_new:
        todo = [f for f in firms if previous.get(f["name"], {}).get("verdict") != "ok"]
    if args.limit:
        todo = todo[:args.limit]
    if not todo:
        print("nothing to check")
        return

    print(f"checking {len(todo)} of {len(firms)} firms\n")
    session = http_client.session()
    results = dict(previous)

    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futs = {pool.submit(check_one, session, f): f for f in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                try:
                    res = fut.result()
                except Exception as e:
                    print(f"  ! {futs[fut]['name']}: {e}", file=sys.stderr)
                    continue
                results[res["name"]] = res
                if res["verdict"] != "ok":
                    print(f"[{i}/{len(todo)}] {res['verdict'].upper():<12} "
                          f"{res['name'][:30]:<32} {res['domain'][:28]:<30} {res['detail'][:44]}")
                if i % 50 == 0:
                    write(results)
    except KeyboardInterrupt:
        print("\ninterrupted — keeping what completed", file=sys.stderr)

    write(results)

    counts = {}
    for r in results.values():
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    print("\n" + "  ".join(f"{v} {k}" for k, v in sorted(counts.items(), key=lambda x: -x[1])))

    bad = {n for n, r in results.items() if r["verdict"] in ("unreachable", "mismatch", "moved")}
    if not bad:
        print("every domain checks out")
        return
    print(f"\n{len(bad)} firms have a domain that did not check out")
    if not args.fix:
        print("run again with --fix to blank them (sniff.py then skips those firms)")
        return

    for f in firms:
        if f["name"] in bad:
            f["domain"] = ""
    with open("firms.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain"])
        w.writeheader()
        w.writerows(firms)
    print(f"blanked {len(bad)} domains in firms.csv")


def write(results):
    with open("firm_check.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(results.values(), key=lambda r: (r["verdict"], r["name"])))


if __name__ == "__main__":
    main()
