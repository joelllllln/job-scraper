#!/usr/bin/env python3
"""
bench_extract.py — how many real careers-page shapes can we actually read?

Coverage arguments were being settled by reasoning, which is how "86% need a
browser" survived until it turned out half of them ship their jobs in the HTML.
This is the measuring stick: a corpus of the shapes careers pages actually come
in, and a score for how many of them yield a usable job.

    python bench_extract.py            # score
    python bench_extract.py -v         # and show what each case produced

Every case is a page the extractors should handle. A case that fails is not a
bug report, it is a coverage gap with a name — which is the point.
"""

import argparse
import json
import sys

import embedded
import sniff

JOB = {"title": "Junior Gas Analyst", "location": "London, UK"}


def _ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def _posting(**kw):
    base = {"@type": "JobPosting", "title": JOB["title"], "url": "https://x.com/j/1",
            "datePosted": "2026-08-01", "hiringOrganization": {"name": "Testco"},
            "jobLocation": {"address": {"addressLocality": "London"}}}
    base.update(kw)
    return base


CASES = [
    # --- framework state, serialised into the HTML -------------------------
    ("next.js pages router",
     '<script id="__NEXT_DATA__" type="application/json">' +
     json.dumps({"props": {"pageProps": {"jobs": [dict(JOB, slug="jga")]}}}) + '</script>'),

    ("next.js app router (streamed)",
     '<script>self.__next_f.push([1,' + json.dumps(
         '{"openPositions":[{"title":"Junior Gas Analyst","location":"London, UK",'
         '"slug":"jga"}]}') + '])</script>'),

    ("nuxt",
     '<script>window.__NUXT__ = ' +
     json.dumps({"data": {"vacancies": [dict(JOB, url="/jobs/1")]}}) + ';</script>'),

    ("redux initial state",
     '<script>window.__INITIAL_STATE__ = ' +
     json.dumps({"careers": {"openPositions": [dict(JOB, applyUrl="https://x.com/1")]}}) +
     ';</script>'),

    ("apollo cache",
     '<script>window.__APOLLO_STATE__ = ' +
     json.dumps({"ROOT_QUERY": {"jobPostings": [dict(JOB, id="1")]}}) + ';</script>'),

    ("generic application/json island",
     '<script type="application/json" id="jobs-data">' +
     json.dumps({"vacancies": [dict(JOB, href="/v/1")]}) + '</script>'),

    # --- schema.org --------------------------------------------------------
    ("json-ld single posting", _ld(_posting())),
    ("json-ld array of postings", _ld([_posting(), _posting(title="Power Analyst")])),
    ("json-ld inside @graph", _ld({"@graph": [_posting()]})),
    ("json-ld with @type as a list", _ld(_posting(**{"@type": ["JobPosting"]}))),

    # --- plain HTML, no JSON anywhere --------------------------------------
    ("html list of job links",
     '<ul class="vacancies">'
     '<li><a href="/jobs/junior-gas-analyst">Junior Gas Analyst</a> — London, UK</li>'
     '<li><a href="/jobs/power-analyst">Power Trading Analyst</a> — London</li></ul>'),

    ("html table of jobs",
     '<table><tr><th>Role</th><th>Location</th></tr>'
     '<tr><td><a href="/careers/1">Junior Gas Analyst</a></td><td>London, UK</td></tr>'
     '</table>'),

    ("html cards with headings",
     '<div class="job-card"><h3><a href="/roles/jga">Junior Gas Analyst</a></h3>'
     '<span class="location">London, UK</span></div>'),

    # --- an ATS link, which is worth more than the jobs themselves ---------
    ("greenhouse iframe embed",
     '<iframe src="https://boards.greenhouse.io/embed/job_board?for=testco"></iframe>'),
    ("workday link", '<a href="https://testco.wd3.myworkdayjobs.com/en-US/Careers">Jobs</a>'),
    ("oracle link", '<a href="https://x.fa.ocs.oraclecloud.com/hcmUI/CandidateExperience/'
                    'en/sites/CX_1001/requisitions">Search</a>'),
    ("eightfold link", '<a href="https://testco.eightfold.ai/careers">Openings</a>'),
]


# Pages that must yield NOTHING. A benchmark that only counts recall rewards an
# extractor for mining navigation menus, and the digest is where that lands.
NEGATIVE = [
    ("site nav only",
     '<nav><a href="/careers">Careers</a><a href="/about">About us</a>'
     '<a href="/contact">Contact</a><a href="/news">Newsroom</a></nav>'),

    ("blog listing",
     '<ul><li><a href="/insights/gas-market-outlook-2026">Gas market outlook 2026</a></li>'
     '<li><a href="/insights/hiring-in-a-tight-market">Hiring in a tight market</a></li></ul>'),

    ("people directory",
     '<div class="team"><a href="/people/jane-smith">Jane Smith</a>'
     '<span>Head of Trading</span></div>'),

    ("office locations",
     '<script id="__NEXT_DATA__" type="application/json">' +
     json.dumps({"props": {"pageProps": {"offices": [
         {"name": "London", "city": "London", "url": "/offices/london"}]}}}) + '</script>'),

    ("careers landing page with no vacancies",
     '<h1>Careers</h1><p>We are always interested in hearing from talented people.</p>'
     '<a href="/careers/benefits">Benefits</a><a href="/careers/culture">Our culture</a>'),

    ("cookie banner and legal footer",
     '<div>We use cookies. <a href="/privacy">Privacy policy</a> '
     '<a href="/terms">Terms of use</a> <a href="/modern-slavery">Modern slavery statement</a></div>'),
]


