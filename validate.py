#!/usr/bin/env python3
"""
validate.py — check the config files before a run, not 20 minutes into one.

A single malformed regex in config.yaml used to crash the pipeline after all the
scraping was done. A weight accidentally typed as a string in scoring.yaml made
every score identical without any error at all — the second failure is worse,
because the digest still looks normal.

    python validate.py        # exits non-zero if anything is wrong

weekly.sh runs this first and aborts if it fails.
"""

import os
import re
import sys

import yaml

ERRORS, WARNINGS = [], []


def err(m):
    ERRORS.append(m)


def warn(m):
    WARNINGS.append(m)


def check_regexes(patterns, where):
    for p in patterns:
        if not isinstance(p, str):
            err(f"{where}: pattern is {type(p).__name__}, expected string: {p!r}")
            continue
        try:
            re.compile(p, re.I)
        except re.error as e:
            err(f"{where}: bad regex {p!r} — {e}")
        if p.strip() in (".", ".*", ".+", ""):
            err(f"{where}: pattern {p!r} matches everything — this would flood the digest")


def check_config():
    if not os.path.exists("config.yaml"):
        err("config.yaml missing")
        return
    try:
        cfg = yaml.safe_load(open("config.yaml"))
    except yaml.YAMLError as e:
        err(f"config.yaml is not valid YAML — {e}")
        return
    for key in ("include", "exclude", "locations"):
        if key not in cfg:
            err(f"config.yaml: missing '{key}'")
        elif not isinstance(cfg[key], list) or not cfg[key]:
            err(f"config.yaml: '{key}' must be a non-empty list")
        else:
            check_regexes(cfg[key], f"config.yaml/{key}")

    if not ERRORS:
        inc = re.compile("|".join(cfg["include"]), re.I)
        exc = re.compile("|".join(cfg["exclude"]), re.I)
        # a filter that rejects the obvious keepers is broken even if it compiles
        must_keep = ["Commodity Analyst", "Junior Trader", "Market Analyst",
                     "Quantitative Researcher", "Data Scientist, Trading",
                     # junior without being student-only — the actual target. If
                     # the filter stops collecting these the digest quietly
                     # loses its best roles.
                     "Junior Market Analyst", "Trainee Broker",
                     "Entry Level Trading Analyst", "Assistant Trader"]
        must_drop = ["Head of Trading", "Credit Risk Analyst", "Trade Support Analyst",
                     "Marketing Manager"]
        for t in must_keep:
            if not inc.search(t) or exc.search(t):
                err(f"config.yaml: '{t}' would be filtered out — check include/exclude")
        for t in must_drop:
            if inc.search(t) and not exc.search(t):
                warn(f"config.yaml: '{t}' would be kept — you said you didn't want these")


