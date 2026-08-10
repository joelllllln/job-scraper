#!/usr/bin/env python3
"""
selftest.py — prove the pipeline works without touching the network.

Seeds a throwaway database with jobs that have known properties, runs the real
scoring, verification parsing and dedupe code against them, and asserts the
answers. Run it after changing scoring.yaml or any of the parsers — it takes two
seconds and catches the class of bug where everything still runs but silently
ranks the wrong things.

    python selftest.py
"""

import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"  {'pass' if ok else 'FAIL'}  {name}" + ("" if ok else f"   got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def check_true(name, cond, detail=""):
    print(f"  {'pass' if cond else 'FAIL'}  {name}" + ("" if cond else f"   {detail}"))
    if not cond:
        FAILS.append(name)


def main():
    import store, verify, score
    import yaml

    print("\nstore — dedupe and provenance")
    check("word order ignored",
          store.canonical_key("Kpler", "Analyst, Crude") ==
          store.canonical_key("Kpler Ltd", "Crude Analyst"), True)
    check("different firms stay separate",
          store.canonical_key("Kpler", "Crude Analyst") ==
          store.canonical_key("Vortexa", "Crude Analyst"), False)
    check("salary text parsed", store.parse_salary({"salary_text": "£45,000 - £60,000"}),
          (45000.0, 60000.0, "GBP"))
    check("no salary stays empty", store.parse_salary({}), (None, None, ""))

    tmp = tempfile.mktemp(suffix=".db")
    con = store.connect(tmp)
    store.save_new(con, [{"company": "Kpler", "title": "Crude Analyst", "location": "London",
                          "url": "https://indeed.com/x", "source": "indeed", "posted": ""}])
    store.save_new(con, [{"company": "Kpler", "title": "Analyst, Crude", "location": "London",
                          "url": "https://boards.greenhouse.io/kpler/1", "source": "greenhouse",
                          "posted": ""}])
    row = con.execute("SELECT source, url, seen_count FROM jobs").fetchall()
    check("duplicate collapsed to one row", len(row), 1)
    check("better source wins", row[0][0], "greenhouse")
    check_true("apply link upgraded to direct", "greenhouse.io" in row[0][1], row[0][1])
    check("seen twice", row[0][2], 2)
    con.close()
    os.unlink(tmp)

    print("\nsniff — ATS fingerprinting")
    import sniff, scrape

    def wd(url):
        m = sniff.WORKDAY.search(url)
        return (m.group(1), m.group(2), m.group(3) or "", m.group(4)) if m else None

    # the optional /en-US/ locale segment is what breaks naive Workday regexes
    check("workday with locale", wd("https://bp.wd3.myworkdayjobs.com/en-US/BPCareers"),
          ("bp", "wd3", "en-US", "BPCareers"))
    check("workday without locale", wd("https://shell.wd3.myworkdayjobs.com/Shell_Careers"),
          ("shell", "wd3", "", "Shell_Careers"))
    check("workday with locale and trailing path",
          wd("https://macquarie.wd3.myworkdayjobs.com/en-US/Macquarie_Careers/job/London"),
          ("macquarie", "wd3", "en-US", "Macquarie_Careers"))
    check_true("workday rejects a cxs api path as a site",
               wd("https://bp.wd3.myworkdayjobs.com/wday/cxs/bp/BPCareers/jobs")[3].lower()
               in ("wday", "en-us"))
    bh = sniff.BULLHORN.search(
        "https://public-rest31.bullhornstaffing.com/rest-services/2fh1q9/search/JobOrder")
    check("bullhorn cluster and token", (bh.group(1), bh.group(2)) if bh else None,
          ("31", "2fh1q9"))
    check_true("greenhouse embed form recognised",
               any(re.search(p, "greenhouse.io/embed/job_board?for=kpler", re.I)
                   for p in sniff.SCRAPABLE["greenhouse"]))
    check_true("workday is not read as a plain GET board",
               "workday" not in scrape.ATS_ENDPOINT and "bullhorn" not in scrape.ATS_ENDPOINT)
    check("sniffed token becomes a real endpoint",
          scrape.ATS_ENDPOINT["lever"].format(t="kpler"),
          "https://api.lever.co/v0/postings/kpler?mode=json")

    print("\nverify — parsers")
    check("years: range takes the floor", verify.years_required("3-5 years experience"), 3)
    check("years: plus form", verify.years_required("5+ years of experience"), 5)
    check("years: absent", verify.years_required("no numbers here"), None)
    check("title match identical", verify.title_similarity("Market Analyst", "Market Analyst"), 1.0)
    check_true("title match rejects listings page",
               verify.title_similarity("Market Analyst", "Search results — 412 jobs found") < 0.34)
    check_true("closed marker detected",
               any(m in "this job has expired" for m in verify.CLOSED_MARKERS))
    check_true("anonymous employer detected",
               bool(verify.ANON_EMPLOYER.search("A leading commodity trading house")))
    check_true("agency language detected",
               bool(verify.AGENCY_MARKERS.search("We are recruiting for our client")))
    ld = verify.extract_jsonld(
        '<script type="application/ld+json">{"@type":"JobPosting","title":"Gas Analyst",'
        '"hiringOrganization":{"name":"Kpler"},"validThrough":"2026-01-01"}</script>')
    check("JSON-LD extracted", ld["title"] if ld else None, "Gas Analyst")

    print("\nscore — rubric behaves")
    cfg = yaml.safe_load(open("scoring.yaml"))
    now = datetime.now(timezone.utc)

    def job(**kw):
        base = {"id": "x", "company": "Kpler", "title": "Market Analyst", "location": "London",
                "source": "greenhouse", "posted": (now - timedelta(days=2)).isoformat(),
                "first_seen": (now - timedelta(days=2)).isoformat(), "description": "",
                "checked_at": now.isoformat(), "live": 1, "reason": "ok", "title_match": 0.9,
                "valid_through": "", "years_required": None, "anonymous": 0, "agency": 0}
        base.update(kw)
        return base

    cats = {"kpler": "data_vendor", "hc group": "recruiter"}
    good = score.score_job(job(), cfg, cats, set())[0]
    senior = score.score_job(job(years_required=8), cfg, cats, set())[0]
    unverified = score.score_job(job(checked_at=None, live=None), cfg, cats, set())[0]
    dead = score.score_job(job(live=0, reason="http 404"), cfg, cats, set())[0]
    anon = score.score_job(job(anonymous=1), cfg, cats, set())[0]
    ghost = score.score_job(job(), cfg, cats, {"x"})[0]
    viaboard = score.score_job(job(source="indeed"), cfg, cats, set())[0]
    edge = score.score_job(job(description="remit surveillance market abuse python commodit"),
                           cfg, cats, set())[0]

    check_true("baseline good job scores well", good >= 60, f"got {good}")
    check_true("8 years of experience is penalised", senior < good - 30, f"{senior} vs {good}")
    check_true("unverified ranks below verified", unverified < good, f"{unverified} vs {good}")
    check_true("dead job is removed from contention", dead < 0, f"got {dead}")
    check_true("anonymous employer penalised", anon < good, f"{anon} vs {good}")
    check_true("ghost repost penalised", ghost < good, f"{ghost} vs {good}")
    check_true("direct beats aggregator", good > viaboard, f"{good} vs {viaboard}")
    check_true("regulatory background rewarded", edge > good, f"{edge} vs {good}")

    print("\nscore — ghost detection over history")
    rows = [{"id": f"g{i}", "company": "Ghost Co", "title": "Market Analyst",
             "first_seen": (now - timedelta(days=d)).isoformat()}
            for i, d in enumerate([200, 140, 70, 5])]
    check("4 reposts over 6 months flagged", len(score.find_ghosts(rows)), 4)
    few = [{"id": "a", "company": "Real Co", "title": "Market Analyst",
            "first_seen": now.isoformat()}]
    check("single posting not flagged", len(score.find_ghosts(few)), 0)

    print("\nhardening — http layer")
    import http_client as hc
    check("clean_text strips tags and entities",
          hc.clean_text("<b>Market&nbsp;Analyst</b> &amp; Research"), "Market Analyst & Research")
    check("clean_text handles None", hc.clean_text(None), "")
    check("bad scheme refused", hc.get("javascript:alert(1)"), None)
    check("empty url refused", hc.get(""), None)
    check("json_of tolerates None", hc.json_of(None), None)
    check("text_of tolerates None", hc.text_of(None), "")

    class FakeResp:
        status_code = 200
        headers = {"content-type": "text/html"}
        text = "<html>not json</html>"
    check("json_of rejects an HTML error page", hc.json_of(FakeResp()), None)

    hosts_before = dict(hc._fails)
    for _ in range(hc.BREAKER_THRESHOLD):
        hc._record("dead.example", False)
    check("circuit breaker opens after repeated failures", hc.breaker_open("dead.example"), True)
    check("healthy host stays closed", hc.breaker_open("fine.example"), False)
    hc._open_until.clear(); hc._fails.clear(); hc._fails.update(hosts_before)

    print("\nhardening — store validation")
    for j, want in [
        ({"company": "X", "title": "Analyst", "url": "https://a"}, True),
        ({"company": "X", "title": "", "url": "https://a"}, False),
        ({"company": "", "title": "Analyst", "url": "https://a"}, False),
        ({"company": "X", "title": "A" * 400, "url": "https://a"}, False),
        ({"company": "X", "title": "Analyst", "url": "javascript:x"}, False),
        ({"company": "X", "title": "12345", "url": "https://a"}, False),
        ({"company": "X", "title": "<div>hi</div>", "url": "https://a"}, False),
    ]:
        check(f"valid_job: {str(j)[:44]}", store.valid_job(j)[0], want)

    tmp2 = tempfile.mktemp(suffix=".db")
    c2 = store.connect(tmp2)
    check("WAL enabled", c2.execute("PRAGMA journal_mode").fetchone()[0].lower(), "wal")
    check_true("busy_timeout set", c2.execute("PRAGMA busy_timeout").fetchone()[0] >= 30000)
    kept = store.save_new(c2, [{"company": "X", "title": "", "url": ""},
                               {"company": "Kpler", "title": "Gas Analyst", "url": "https://a"}])
    check("bad row dropped, good row kept", len(kept), 1)
    c2.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp2 + suffix):
            os.unlink(tmp2 + suffix)

    print("\nreport — hostile input cannot inject")
    nasty = score.render_html(
        [{"title": 'Analyst" onmouseover="alert(1)', "company": "<script>x</script>",
          "location": "London", "source": "indeed", "url": 'https://a"><script>y</script>',
          "years_required": None, "score": 70, "why": [("via <b>indeed</b>", 0)]}],
        [], {"date": "1 Jan", "scraped": 1, "verified": 1, "dead": 0, "ghosts": 0})
    check_true("title quotes escaped", '"' not in nasty.split('class="t">')[1].split("<")[0])
    check_true("no script tag survives", "<script>" not in nasty)
    check_true("url attribute escaped", '"><script>' not in nasty)

    print("\ncollectors — wiring")
    import feeds, workday
    # feeds.py used to call its writer store(), which shadowed the store module
    # and made every Reed/Bullhorn run die with AttributeError at the last step
    check_true("feeds does not shadow the store module",
               feeds.store is store, type(feeds.store).__name__)
    for mod, name in ((feeds, "feeds"), (workday, "workday"), (scrape, "scrape")):
        check_true(f"{name} writes through store.save_new",
                   hasattr(mod.store, "save_new"))

    print("\nconfig — filter sanity")
    inc = re.compile("|".join(yaml.safe_load(open("config.yaml"))["include"]), re.I)
    exc = re.compile("|".join(yaml.safe_load(open("config.yaml"))["exclude"]), re.I)
    for title, want in [("Commodity Analyst", True), ("Junior Trader", True),
                        ("Data Scientist, Trading", True), ("Head of Trading", False),
                        ("Credit Risk Analyst", False), ("Trade Support Analyst", False),
                        ("Clearing Operations", False), ("Marketing Manager", False),
                        ("Short-Term Power Analyst", True), ("Quantitative Researcher", True)]:
        check(f"filter: {title}", bool(inc.search(title)) and not exc.search(title), want)

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
