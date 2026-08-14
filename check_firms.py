#!/usr/bin/env python3
"""
check_firms.py — prove every domain in firms.csv really belongs to that firm.

A wrong domain is the one genuinely harmful error in the registry. It does not
fail loudly: sniff.py cheerfully reads whoever does own the domain, finds their
applicant tracking system, and files their vacancies under your firm's name. You
end up applying to the wrong company.

So each domain is fetched and the page is checked against the firm's name. Only
a page that plainly belongs to somebody else is blanked, and that restraint is
the whole design: an over-eager check is worse than none. The first version
treated a bot wall and a redirect as failures and blanked 661 of 2303 domains,
most of them correct — Mercuria, Hartree, Engelhart, Louis Dreyfus among them.
Blanking a correct domain removes the firm from every future run silently.

    python check_firms.py              # report only
    python check_firms.py --fix        # blank only the clear mismatches
    python check_firms.py --only-new   # skip firms already recorded as ok

Writes firm_check.csv: name, domain, verdict, detail, final_url.

Verdicts, and what --fix does with each:
    ok          the page names the firm                        kept
    thin        reachable, page says little                    kept
    blocked     403/503 or a bot challenge — a datacentre IP
                being turned away, not a wrong domain          kept
    moved       redirects somewhere that doesn't name it —
                often a real acquisition, worth a human look   kept, reported
    unreachable no response at all                             kept, reported
    mismatch    the page is plainly a different company        BLANKED
"""

import argparse
import csv
import os
import re
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed

import http_client
import store

WORKERS = 24

# Statuses that mean "not to a datacentre IP", not "no such site".
BOT_BLOCK_STATUS = {401, 403, 405, 406, 409, 429, 503}
INTERSTITIAL = re.compile(r"perfdrive|datadome|cloudflare|incapsula|imperva|akamai|"
                          r"just a moment|checking your browser|are you a robot|"
                          r"access denied|attention required", re.I)
FIELDS = ["name", "domain", "verdict", "detail", "final_url", "misses"]

# How many consecutive unreachable checks before a domain is considered dead.
# Never one: 383 of 401 unreachable verdicts in a single pass were "no response",
# and a DNS hiccup, an expired TLS cert or a firewall having a bad afternoon all
# look exactly like that. Only verdicts that mean "nothing is there" count —
# blocked, thin and moved are evidence the host is alive, and reset the counter.
DEAD_AFTER = 3
COUNTS_AS_MISS = ("unreachable",)
# http 410 Gone is the one definitive answer: the server is telling you the
# resource is permanently removed, so it does not need three attempts.
DEFINITELY_GONE = re.compile(r"http 410", re.I)

# Words that carry no identity — the same set the dedupe key ignores, plus the
# corporate furniture that appears on every second company in the City.
EXTRA_NOISE = {"capital", "asset", "management", "investment", "investments",
               "energy", "trading", "markets", "market", "resources", "commodities",
               "securities", "financial", "finance", "bank", "banking", "advisors",
               "advisers", "global", "united", "kingdom", "company", "corporation",
               "corp", "co", "and", "the", "of"}


