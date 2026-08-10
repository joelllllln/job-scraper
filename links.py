#!/usr/bin/env python3
"""
links.py — build links.md: a clickable index for every firm.

For firms with no public ATS (see no_ats.csv after running discover.py), this is
how you check them: LinkedIn company jobs, Indeed, Glassdoor, a Google X-ray that
finds the real careers page, and the likely careers URLs to try directly.

    python links.py            # all firms
    python links.py no_ats.csv # only the ones the scraper can't reach
"""

import csv
import re
import sys
from urllib.parse import quote_plus

SRC = sys.argv[1] if len(sys.argv) > 1 else "firms.csv"

ROLE_QUERY = (
    '"commodity analyst" OR "market analyst" OR "trading analyst" OR '
    '"quantitative analyst" OR "data scientist" OR "junior trader" OR '
    '"research analyst" OR "fundamental analyst"'
)

CAREER_PATHS = ["careers", "jobs", "about/careers", "company/careers", "careers/vacancies"]


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def block(name, domain):
    q = quote_plus(name)
    s = slug(name)
    lines = [
        f"### {name}",
        f"- Careers (try): " + " · ".join(f"[{p}](https://{domain}/{p})" for p in CAREER_PATHS[:3]),
        f"- [LinkedIn company](https://www.linkedin.com/company/{s}/jobs/)",
        f"- [LinkedIn jobs search](https://www.linkedin.com/jobs/search/?keywords={q}&location=London%2C%20England%2C%20United%20Kingdom)",
        f"- [Indeed](https://uk.indeed.com/jobs?q={q}&l=London)",
        f"- [Glassdoor](https://www.glassdoor.co.uk/Search/results.htm?keyword={q})",
        f"- [Google X-ray careers](https://www.google.com/search?q=" + quote_plus(f"site:{domain} careers OR jobs analyst") + ")",
        f"- [Google X-ray LinkedIn](https://www.google.com/search?q=" + quote_plus(f'site:linkedin.com/jobs "{name}" London analyst') + ")",
        "",
    ]
    return "\n".join(lines)


def main():
    rows = list(csv.DictReader(open(SRC)))
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r.get("category", "other"), []).append(r)

    out = ["# Firm link index", "",
           f"Generated from `{SRC}` — {len(rows)} firms.", "",
           "## Global searches", "",
           f"- [LinkedIn — all roles, London](https://www.linkedin.com/jobs/search/?keywords={quote_plus(ROLE_QUERY)}&location=London%2C%20England%2C%20United%20Kingdom&f_TPR=r604800)",
           f"- [Indeed — commodity analyst](https://uk.indeed.com/jobs?q={quote_plus('commodity OR trading OR market analyst')}&l=London&fromage=7)",
           f"- [Google Jobs](https://www.google.com/search?q={quote_plus('commodity analyst London')}&ibp=htl;jobs)",
           "- [eFinancialCareers](https://www.efinancialcareers.co.uk/jobs-UK-London)",
           "- [Otta / Welcome to the Jungle](https://app.otta.com/jobs)",
           "- [Adzuna](https://www.adzuna.co.uk/search?q=commodity+analyst&loc=London)",
           "- [Modo Energy jobs](https://modoenergy.com/jobs)",
           "- [Climatebase](https://climatebase.org/jobs)",
           "- [CryptoJobsList](https://cryptojobslist.com/)",
           "- [QuantNet](https://quantnet.com/forum/quant-jobs/)",
           "- [Companies House advanced search](https://find-and-update.company-information.service.gov.uk/advanced-search) — SIC 46719, 64999, 66120, 35140 + London",
           "- [FCA Register](https://register.fca.org.uk/s/)",
           ""]

    for cat in sorted(by_cat):
        out.append(f"## {cat.replace('_', ' ').title()}")
        out.append("")
        for r in sorted(by_cat[cat], key=lambda x: x["name"]):
            out.append(block(r["name"], r["domain"]))

    open("links.md", "w").write("\n".join(out))
    print(f"wrote links.md — {len(rows)} firms across {len(by_cat)} categories")


if __name__ == "__main__":
    main()
