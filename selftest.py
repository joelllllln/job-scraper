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


# Environment variables that change what the code under test does. weekly.sh
# runs this as a gate from inside the GitHub workflow, which exports several of
# them, so without scrubbing here a test can pass on a laptop and fail on the
# runner — and a failing gate aborts the whole weekly run before it collects
# anything. Tests that want one of these set it themselves and restore it.
AMBIENT = ("NO_LINKEDIN", "JOBSPY_PROXIES", "SMTP_HOST", "SMTP_PORT", "SMTP_USER",
           "SMTP_PASS", "DIGEST_TO", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
           "GITHUB_TOKEN", "RUN_MODE", "HOURS")


def main():
    import store, verify, score
    import yaml

    for var in AMBIENT:
        os.environ.pop(var, None)

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

    print("\nscale — what breaks at thousands of firms")
    import links
    # Companies House supplies names with no website. Each blank domain used to
    # cost 13 DNS timeouts in sniff.py — hours of the run, proving nothing.
    check("no domain, nothing to sniff", sniff.sniff_one(None, {"name": "X", "domain": ""}), None)
    check("no domain key at all", sniff.sniff_one(None, {"name": "X"}), None)
    check_true("a blank domain never becomes a broken link",
               "https:///" not in links.block("Acme Energy", ""))
    import companies_house as ch
    for pc, want in [("EC2V 7NQ", True), ("E14 5AB", True), ("SE1 9SG", True),
                     ("NW1 6XE", True), ("EX1 1AA", False), ("NE1 4ST", False),
                     ("WA1 1AA", False)]:
        check(f"london postcode: {pc}", bool(ch.LONDON_POSTCODES.match(pc)), want)
    for nm, want in [("Acme Bidco Limited", True), ("Sunrise Energy No. 4 Limited", True),
                     ("Green Power III Limited", True), ("Riverside Nominees Ltd", True),
                     ("Mercuria Energy Trading", False), ("Onyx Capital Group", False)]:
        check(f"shell company rejected: {nm[:30]}", bool(ch.JUNK.search(nm)), want)
    check_true("companies house covers finance and energy", len(ch.SIC) >= 25)

    print("\nadd_firms — the two quiet ways to corrupt the registry")
    import add_firms
    have = [{"name": "Vitol", "category": "trading_house", "domain": "vitol.com"}]
    ok, bad = add_firms.add([
        {"name": "Vitol", "category": "trading_house", "domain": "vitol.co"},
        {"name": "Vitol Trading Two", "category": "trading_house", "domain": "vitol.com"},
        {"name": "", "category": "fund", "domain": "x.com"},
        {"name": "Kpler", "category": "data_vendor", "domain": "kpler.com"},
        {"name": "Kpler Two", "category": "data_vendor", "domain": "kpler.com"},
        {"name": "No Website Firm", "category": "fund", "domain": ""},
        {"name": "Also No Website", "category": "fund", "domain": ""},
    ], have)
    check("only the genuinely new are accepted",
          [r["name"] for r in ok], ["Kpler", "No Website Firm", "Also No Website"])
    check("and the reasons are given",
          [why for _, why in bad],
          ["duplicate name", "domain already claimed", "no name", "domain already claimed"])
    check_true("blank domains never collide with each other",
               sum(1 for r in ok if not r["domain"]) == 2)

    print("\ncheck_firms — a domain must prove it belongs to the firm")
    import check_firms
    import http_client as _hc
    check("short all-noise names still have something to match",
          (check_firms.name_tokens("BP"), check_firms.name_tokens("SSE")), (["bp"], ["sse"]))
    check("corporate furniture is not identity",
          check_firms.name_tokens("Harbour Energy Capital Management"), ["harbour"])
    ident = check_firms.page_identity(
        '<title>Harbour Energy | Home</title><h1>Welcome</h1>'
        '<footer>&copy; 2026 Harbour Energy plc</footer>')
    check_true("identity read from title and copyright", "harbour energy" in ident, ident[:60])

    class Resp:
        def __init__(self, url, code=200, text=""):
            self.url, self.status_code, self.text = url, code, text
            self.encoding, self.headers = "utf-8", {}

    saved_get = _hc.get
    try:
        firm = {"name": "Harbour Energy", "domain": "harbourenergy.com"}
        _hc.get = lambda u, **k: Resp("https://harbourenergy.com/",
                                     200, "<title>Harbour Energy</title>" + "energy " * 30)
        check("right company passes", check_firms.check_one(None, firm)["verdict"], "ok")
        _hc.get = lambda u, **k: Resp("https://plumbing.example/", 200,
                                     "<title>Bob's Plumbing</title>" + "pipes " * 30)
        check("someone else's site is caught",
              check_firms.check_one(None, firm)["verdict"], "moved")
        _hc.get = lambda u, **k: None
        check("dead domain is caught",
              check_firms.check_one(None, firm)["verdict"], "unreachable")
        _hc.get = lambda u, **k: Resp("https://harbourenergy.com/", 200,
                                     "<title>This domain is for sale</title>" + "parked " * 30)
        check("parked domain is caught",
              check_firms.check_one(None, firm)["verdict"], "mismatch")
        check("a firm with no domain is not a failure",
              check_firms.check_one(None, {"name": "X", "domain": ""})["verdict"], "ok")

        # An over-eager check is worse than none: the first version blanked 661
        # of 2303 domains, most of them correct. Each case below was a real
        # false positive that removed a real firm from every future run.
        for code in (403, 503, 429):
            _hc.get = lambda u, c=code, **k: Resp("https://abnamro.com/", c, "")
            r = check_firms.check_one(None, {"name": "ABN AMRO", "domain": "abnamro.com"})
            check(f"http {code} is a bot wall, not a dead domain", r["verdict"], "blocked")

        _hc.get = lambda u, **k: Resp("https://validate.perfdrive.com/", 200,
                                      "<title>Access Denied</title>" + "x " * 30)
        check("a bot challenge page is not an acquisition",
              check_firms.check_one(None, {"name": "Acerinox", "domain": "acerinox.com"})["verdict"],
              "blocked")

        _hc.get = lambda u, **k: Resp("https://bank-abc.com:443/", 200,
                                      "<title>Bank ABC</title>" + "banking " * 30)
        check("same host on an explicit port is not a move",
              check_firms.check_one(None, {"name": "Arab Banking Corporation",
                                           "domain": "bank-abc.com"})["verdict"], "ok")

        _hc.get = lambda u, **k: Resp("https://cez.cz/", 200,
                                      "<title>Skupina ČEZ</title>" + "energie " * 30)
        check("a site naming itself with diacritics still matches",
              check_firms.check_one(None, {"name": "CEZ Group", "domain": "cez.cz"})["verdict"], "ok")

        _hc.get = lambda u, **k: Resp("https://bimco.org/", 200,
                                      "<title>BIMCO</title>" + "shipping " * 30)
        check("the domain's own label counts as identity",
              check_firms.check_one(None, {"name": "Baltic and International Maritime Council",
                                           "domain": "bimco.org"})["verdict"], "ok")
    finally:
        _hc.get = saved_get

    print("\nnew boards — one tolerant reader, several shapes")
    import discover, json as _json
    shapes = {
        "rippling": [{"name": "Market Analyst", "url": "https://x/1", "workplace_city": "London"}],
        "pinpoint": {"data": [{"title": "Battery Storage Analyst", "url": "https://x/2",
                               "location": {"name": "London"}}]},
        "comeet": {"positions": [{"name": "Flexibility Analyst", "url": "https://x/3",
                                  "location": "London"}]},
        "jobvite": {"jobs": [{"title": "Power Market Modeller", "applyUrl": "https://x/4",
                              "city": "London"}]},
    }
    for ats, payload in shapes.items():
        got = scrape.norm(ats, "TestCo", payload)
        check(f"{ats}: job read out", (len(got), got[0]["location"] if got else None),
              (1, "London"))
        check(f"{ats}: discovery confirms a hit", discover.count_jobs(ats, _json.dumps(payload)), 1)
    check("junk rows are not invented", scrape.norm("pinpoint", "X", {"data": ["junk", 42, {}]}), [])
    weights = yaml.safe_load(open("scoring.yaml"))["source_weights"]
    for src in ("rippling", "pinpoint", "comeet", "jobvite"):
        # a new source missing from either table is worse than not having it:
        # the direct apply link silently loses to Indeed
        check_true(f"{src} outranks an aggregator",
                   store.SOURCE_RANK[src] > store.SOURCE_RANK["indeed"])
        check_true(f"{src} is paid its provenance points",
                   weights.get(src, 0) > weights.get("indeed", 0))

    print("\nverify — parsers")
    check("years: range takes the floor", verify.years_required("3-5 years experience"), 3)
    check("years: plus form", verify.years_required("5+ years of experience"), 5)
    check("years: absent", verify.years_required("no numbers here"), None)
    check("years: firm's own boast ignored",
          verify.years_required("We have over 30 years of experience in commodities"), None)
    check("years: 'our ideal candidate has' is still a requirement",
          verify.years_required("Our ideal candidate has 5 years of experience"), 5)
    check("years: boast does not mask a real requirement",
          verify.years_required("Our team has 40 years of experience. You will bring "
                                "2 years of experience in python."), 2)
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

    # Real job descriptions run to thousands of characters; scoring now ignores
    # anything under min_jd_chars because a short "description" is a cookie
    # banner or a login wall, not a spec. Test fixtures have to look like the
    # real thing or they measure the gate instead of the rubric. The filler is
    # deliberately inert — no word in it matches any description_signal.
    def jd(text):
        filler = ("The successful applicant will join our London office and work "
                  "alongside the wider team. Further details are available on request. ")
        return text + " " + filler * (1 + cfg["min_jd_chars"] // len(filler))

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
    edge = score.score_job(job(description=jd("remit surveillance market abuse python commodit")),
                           cfg, cats, set())[0]

    check_true("baseline good job scores well", good >= 60, f"got {good}")
    check_true("8 years of experience is penalised", senior < good - 30, f"{senior} vs {good}")
    check_true("unverified ranks below verified", unverified < good, f"{unverified} vs {good}")
    check_true("dead job is removed from contention", dead < 0, f"got {dead}")
    # --everything drops the minimum score to 0 rather than going negative,
    # which only keeps dead links out because the dead penalty is heavy
    check_true("a dead link still fails a zero minimum score", dead < 0, f"got {dead}")
    check_true("anonymous employer penalised", anon < good, f"{anon} vs {good}")
    check_true("ghost repost penalised", ghost < good, f"{ghost} vs {good}")
    check_true("direct beats aggregator", good > viaboard, f"{good} vs {viaboard}")
    check_true("regulatory background rewarded", edge > good, f"{edge} vs {good}")

    # Junior is weighted above everything else. Student intake is not: a
    # graduate scheme or internship wants a current undergraduate, so it earns
    # nothing for its title however well it matches otherwise.
    junior = score.score_job(job(title="Junior Market Analyst"), cfg, cats, set())[0]
    trainee = score.score_job(job(title="Trainee Commodity Analyst"), cfg, cats, set())[0]
    trains = score.score_job(job(title="Market Analyst",
                                 description=jd("full training provided, no prior experience")),
                             cfg, cats, set())[0]
    check_true("junior beats the same role without the word",
               junior > good + 30, f"{junior} vs {good}")
    check_true("trainee counts as junior too", trainee > good + 30, f"{trainee} vs {good}")
    check_true("'no prior experience' in the description is worth real points",
               trains > good + 15, f"{trains} vs {good}")

    # verify.py stores whatever came back, and on a JS-rendered or walled page
    # that is furniture. no_experience_needed is the biggest single description
    # signal at 25 points, so a stray "entry level" in a nav menu could push an
    # unparsed page onto the shortlist on no evidence whatsoever.
    banner = "We use cookies. Entry level roles available. Accept all. Manage preferences."
    check_true("a short page is under the floor", len(banner) < cfg["min_jd_chars"])
    junk = score.score_job(job(description=banner), cfg, cats, set())
    check("no description signal fires on an unreadable page",
          [l for l, _ in junk[1] if l.startswith("jd:")], [])
    check_true("and it says so rather than failing silently",
               any("too short" in l for l, _ in junk[1]))
    check("an unreadable page scores exactly as a missing one",
          junk[0], score.score_job(job(description=""), cfg, cats, set())[0])
    check_true("but a real description still scores",
               score.score_job(job(description=jd("no prior experience")), cfg, cats,
                               set())[0] > junk[0] + 15)

    for title in ("Graduate Commodity Analyst", "Summer Analyst Programme",
                  "Trading Internship", "Sales and Trading Graduate Programme",
                  "Commercial Placement Year", "Off-Cycle Analyst"):
        pts, why = score.score_job(job(title=title), cfg, cats, set())
        tier = [(l, n) for l, n in why if l.startswith("title:")]
        check(f"student intake earns nothing for its title: {title[:34]}",
              tier, [("title:student_only", 0)])
        check_true(f"and ranks below an ordinary match: {title[:30]}",
                   pts < good, f"{pts} vs {good}")
    check_true("no junior bonus for a graduate scheme either",
               not any(l == "junior title" for l, _ in
                       score.score_job(job(title="Graduate Analyst"), cfg, cats, set())[1]))

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

    print("\nseniority — the hard filters")
    excl = score.load_excludes()
    for title, want in [("Senior Market Analyst", True), ("Snr Quant Researcher", True),
                        ("Sr. Data Scientist", True), ("Head of Trading", True),
                        ("Chief Data Officer", True), ("Staff Engineer", True),
                        ("Data Scientist II", True), ("Team Lead, Analytics", True),
                        ("Market Analyst", False), ("Junior Trader", False),
                        ("Graduate Trader", False), ("Quantitative Researcher", False)]:
        check(f"excluded at scoring: {title}", bool(excl.search(title)), want)

    cap = cfg["seniority"]["exclude_over_years"]
    check("hard cap is 2 years", cap, 2)

    # a parser fix must reach rows verified before it landed, or the hard filter
    # keeps acting on numbers the old parser produced
    tmp3 = tempfile.mktemp(suffix=".db")
    c3 = verify.store.connect(tmp3)
    c3.execute("""CREATE TABLE verify (id TEXT PRIMARY KEY, checked_at TEXT, status INTEGER,
        live INTEGER, final_url TEXT, title_match REAL, reason TEXT, employer TEXT, posted TEXT,
        valid_through TEXT, years_required INTEGER, anonymous INTEGER, agency INTEGER,
        desc_len INTEGER, description TEXT)""")
    c3.execute("INSERT INTO verify (id, years_required, agency, description) VALUES (?,?,?,?)",
               ("j1", 30, 0, "We have over 30 years of experience. You will bring 2 years "
                             "of experience in python."))
    c3.commit()
    seen, changed = verify.reparse(c3)
    check("reparse revisits stored descriptions", (seen, changed), (1, 1))
    check("stale 30y corrected to the real 2y",
          c3.execute("SELECT years_required FROM verify WHERE id='j1'").fetchone()[0], 2)
    c3.close()
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(tmp3 + suffix):
            os.unlink(tmp3 + suffix)
    # the drop must beat the rubric: an 8-year role at a top-category firm still goes
    for yrs, want_dropped in [(None, False), (1, False), (2, False), (3, True), (8, True)]:
        check(f"{yrs} years required -> dropped: {want_dropped}",
              yrs is not None and yrs > cap, want_dropped)

    print("\ndigest content — the description and requirements reach the email")
    page = ("Home About Careers  About the role  We are looking for a Junior Gas Scheduler to join "
          "our London gas desk. You will manage daily nominations across UK pipelines. "
          "What you'll need  A numerate degree, 1-2 years of experience in energy, strong SQL. "
          "Benefits  Pension, bonus. We are an equal opportunities employer.")
    s, w = score.summarise(page), score.requirements(page)
    check_true("summary starts at the role, not the site navigation",
               s.startswith("We are looking for"), s[:50])
    check_true("summary stops before the requirements", "numerate degree" not in s, s[-50:])
    check_true("requirements are pulled out separately", "numerate degree" in w, w[:60])
    check_true("boilerplate is left out of both",
               "Pension" not in s and "Pension" not in w and "equal opport" not in w)
    # "essential" mid-sentence is not a heading — it used to start the excerpt there
    plain = ("Kpler is hiring an oil market analyst. The candidate will have a degree in a "
             "quantitative subject and proficiency in Python. Large datasets is essential.")
    check_true("a mid-sentence 'essential' does not fake a requirements heading",
               score.requirements(plain).startswith("The candidate"),
               score.requirements(plain)[:40])
    check("no description is not a crash", (score.summarise(None), score.requirements("")), ("", ""))

    rendered = score.render_md(
        [{"score": 91, "title": "Junior Gas Scheduler", "company": "Vitol", "location": "London",
          "url": "https://x/1", "years_required": 1, "why": [("title:junior", 45)],
          "summary": s, "requirements": w}], [],
        {"date": "1 Jan", "scraped": 1, "verified": 1, "dead": 0, "ghosts": 0, "filtered": 0})
    for must in ("Vitol", "London", "1y required", "We are looking for", "WANTS:"):
        check_true(f"plain-text email carries: {must}", must in rendered)

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

    print("\nnotify — delivery")
    import smtplib
    import notify

    saved_env = {k: os.environ.get(k) for k in
                 ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS", "DIGEST_TO",
                  "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")}
    saved_smtp = smtplib.SMTP
    for k in saved_env:
        os.environ.pop(k, None)
    try:
        check("no delivery configured sends nothing", notify.send_email("s", "<p>h</p>", "t"), False)
        check("telegram unconfigured is a no-op", notify.send_telegram("t"), False)

        sent = {}

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent.update(host=host, port=port)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def starttls(self):
                sent["tls"] = True

            def login(self, u, p):
                sent["user"] = u

            def send_message(self, m):
                sent["msg"] = m

        smtplib.SMTP = FakeSMTP
        # an unset GitHub secret arrives as "", not as absent — int("") used to raise
        # in here and get swallowed, so the digest silently never sent
        os.environ.update(SMTP_HOST="smtp.gmail.com", SMTP_PORT="", SMTP_USER="me@gmail.com",
                          SMTP_PASS="app-pw", DIGEST_TO="you@gmail.com")
        ok = notify.send_email("3 new roles", "<p>report</p>", "report")
        check("empty SMTP_PORT falls back to 587", (ok, sent.get("port")), (True, 587))
        check("starttls before login", sent.get("tls"), True)
        check("digest addressed to DIGEST_TO", sent["msg"]["To"], "you@gmail.com")

        # A source that has never returned anything never appears in the
        # trailing-average comparison, so it used to be invisible: six sources
        # were contributing nothing while the digest looked perfectly healthy.
        tmp4 = tempfile.mktemp(suffix=".db")
        c4 = store.connect(tmp4)
        store.save_new(c4, [{"company": "Kpler", "title": "Gas Analyst",
                             "url": "https://a", "source": "greenhouse"}])
        # Explicitly cleared, not merely assumed absent. The GitHub workflow
        # exports NO_LINKEDIN=1 for the whole step, so this block inherited it
        # and the assertion below failed on the runner while passing locally —
        # which aborted a whole run at the gate. A test that reads ambient
        # environment is a test that passes on your machine and nowhere else.
        os.environ.pop("NO_LINKEDIN", None)
        _, warns = notify.health(c4)
        named = " ".join(warns)
        check_true("every never-working source is accounted for somewhere",
                   all(s in named for s in notify.ALWAYS_CALLED),
                   [s for s in notify.ALWAYS_CALLED if s not in named])

        # Four states, not two. Eight permanent alarms nobody could act on is
        # how a health section stops being read — and skimming it is exactly
        # when a real regression slips past. A missing API key is not a fault,
        # and an already-diagnosed fault is not news.
        check("sources with no key are grouped into one actionable line",
              len([w for w in warns if w.startswith("no API key set")]), 1)
        check("already-diagnosed faults are stated once, not alarmed weekly",
              len([w for w in warns if w.startswith("known broken")]), 1)
        check_true("and a key-less source is never called 'unexplained'",
                   not any("reed" in w and "unexplained" in w for w in warns))
        unexplained = [w.split(":")[0] for w in warns if "nothing explains why" in w]
        check_true("only genuinely unexplained silence gets its own alarm",
                   set(unexplained).isdisjoint(set(notify.NEEDS_KEY) | set(notify.KNOWN_BROKEN)),
                   unexplained)

        # Whatever list it is on, a source that starts producing drops off.
        # Distinct titles on purpose: same company + same title is one job, so
        # a shared title would collapse these two rows into one and only the
        # better-ranked source would survive.
        for src in ("reed", "glassdoor"):
            store.save_new(c4, [{"company": f"Firm {src}", "title": f"{src} Analyst",
                                 "url": f"https://b/{src}", "source": src}])
        _, warns2 = notify.health(c4)
        check_true("a source that starts working stops being reported",
                   not any(src in w for w in warns2 for src in ("reed", "glassdoor")),
                   warns2)

        # "Not run here" and "ran and returned nothing" need different answers
        # from the reader. LinkedIn is deliberately off on GitHub Actions, and
        # reporting that as silence made it look like LinkedIn simply had no
        # London jobs for six weeks running.
        os.environ["NO_LINKEDIN"] = "1"
        _, warns3 = notify.health(c4)
        li = [w for w in warns3 if w.startswith("linkedin:")]
        check("linkedin reported as skipped, not as broken",
              (len(li), "not run" in li[0] if li else None), (1, True))
        os.environ.pop("NO_LINKEDIN")
        _, warns4 = notify.health(c4)
        check_true("and as broken again when it is meant to run",
                   any(w.startswith("linkedin:") and "never returned" in w for w in warns4))
        c4.close()
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(tmp4 + suffix):
                os.unlink(tmp4 + suffix)
        check("sent as text plus html",
              [p.get_content_type() for p in sent["msg"].walk()],
              ["multipart/alternative", "text/plain", "text/html"])
    finally:
        smtplib.SMTP = saved_smtp
        for k, v in saved_env.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v

    print("\nconfig — filter sanity")
    inc = re.compile("|".join(yaml.safe_load(open("config.yaml"))["include"]), re.I)
    exc = re.compile("|".join(yaml.safe_load(open("config.yaml"))["exclude"]), re.I)
    for title, want in [("Commodity Analyst", True), ("Junior Trader", True),
                        ("Data Scientist, Trading", True), ("Head of Trading", False),
                        ("Credit Risk Analyst", False), ("Trade Support Analyst", False),
                        ("Clearing Operations", False), ("Marketing Manager", False),
                        ("Short-Term Power Analyst", True), ("Quantitative Researcher", True),
                        # the widened vocabulary — these are the roles the old
                        # include list silently walked past
                        ("Battery Storage Analyst", True), ("Flexibility Analyst", True),
                        ("Electricity Market Analyst", True), ("Market Surveillance Analyst", True),
                        ("Power Market Modeller", True), ("Renewables Analyst", True),
                        ("Asset Optimisation Analyst", True), ("REMIT Compliance Analyst", True),
                        # and the widened exclusions still bite on the same words
                        ("Senior Battery Storage Analyst", False),
                        ("Head of Flexibility", False), ("Electricity Trader II", False),
                        # early career, collected on the programme alone
                        ("Graduate Scheme, Commodities", True), ("Trading Internship", True),
                        ("Summer Analyst Programme", True), ("Commercial Placement Year", True),
                        ("Apprentice Trader", True), ("Off-Cycle Analyst", True),
                        ("School Leaver Programme", True),
                        # a blanket "sales" exclusion used to throw this one out
                        ("Sales and Trading Graduate Programme", True),
                        ("Sales Executive", False), ("Account Manager", False)]:
        check(f"filter: {title}", bool(inc.search(title)) and not exc.search(title), want)

    print("\nrubric coverage — everything collected must be scorable")
    # A title the filter collects but no tier matches scores zero for its title
    # and lands mid-table on firm and freshness alone. That is how a prime
    # target quietly ranks below an ordinary one.
    for title in ("Battery Storage Analyst", "Electricity Market Analyst",
                  "Market Surveillance Analyst", "Trade Surveillance Analyst",
                  "Market Abuse Analyst", "Renewables Analyst",
                  "Power Market Modeller", "Asset Optimisation Analyst",
                  "Flexibility Analyst", "Regulatory Analyst",
                  "Gas Scheduler", "Cargo Operator", "Trade Operations Analyst",
                  "Market Risk Analyst", "Commodity Risk Analyst", "Product Control Analyst",
                  "Energy Economist", "ESG Analyst", "Climate Risk Analyst",
                  "Commercial Analyst", "PPA Analyst", "Catastrophe Modelling Analyst",
                  "Underwriting Assistant", "Investment Banking Analyst",
                  "Sale and Purchase Broker", "Investment Analyst",
                  "Transaction Reporting Analyst", "Demurrage Analyst"):
        tier = [l for l, _ in score.score_job(job(title=title), cfg, cats, set())[1]
                if l.startswith("title:")]
        check_true(f"scored on its title: {title[:34]}", bool(tier), "no tier matched")

    print("\nFCA routes — the moves an in-post regulator analyst can actually make")
    fca_keep = scrape.build_filter(yaml.safe_load(open("config.yaml")))
    for title in ("Transaction Reporting Analyst", "Regulatory Reporting Analyst",
                  "MiFID Reporting Analyst", "EMIR Analyst", "Trade Surveillance Analyst",
                  "Market Abuse Analyst", "Financial Crime Analytics Analyst",
                  "Conduct Risk Analyst", "Compliance Data Analyst",
                  "Investment Analyst", "Infrastructure Investment Analyst",
                  "Credit Analyst", "Fund Analyst"):
        check_true(f"collected: {title[:36]}", fca_keep({"title": title, "location": "London"}))
    # the regulatory signal stacks on the title tier, so these clear the rest
    reg = score.score_job(job(title="Transaction Reporting Analyst",
                              description=jd("mifid transaction reporting and market abuse "
                                             "surveillance, python and sql")), cfg, cats, set())[0]
    plain = score.score_job(job(title="Commodity Analyst",
                                description=jd("python and sql")), cfg, cats, set())[0]
    check_true("a reporting/surveillance role outranks a plain commodity analyst",
               reg > plain, f"{reg} vs {plain}")
    # no pattern may be silently dead — a Cyrillic lookalike once made one unmatchable
    for spec in list(cfg["description_signals"].values()) + list(cfg["title_tiers"].values()):
        for p in spec["patterns"]:
            check_true(f"pattern is ascii and live: {p[:32]}", all(ord(c) < 128 for c in p))

    print("\ndedupe — one job advertised twice under different titles")
    D1 = "We are hiring an energy operations analyst for the London desk. " * 12
    D2 = "A different posting about broking rates products in London. " * 12
    dup_rows = [
        dict(company="SmartestEnergy", title="Energy Operations Analyst", description=D1, score=70),
        dict(company="SmartestEnergy", title="Energy Operations Analyst - Renewables",
             description=D1, score=65),
        dict(company="Ebury", title="FX Sales", description=D1, score=60),
        dict(company="Ebury", title="FX Sales - London", description=D1, score=55),
        # two desks at one firm: different descriptions, must stay separate
        dict(company="Tradition", title="Broker - FX Options", description=D1, score=50),
        dict(company="Tradition", title="Broker - Rates", description=D2, score=50),
        # same boilerplate description, unrelated titles: must stay separate
        dict(company="Acme", title="Power Analyst", description=D2, score=40),
        dict(company="Acme", title="Gas Scheduler", description=D2, score=39),
        # nothing readable: an empty description matches every other empty one,
        # so it must never be treated as evidence that two rows are one job
        dict(company="Beta", title="Analyst", description="", score=30),
        dict(company="Beta", title="Analyst - Two", description="", score=29),
    ]
    kept_rows, merged_rows = score.collapse_duplicates(dup_rows)
    check("only the title-superset duplicates merge",
          sorted(o["title"] for o, _ in merged_rows),
          ["Energy Operations Analyst - Renewables", "FX Sales - London"])
    check_true("the better-scoring copy is the one kept",
               all(k["score"] > o["score"] for o, k in merged_rows))
    check("everything else survives", len(kept_rows), 8)
    check_true("two desks at one firm are not merged",
               sum(1 for r in kept_rows if r["company"] == "Tradition") == 2)
    check_true("shared boilerplate does not merge unrelated titles",
               sum(1 for r in kept_rows if r["company"] == "Acme") == 2)
    check_true("rows with no readable description are never merged",
               sum(1 for r in kept_rows if r["company"] == "Beta") == 2)
    check("a short description has no fingerprint", score.jd_fingerprint("too short"), None)
    check_true("and the same posting fingerprints the same through whitespace",
               score.jd_fingerprint(D1) == score.jd_fingerprint("  " + D1.upper().replace(" ", "  ")))

    print("\ninbox — roles collected on another machine")
    import inbox
    inb = tempfile.mktemp(suffix=".csv")
    with open(inb, "w", newline="") as fh:
        fh.write("company,title,location,url,source,posted\n")
        fh.write('Citadel,"Analyst, Global Markets",London,https://li/1,linkedin,\n')
        fh.write("Badco,Delivery Driver,London,https://li/2,linkedin,\n")
        fh.write("Nowhere,Market Analyst,Singapore,https://li/3,linkedin,\n")
    got = inbox.read(inb)
    check("every row read back", len(got), 3)
    # The file comes from another machine running another checkout of the
    # config, so the gate into the database has to re-apply the current rules.
    keep2 = scrape.build_filter(yaml.safe_load(open("config.yaml")))
    check("inbox re-filters on ingest",
          [j["company"] for j in got if keep2(j)], ["Citadel"])
    check("a missing inbox is a no-op, not an error", inbox.read("does-not-exist.csv"), [])
    os.unlink(inb)

    print("\nboards: per-site accounting, and the Glassdoor location bug")
    import boards
    # Glassdoor is known broken upstream — 400 from its location endpoint even
    # with a bare city name, confirmed from Railway. These assert the input is
    # well-formed, not that Glassdoor works; boards.py reports its zero loudly.
    check("glassdoor gets a bare city name",
          boards.SITE_LOCATION.get("glassdoor"), "London")
    check_true("everything else keeps the full location",
               boards.SITE_LOCATION.get("indeed") is None
               and boards.LOCATION == "London, United Kingdom")
    # A comma or space in the term lands unescaped in Glassdoor's location URL
    # and it answers 400 to every query, which is what happened for six runs.
    for site, loc in list(boards.SITE_LOCATION.items()) or []:
        check_true(f"{site} location has no character that breaks a bare URL",
                   not any(ch in loc for ch in ", &?#"))

    print("\nfilter precision — the widened patterns must not go permissive")
    keep = scrape.build_filter(yaml.safe_load(open("config.yaml")))
    # Real titles that sit next to the words this filter was widened with.
    # Unscoped, "battery" collects technicians, "trainee" dental nurses,
    # "apprentice" chefs, "placement" nursing coordinators and "surveillance"
    # CCTV operators. 20 of these once got through.
    noise = ["Battery Production Operative", "Battery Technician", "Electrician",
             "Electricity Meter Reader", "CCTV Surveillance Operator",
             "Security Surveillance Officer", "Retail Placement Assistant",
             "Nursing Placement Coordinator", "Apprentice Plumber", "Apprentice Chef",
             "Apprentice Electrician", "Trainee Dental Nurse", "Trainee Driving Instructor",
             "Trainee Estate Agent", "Trainee Accountant", "Graduate Nurse",
             "Internship - Fashion PR", "Yacht Chartering Assistant",
             "Structuring Engineer - Buildings", "Origination Manager - Mortgages",
             "Warehouse Operative", "Care Assistant", "Delivery Driver", "Receptionist",
             # neighbours of the ops / risk / economics / insurance vocabulary
             "Bus Scheduler", "Production Scheduler", "Forklift Operator", "Crane Operator",
             "Warehouse Operations Assistant", "Retail Operations Assistant",
             "Fire Risk Assessor", "Risk and Compliance Officer", "Credit Risk Analyst",
             "Economics Teacher", "Sustainability Officer - Council", "Climate Campaigner",
             "ESG Marketing Executive", "Commercial Manager - Construction",
             "Commercial Director", "Underwriting Manager", "Actuarial Director",
             "Corporate Finance Manager", "Insurance Broker - Motor", "Mortgage Broker",
             "Broker Support Administrator", "Recruitment Consultant",
             # neighbours of the pair matcher (role word + domain word). These
             # all carry one half of a real title and must still be rejected.
             "Head of Trading", "Senior Markets Analyst", "Trading Floor Cleaner",
             "Insurance Sales Broker", "Commercial Insurance Broker",
             "Energy Advisor - Call Centre", "Gas Engineer", "Gas Safe Engineer",
             "Oil Rig Roustabout", "Shipping Clerk", "Cargo Handler",
             "Policy Advisor - Housing", "Sports Performance Analyst",
             "Debt Collector", "Debt Advisor", "Portfolio Manager", "Head of Risk",
             "Risk Manager", "Trading Standards Officer", "Estate Agent"]
    signal = ["Junior Market Analyst", "Trainee Commodity Broker",
              "Entry Level Trading Analyst", "Battery Storage Optimisation Analyst",
              "Electricity Market Analyst", "Trade Surveillance Analyst",
              "Market Abuse Analyst", "REMIT Analyst", "Graduate Commodity Analyst",
              "Commodity Analyst", "Power Trading Analyst", "LNG Analyst",
              "Quantitative Researcher", "Structuring Analyst",
              "Origination Analyst - Power", "Dry Cargo Chartering Trainee",
              "Assistant Trader", "Commodities Trading Internship",
              # the functions added after measuring 9/62 coverage
              "Gas Scheduler", "Power Scheduler", "Cargo Operator",
              "Trade Operations Analyst", "Deal Capture Analyst", "Demurrage Analyst",
              "Market Risk Analyst", "Commodity Risk Analyst", "Model Validation Analyst",
              "Product Control Analyst", "Valuations Analyst", "Energy Economist",
              "ESG Analyst", "Sustainability Analyst", "Climate Risk Analyst",
              "Commercial Analyst", "PPA Analyst", "Corporate Development Analyst",
              "Catastrophe Modelling Analyst", "Exposure Management Analyst",
              "Underwriting Assistant", "Investment Banking Analyst", "M&A Analyst",
              "Leveraged Finance Analyst", "Sale and Purchase Broker", "Dry Cargo Broker",
              "Shipbroking Trainee", "Transaction Reporting Analyst", "Investment Analyst",
              # The inverted house style. Every include phrase spells
              # "<domain> analyst"; banks write "Analyst, <domain>" at least as
              # often, and 53 of a 103-title sample were being rejected on that
              # alone. These are the forms that were missing.
              "Analyst, Global Markets", "Analyst - Markets", "Markets Analyst",
              "Analyst, Fixed Income", "Global Markets Analyst",
              "Analyst, Commodities Trading", "Analyst - Capital Markets",
              "Capital Markets Analyst", "Derivatives Analyst", "Treasury Analyst",
              "Analyst, Risk", "Financial Analyst", "Analyst, Investment Management",
              "Investment Operations Analyst", "Analyst - Energy Transition",
              "Analyst, Private Credit", "Analyst, Prime Brokerage",
              "Analyst, Electronic Trading", "Analyst, Insurance",
              "Analyst, Model Risk", "Analyst, Asset Management",
              "Real Assets Analyst", "Analyst, Real Estate Investment",
              "Analyst - Hedge Fund", "Hedge Fund Analyst", "Analyst, Macro Research",
              "Macro Analyst", "Rates Analyst", "FX Analyst",
              "Analyst, Foreign Exchange", "Distressed Debt Analyst",
              "Analyst - Corporate Finance", "Analyst, Valuations",
              "Analyst - Financial Crime", "Analyst - Conduct Risk",
              "Analyst, Prudential Risk", "Policy Analyst",
              "Analyst, Financial Stability", "Equity Research Associate",
              "Associate, Equity Research", "Trading Assistant", "Trade Floor Analyst",
              "Business Analyst - Trading", "Broker", "Junior Broker", "Energy Broker",
              "Analyst, Investor Relations", "Analyst, Trading Strategy",
              # a junior marker outranks a rank word in the exclude list
              "Junior Portfolio Manager"]
    caught = [t for t in noise if keep({"title": t, "location": "London"})]
    missed = [t for t in signal if not keep({"title": t, "location": "London"})]
    check(f"none of {len(noise)} unrelated titles collected", caught, [])
    check(f"all {len(signal)} on-target titles collected", missed, [])

    print("\nlocation filter — London roles are labelled many ways")
    for l in ["London", "London, United Kingdom", "City of London", "Canary Wharf",
              "EC2M, England", "England", "GB", "Great Britain", "London (Hybrid)",
              "EMEA", "Reading, England", "UK-London", ""]:
        check_true(f"location kept: {l or '(blank)'}",
                   keep({"title": "Market Analyst", "location": l}))
    for l in ["Ukraine", "Paris, France", "Geneva", "Singapore", "Houston, TX",
              "New York, NY", "Dubai", "Sydney"]:
        check_true(f"location dropped: {l}",
                   not keep({"title": "Market Analyst", "location": l}))

    # The pair matcher must need BOTH halves. A role word alone or a domain word
    # alone getting through is how it would silently become a single-word match.
    for half in ["Analyst", "Associate", "Trader", "Researcher", "Manager",
                 "Markets", "Energy", "Risk", "Investment", "Pricing"]:
        check_true(f"bare '{half}' alone is not enough",
                   not keep.matches(half) or half.lower() in
                   " ".join(yaml.safe_load(open("config.yaml"))["include"]).lower())

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
        sys.exit(1)
    print("all checks passed")


if __name__ == "__main__":
    main()