def deaccent(s):
    """cez.cz says "skupina ČEZ", botas.gov.tr says "BOTAŞ". Without folding the
    diacritics away, every non-English site fails to match its own name."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def name_tokens(name, domain=""):
    """The parts of a firm's name distinctive enough to identify it on a page.

    The domain's own label counts as one: bimco.org belongs to the Baltic and
    International Maritime Council, whose site quite reasonably just says
    "BIMCO", and no word of the registered name appears anywhere on it.
    """
    label = (domain or "").split(".")[0].replace("-", "")
    words = re.findall(r"[a-z0-9]+", deaccent(name).lower())
    toks = [t for t in words if t not in store.NOISE and t not in EXTRA_NOISE and len(t) > 2]
    # Short, all-noise names still need something to match on — BP, ICE, SSE,
    # MOL. Falling through to an empty list would fail every one of them.
    toks = toks or [t for t in words if len(t) > 2] or words
    if label and len(label) > 2 and label not in toks:
        toks.append(label)
    return toks


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

    # Try www too. An apex with no DNS record and a working www is extremely
    # common, and asking only for the apex recorded 401 live companies as
    # unreachable — which then excluded them from every later stage.
    r = http_client.get(f"https://{domain}", sess=session, retries=1)
    if r is None and not domain.startswith("www.") and "/" not in domain:
        r = http_client.get(f"https://www.{domain}", sess=session, retries=1)
    elif r is None and domain.startswith("www."):
        r = http_client.get(f"https://{domain[4:]}", sess=session, retries=1)
    if r is None:
        out["verdict"] = "unreachable"
        out["detail"] = "no response"
        return out
    # A datacentre IP asking for a corporate homepage gets turned away constantly.
    # 403 and 503 mean "not to you", not "no such company" — abnamro.com and
    # accessbankplc.com are plainly correct, and treating these as failures once
    # blanked 557 domains, most of them right.
    if r.status_code in BOT_BLOCK_STATUS:
        out["verdict"] = "blocked"
        out["detail"] = f"http {r.status_code} — bot protection, domain probably fine"
        return out
    if r.status_code >= 400:
        out["verdict"] = "unreachable"
        out["detail"] = f"http {r.status_code}"
        return out

    out["final_url"] = r.url
    html = http_client.text_of(r, 400_000)
    ident = deaccent(page_identity(html))
    # bank-abc.com writes itself "Bank ABC", redwheel.com "Red Wheel". Comparing
    # with the punctuation and spaces squeezed out catches the whole family.
    squashed = re.sub(r"[^a-z0-9]", "", ident)
    toks = name_tokens(name, domain)
    hit = [t for t in toks if t in ident or (len(t) > 3 and t in squashed)]

    final_host = http_client.host_of(r.url).replace("www.", "").split(":")[0]
    bare = domain.replace("www.", "").split(":")[0]
    same_site = final_host.endswith(bare) or bare.endswith(final_host)
    if not hit and not same_site and INTERSTITIAL.search(final_host + " " + ident):
        # DataDome, Cloudflare and friends serve a challenge page from their own
        # host. That is the bot wall again, not evidence the firm moved.
        out["verdict"] = "blocked"
        out["detail"] = f"bot challenge at {final_host}"
        return out

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
    elif not mostly_latin(ident):
        # A Latin firm name cannot be found in a page written in Chinese,
        # Japanese, Korean or Thai, so "the page does not name the firm" is not
        # a finding here — it is the only possible outcome. Seven of thirteen
        # domains blanked in one run were correct: icbc.com.cn served
        # 中国工商银行, itochu.co.jp served 伊藤忠商事株式会社, and both were
        # deleted from the registry as belonging to somebody else. Reported as
        # unjudged so the firm keeps its domain.
        out["verdict"] = "thin"
        out["detail"] = f"page is not in Latin script — cannot match the name: {ident[:40]}"
    else:
        out["verdict"] = "mismatch"
        out["detail"] = f"page does not name the firm: {ident[:60]}"
    return out


def mostly_latin(text, floor=0.5):
    """Can a Latin-alphabet company name possibly be found in this text?

    Counts only letters, so punctuation, digits and whitespace do not sway it
    either way. Mojibake counts as non-Latin too, which is the right answer for
    the same reason: mitsubishi's page arrived as 'дєиип±гв±ггягвђ' and no name
    match was ever going to succeed against it.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return True                     # nothing to judge — leave it to the other rules
    latin = sum(1 for c in letters if c.isascii())
    return latin / len(letters) >= floor