def check_scoring():
    if not os.path.exists("scoring.yaml"):
        err("scoring.yaml missing")
        return
    try:
        cfg = yaml.safe_load(open("scoring.yaml"))
    except yaml.YAMLError as e:
        err(f"scoring.yaml is not valid YAML — {e}")
        return

    for key in ("title_tiers", "seniority", "firm_categories", "description_signals",
                "source_weights", "freshness", "verification", "report"):
        if key not in cfg:
            err(f"scoring.yaml: missing '{key}'")

    for tier, spec in (cfg.get("title_tiers") or {}).items():
        if not isinstance(spec.get("points"), (int, float)):
            err(f"scoring.yaml: title_tiers/{tier}/points must be a number, "
                f"got {spec.get('points')!r}")
        check_regexes(spec.get("patterns") or [], f"scoring.yaml/title_tiers/{tier}")

    for name, spec in (cfg.get("description_signals") or {}).items():
        if not isinstance(spec.get("points"), (int, float)):
            err(f"scoring.yaml: description_signals/{name}/points must be a number")
        check_regexes(spec.get("patterns") or [], f"scoring.yaml/description_signals/{name}")

    for name, v in (cfg.get("firm_categories") or {}).items():
        if not isinstance(v, (int, float)):
            err(f"scoring.yaml: firm_categories/{name} must be a number, got {v!r}")
    if "unknown" not in (cfg.get("firm_categories") or {}):
        err("scoring.yaml: firm_categories needs an 'unknown' fallback")

    for name, v in (cfg.get("source_weights") or {}).items():
        if not isinstance(v, (int, float)):
            err(f"scoring.yaml: source_weights/{name} must be a number, got {v!r}")

    fresh = cfg.get("freshness") or []
    if not all(isinstance(p, list) and len(p) == 2 for p in fresh):
        err("scoring.yaml: freshness must be a list of [days, points] pairs")
    elif [p[0] for p in fresh] != sorted(p[0] for p in fresh):
        err("scoring.yaml: freshness thresholds must be in ascending order of days")

    ver = cfg.get("verification") or {}
    if ver.get("dead", 0) > -100:
        warn("scoring.yaml: verification/dead is mild — dead jobs may still rank")
    if ver.get("live", 0) <= 0:
        warn("scoring.yaml: verification/live is not positive — verification isn't rewarded")

    rep = cfg.get("report") or {}
    if rep.get("shortlist_threshold", 0) < rep.get("min_score", 0):
        err("scoring.yaml: report/shortlist_threshold is below min_score — "
            "the shortlist would include everything")


def check_firms():
    if not os.path.exists("firms.csv"):
        err("firms.csv missing")
        return
    import csv
    rows = list(csv.DictReader(open("firms.csv")))
    if not rows:
        err("firms.csv is empty")
        return
    missing_cols = {"name", "category", "domain"} - set(rows[0].keys())
    if missing_cols:
        err(f"firms.csv: missing columns {missing_cols}")
        return
    blank = sum(1 for r in rows if not (r["domain"] or "").strip())
    dupes = len(rows) - len({(r["name"] or "").lower() for r in rows})
    if dupes:
        warn(f"firms.csv: {dupes} duplicate firm names")

    # Two firms on one domain means one careers board scraped twice and filed
    # under two company names — the digest shows the same role twice, because
    # the dedupe key includes the company.
    seen = {}
    shared = []
    for r in rows:
        d = (r["domain"] or "").strip().lower()
        if not d:
            continue
        if d in seen:
            shared.append(f"{seen[d]} / {r['name']} ({d})")
        else:
            seen[d] = r["name"]
    if shared:
        err(f"firms.csv: {len(shared)} domains claimed by two firms — the same board "
            f"would be scraped twice: {'; '.join(shared[:3])}"
            + (" ..." if len(shared) > 3 else ""))
    if blank:
        warn(f"firms.csv: {blank} firms have no domain — sniff.py will skip them")
    print(f"  firms.csv: {len(rows)} firms, {len(rows) - blank} with domains")


def check_env():
    optional = {
        "REED_API_KEY": "feeds.py --reed will be skipped",
        "ADZUNA_APP_ID": "Adzuna results will be skipped",
        "JOOBLE_API_KEY": "Jooble results will be skipped",
        "CAREERJET_AFFID": "Careerjet results will be skipped",
        "CH_API_KEY": "companies_house.py will not run",
    }
    for k, effect in optional.items():
        if not os.getenv(k):
            print(f"  note: {k} not set — {effect}")
    if not (os.getenv("SMTP_HOST") or os.getenv("TELEGRAM_TOKEN")):
        print("  note: no delivery configured — the digest will only print to stdout")


def main():
    print("validating config\n")
    check_config()
    check_scoring()
    check_firms()
    check_env()

    print()
    for w in WARNINGS:
        print(f"  warn   {w}")
    for e in ERRORS:
        print(f"  ERROR  {e}")

    if ERRORS:
        print(f"\n{len(ERRORS)} error(s) — fix before running")
        sys.exit(1)
    print(f"\nconfig ok{f' ({len(WARNINGS)} warnings)' if WARNINGS else ''}")


if __name__ == "__main__":
    main()