def _at_scale():
    """A realistic page: full site chrome, blog teasers, people, legal, 5 jobs.

    The small fixtures hid two real defects — the location field ran past the
    end of its list item and swallowed the next job's title, and "Apply now"
    slipped through on a perfectly job-shaped /jobs/apply-now href.
    """
    nav = "".join(f'<a href="/about/{i}">About section {i}</a>' for i in range(40))
    blog = "".join(f'<li><a href="/insights/market-note-{i}">Market note {i} for 2026</a></li>'
                   for i in range(60))
    people = "".join(f'<a href="/people/p-{i}">Person Number {i}</a><span>Head of Desk</span>'
                     for i in range(50))
    legal = ('<a href="/privacy">Privacy policy</a><a href="/careers/benefits">Benefits</a>'
             '<a href="/careers/our-culture">Our culture</a>'
             '<a href="/careers/why-join-us">Why join us</a>')
    jobs = ('<ul class="vacancies">'
            '<li><a href="/jobs/junior-gas-analyst">Junior Gas Analyst</a> \u2014 London, UK</li>'
            '<li><a href="/jobs/power-trading-analyst">Power Trading Analyst</a> \u2014 London</li>'
            '<li><a href="/jobs/lng-scheduler">LNG Scheduler</a> \u2014 London</li>'
            '<li><a href="/careers/risk-analyst-2026">Market Risk Analyst</a> \u2014 London</li>'
            '<li><a href="/jobs/apply-now">Apply now</a></li></ul>')
    filler = "<p>" + ("Vitol is a leader in energy. " * 400) + "</p>"
    return f"<html><body>{nav}{filler}{blog}{people}{jobs}{legal}{filler}</body></html>"


def score_case(name, html):
    """Did anything usable come out? Either an ATS or at least one job."""
    ats = sniff.fingerprint("Testco", html + " https://x.com/careers", "https://x.com/careers")
    if ats:
        return "ats", f"{ats['ats']}/{ats.get('token') or ats.get('site')}"
    jobs = embedded.jobs_from_jsonld("Testco", html)
    if jobs:
        return "jobs", f"{len(jobs)} via json-ld: {jobs[0]['title']}"
    jobs = embedded.jobs_from_html("Testco", html, "https://x.com/careers")
    if jobs:
        return "jobs", f"{len(jobs)} via page state: {jobs[0]['title']}"
    jobs = embedded.jobs_from_links("Testco", html, "https://x.com/careers")
    if jobs:
        return "jobs", f"{len(jobs)} via html links: {jobs[0]['title']}"
    return "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    got, missed = 0, []
    for name, html in CASES:
        kind, detail = score_case(name, html)
        if kind:
            got += 1
            if args.verbose:
                print(f"  ok    {name:<34} {kind}: {detail}")
        else:
            missed.append(name)
            print(f"  MISS  {name}")
    print(f"\n{got}/{len(CASES)} careers-page shapes yield something usable "
          f"({100 * got // len(CASES)}%)")
    if missed:
        print("gaps: " + ", ".join(missed))

    print()
    clean, dirty = 0, []
    for name, html in NEGATIVE:
        kind, detail = score_case(name, html)
        if kind:
            dirty.append(f"{name} -> {detail}")
            print(f"  FALSE POSITIVE  {name:<34} {detail}")
        else:
            clean += 1
            if args.verbose:
                print(f"  ok    {name:<34} correctly yielded nothing")
    print(f"\n{clean}/{len(NEGATIVE)} non-vacancy pages correctly yield nothing "
          f"({100 * clean // len(NEGATIVE)}%)")

    print()
    page = _at_scale()
    got_s = embedded.jobs_from_links("Vitol", page, "https://vitol.com/careers")
    want = {"Junior Gas Analyst", "Power Trading Analyst", "LNG Scheduler", "Market Risk Analyst"}
    have = {j["title"] for j in got_s}
    bleed = [j for j in got_s if len(j["location"]) > 24]
    print(f"at realistic scale ({len(page)//1024} KB, {page.count('<a ')} links): "
          f"{len(have & want)}/{len(want)} found, {len(have - want)} false positives, "
          f"{len(bleed)} locations bleeding past their element")
    if have - want:
        dirty.append("scale: " + ", ".join(sorted(have - want)))
        print("  FALSE POSITIVE " + ", ".join(sorted(have - want)))
    if bleed:
        dirty.append("scale: location bleed")
        print(f"  LOCATION BLEED  {bleed[0]['location']!r}")
    if dirty:
        print("A false positive is worse than a gap: it reaches the digest.")
    return 1 if dirty else 0


if __name__ == "__main__":
    sys.exit(main())