def load_previous(path="firm_check.csv"):
    """Prior verdicts, including ones written before this file spoke utf-8.

    A run on Windows wrote this file as cp1252 and then could not read it back,
    failing the stage before it checked a single firm. The bytes it managed to
    write are still perfectly good verdicts, so they are decoded leniently
    rather than thrown away — the file is rewritten as utf-8 at the end of the
    run either way, so the leniency applies exactly once.
    """
    try:
        with open(path, newline="", encoding="utf-8") as fh:
            return {r["name"]: r for r in csv.DictReader(fh)}
    except OSError:
        return {}
    except UnicodeDecodeError:
        pass
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as fh:
            return {r["name"]: r for r in csv.DictReader(fh)}
    except OSError:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix", action="store_true",
                    help="blank the domains that failed — the safe state")
    ap.add_argument("--only-new", action="store_true",
                    help="skip firms already recorded ok in firm_check.csv")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--prune", action="store_true",
                    help=f"drop firms unreachable {DEAD_AFTER} runs running, or gone (410)")
    args = ap.parse_args()

    firms = list(csv.DictReader(open("firms.csv", encoding="utf-8")))
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
                # Carry the miss counter across runs: consecutive failures are
                # what distinguishes a dead domain from a bad afternoon.
                was = previous.get(res["name"], {})
                prior = int(was.get("misses") or 0)
                res["misses"] = prior + 1 if res["verdict"] in COUNTS_AS_MISS else 0
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

    stale = prune(firms, results)
    if stale:
        print(f"\n{len(stale)} firms have been unreachable long enough to call dead:")
        for n, why in sorted(stale.items())[:15]:
            print(f"    {n[:38]:<40} {why}")
        if len(stale) > 15:
            print(f"    ... and {len(stale) - 15} more")
        if args.prune:
            firms[:] = [f for f in firms if f["name"] not in stale]
            results = {n: r for n, r in results.items() if n not in stale}
            print(f"pruned {len(stale)} firms from firms.csv")
        else:
            print("run again with --prune to remove them")
    near = sum(1 for r in results.values()
               if r.get("verdict") in COUNTS_AS_MISS and 0 < int(r.get("misses") or 0) < DEAD_AFTER)
    if near:
        print(f"{near} more are failing but not yet at {DEAD_AFTER} consecutive misses")

    bad = {n for n, r in results.items() if r["verdict"] == "mismatch"}
    if not bad:
        print("every domain checks out")
        if args.prune and stale:
            save_firms(firms)     # pruning alone still has to be written out
            write(results)
        return
    print(f"\n{len(bad)} firms have a domain that belongs to somebody else")
    print("blocked / unreachable / moved are reported but NOT blanked: a bot wall\n    or a redirect is not evidence the domain is wrong, and blanking a correct\n    one silently removes the firm from every future run.")
    if not args.fix:
        print("run again with --fix to blank them (sniff.py then skips those firms)")
        return

    for f in firms:
        if f["name"] in bad:
            f["domain"] = ""
    save_firms(firms)
    write(results)
    print(f"blanked {len(bad)} domains in firms.csv")


def prune(firms, results):
    """Remove firms whose domain has been dead for several runs running.

    Deliberately conservative. A firm is only dropped when the host has failed
    to answer DEAD_AFTER times in a row, or has returned 410 Gone once — the
    single status that means "permanently removed" rather than "not today".
    Bot walls, thin pages and redirects never count: all three prove something
    is answering, and a firm removed here stops being scraped forever.
    """
    doomed = {}
    for f in firms:
        r = results.get(f["name"])
        if not r or r.get("verdict") not in COUNTS_AS_MISS:
            continue
        if DEFINITELY_GONE.search(r.get("detail", "")):
            doomed[f["name"]] = "410 gone"
        elif int(r.get("misses") or 0) >= DEAD_AFTER:
            doomed[f["name"]] = f"unreachable {r['misses']} runs running"
    return doomed


def save_firms(firms):
    with open("firms.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "category", "domain"], extrasaction="ignore")
        w.writeheader()
        w.writerows(firms)


def write(results):
    with open("firm_check.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(results.values(), key=lambda r: (r["verdict"], r["name"])))


if __name__ == "__main__":
    main()
