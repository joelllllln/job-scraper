#!/usr/bin/env python3
"""
score.py — rank verified jobs and write the report.

Reads jobs.db (scraped) + the verify table (checked), applies the rubric in
scoring.yaml, and writes report.html, report.md and scored.csv.

    python verify.py     # first — nothing scores well without this
    python score.py
    python score.py --include-unverified   # see the raw pile too

Every score is explained: each job carries the list of rules that fired and what
each was worth, so when something ranks high you can see exactly why and adjust
scoring.yaml rather than guessing.
"""

import argparse
import csv
import html
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

import os

import yaml

DB = "jobs.db"


def ensure_schema(con):
    """Weekly running needs two things: a record of when we last ran, and a
    status per job so applied/ignored roles never resurface."""
    con.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY, ran_at TEXT, n_new INTEGER)")
    cols = [r[1] for r in con.execute("PRAGMA table_info(jobs)")]
    if "status" not in cols:
        con.execute("ALTER TABLE jobs ADD COLUMN status TEXT DEFAULT 'new'")
    con.commit()


def last_run(con):
    row = con.execute("SELECT ran_at FROM runs ORDER BY id DESC LIMIT 1").fetchone()
    return row[0] if row else None


# ---------- helpers ----------

def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def days_since(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            d = datetime.strptime(s.strip()[:len(fmt) + 6], fmt)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).days
        except ValueError:
            continue
    try:
        d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - d).days
    except Exception:
        return None


def load_excludes(path="config.yaml"):
    """The title exclusions from config.yaml, compiled for use at scoring time.

    config.yaml normally filters at collection. Rows already in the database were
    stored under whatever the rules were on the day they were found, so tightening
    the list would otherwise only change future scraping while the old rows kept
    turning up in every digest. Re-applying the same patterns here makes a rule
    change retroactive over everything already collected.
    """
    try:
        cfg = yaml.safe_load(open(path)) or {}
    except (FileNotFoundError, yaml.YAMLError):
        return None
    pats = cfg.get("exclude") or []
    return re.compile("|".join(pats), re.I) if pats else None


def load_categories(path="firms.csv"):
    cats = {}
    try:
        for r in csv.DictReader(open(path)):
            cats[norm(r["name"])] = r["category"]
    except FileNotFoundError:
        pass
    return cats


# Where a job description starts saying something useful, and where it starts
# listing what it wants. Descriptions arrive as whole stripped pages when there
# was no JSON-LD, so the opening is often navigation rather than the job.
LEAD_IN = re.compile(
    r"(about (the|this) (role|job|position|opportunity)|the role|job (description|purpose)|"
    r"role (overview|summary|purpose)|overview|the opportunity|your role|"
    r"what you.{0,3}ll (do|be doing)|purpose of the role)\b[:\s-]*", re.I)
# Single words like "essential" and "requirements" only count as a heading when
# punctuated as one — otherwise "datasets is essential" is read as the start of
# the requirements section and the excerpt begins mid-sentence.
WANTS = re.compile(
    r"(?:(?:requirements?|qualifications?|essential(?: requirements?| skills| criteria)?)"
    r"\s*[:\-–—]"
    r"|what (?:we|you).{0,4}re looking for|what you.{0,3}ll need|about you"
    r"|skills (?:and|&) experience|your profile|candidate profile|key skills"
    r"|who we are looking for|you will have)\s*[:\-–—]*\s*", re.I)
# Where the useful part of a posting stops and the boilerplate starts.
STOP = re.compile(
    r"\b(benefits|what we offer|the package|salary|remuneration|how to apply|"
    r"equal opportunit|diversity (and|&) inclusion|we are an equal|about us|"
    r"our values|next steps|application process|privacy)\b", re.I)
SENTENCE_END = re.compile(r"(?<=[.!?])\s")


def _until_boilerplate(text):
    m = STOP.search(text or "")
    return text[:m.start()] if m and m.start() > 60 else text


def _trim(text, limit):
    """Cut at a sentence end near the limit so an excerpt doesn't end mid-word."""
    text = re.sub(r"\s+", " ", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    parts = SENTENCE_END.split(cut)
    if len(parts) > 1 and len(" ".join(parts[:-1])) > limit * 0.5:
        return " ".join(parts[:-1]).strip()
    return cut.rsplit(" ", 1)[0].rstrip(" ,;:") + "…"


def summarise(desc, limit=260):
    """A couple of sentences saying what the job is."""
    if not desc:
        return ""
    m = LEAD_IN.search(desc[:4000])
    body = _until_boilerplate(desc[m.end():] if m else desc)
    # and stop where the requirements start — that half is reported separately,
    # so repeating it here just costs the reader the description
    w = WANTS.search(body)
    if w and w.start() > 60:
        body = body[:w.start()]
    return _trim(body, limit)


def requirements(desc, limit=260):
    """What the posting says it wants, taken from its own requirements section."""
    if not desc:
        return ""
    m = WANTS.search(desc)
    if m:
        return _trim(_until_boilerplate(desc[m.end():]), limit)
    # no heading — fall back to the sentences that actually state a requirement
    asks = [s for s in SENTENCE_END.split(re.sub(r"\s+", " ", desc))
            if re.search(r"experience|degree|proficien|knowledge of|familiar with|"
                         r"you (will|should) have|ability to|numerate|qualification", s, re.I)]
    return _trim(" ".join(asks[:3]), limit) if asks else ""


def find_ghosts(rows):
    """Same role, posted again and again over months. Usually never filled."""
    groups = defaultdict(list)
    for r in rows:
        groups[(norm(r["company"]), norm(r["title"]))].append(r)
    ghosts = set()
    for key, items in groups.items():
        if len(items) < 3:
            continue
        seen = sorted(d for d in (days_since(i["first_seen"]) for i in items) if d is not None)
        if seen and (seen[-1] - seen[0]) >= 60:
            for i in items:
                ghosts.add(i["id"])
    return ghosts


# ---------- scoring ----------

def score_job(r, cfg, cats, ghosts):
    pts, why = 0, []

    def add(n, label):
        nonlocal pts
        pts += n
        why.append((label, n))

    title = r["title"] or ""
    desc = (r["description"] or "").lower()

    # 1. title tier — first match wins
    for tier, spec in cfg["title_tiers"].items():
        if any(re.search(p, title, re.I) for p in spec["patterns"]):
            add(spec["points"], f"title:{tier}")
            break

    # 2. seniority
    sen = cfg["seniority"]
    # deliberately no graduate/intern/placement/campus here — those are student
    # intake, and they are handled by the student_only title tier instead
    if re.search(r"\bjunior\b|trainee|entry.?level|early care|assistant|"
                 r"associate|analyst i\b", title, re.I):
        add(sen["junior_markers"], "junior title")
    else:
        add(sen["no_marker"], "no seniority marker")

    yrs = r["years_required"]
    if yrs is not None and yrs > sen["years_cap"]:
        over = yrs - sen["years_cap"]
        add(sen["over_cap_penalty"] + sen["per_year_over"] * (over - 1), f"wants {yrs}y experience")

    # 3. firm category
    cat = cats.get(norm(r["company"]), "unknown")
    add(cfg["firm_categories"].get(cat, cfg["firm_categories"]["unknown"]), f"firm:{cat}")

    # 4. description signals
    for name, spec in cfg["description_signals"].items():
        if any(re.search(p, desc) for p in spec["patterns"]):
            add(spec["points"], f"jd:{name}")

    # 5. provenance
    add(cfg["source_weights"].get(r["source"], 0), f"via {r['source']}")

    # 6. freshness
    d = days_since(r["posted"]) or days_since(r["first_seen"])
    if d is not None:
        for limit, points in cfg["freshness"]:
            if d <= limit:
                if points:
                    add(points, f"{d}d old")
                break

    # 7. verification
    v = cfg["verification"]
    if r["checked_at"] is None:
        add(v["unverified"], "not verified")
    elif not r["live"]:
        add(v["dead"], f"dead: {r['reason']}")
    else:
        add(v["live"], "verified live")
        if r["title_match"] is not None and r["title_match"] < 0.34:
            add(v["title_mismatch"], "page title mismatch")
        if r["anonymous"]:
            add(v["anonymous_employer"], "employer anonymous")
        posted_age = days_since(r["posted"])
        if posted_age and posted_age > 60 and not r["valid_through"]:
            add(v["long_open"], f"open {posted_age}d, no close date")

    if r["id"] in ghosts:
        add(v["ghost_repost"], "reposted repeatedly")

    return pts, why


# ---------- report ----------

CSS = """
:root{--ink:#12161b;--paper:#f4f5f6;--card:#fff;--rule:#d7dbdf;--dim:#5d666f;
--sig:#0b3fd6;--ok:#0b6b45;--warn:#9a5b0c;--bad:#8a2020;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);
font:15px/1.5 "Helvetica Neue",Inter,-apple-system,system-ui,sans-serif;}
.wrap{max-width:920px;margin:0 auto;padding:28px 18px 64px}
h1{font-size:clamp(26px,5vw,38px);letter-spacing:-.02em;margin:0 0 4px;font-weight:650}
.sub{color:var(--dim);font-size:13px;margin-bottom:22px}
.tape{display:flex;flex-wrap:wrap;gap:0;border:1px solid var(--rule);background:var(--card);
margin-bottom:26px}
.tape div{flex:1 1 88px;padding:10px 12px;border-right:1px solid var(--rule)}
.tape div:last-child{border-right:0}
.tape b{display:block;font:600 19px/1.1 ui-monospace,"SF Mono",Menlo,monospace}
.tape span{font-size:10.5px;text-transform:uppercase;letter-spacing:.09em;color:var(--dim)}
h2{font-size:12px;text-transform:uppercase;letter-spacing:.11em;color:var(--dim);
margin:30px 0 10px;font-weight:600}
.job{background:var(--card);border:1px solid var(--rule);border-left:3px solid var(--rule);
padding:13px 15px;margin-bottom:8px;position:relative;overflow:hidden}
.job.top{border-left-color:var(--sig)}
.bar{position:absolute;inset:0 auto 0 0;background:var(--sig);opacity:.055;z-index:0}
.job>*{position:relative;z-index:1}
.row1{display:flex;gap:12px;align-items:baseline;justify-content:space-between}
.t{font-weight:600;font-size:15.5px;letter-spacing:-.01em}
.sc{font:600 17px/1 ui-monospace,"SF Mono",Menlo,monospace;color:var(--sig);flex:0 0 auto}
.meta{font:12px/1.55 ui-monospace,"SF Mono",Menlo,monospace;color:var(--dim);margin-top:3px}
.meta b{color:var(--ink);font-weight:600}
.desc{margin-top:7px;font-size:13.5px;line-height:1.5;color:var(--ink)}
.reqs{margin-top:5px;font-size:13px;line-height:1.5;color:var(--dim)}
.reqs span{font:600 10.5px/1 ui-monospace,Menlo,monospace;text-transform:uppercase;
letter-spacing:.08em;color:var(--warn);margin-right:5px}
.tags{margin-top:7px;display:flex;flex-wrap:wrap;gap:4px}
.tag{font:11px/1 ui-monospace,Menlo,monospace;padding:3.5px 6px;border:1px solid var(--rule);
color:var(--dim);border-radius:2px}
.tag.p{color:var(--ok);border-color:#bfdccd}
.tag.n{color:var(--bad);border-color:#e3c2c2}
a{color:inherit;text-decoration:none}
a.apply{display:inline-block;margin-top:8px;font:12px/1 ui-monospace,Menlo,monospace;
color:var(--sig);border-bottom:1px solid currentColor;padding-bottom:2px}
.empty{padding:26px;border:1px dashed var(--rule);color:var(--dim);font-size:14px}
@media(max-width:520px){.row1{flex-direction:column;gap:2px}}
"""


def render_html(shortlist, rest, stats):
    def card(j, top=False):
        pos = [f'<span class="tag p">{html.escape(l)} +{n}</span>'
               for l, n in j["why"] if n > 0]
        neg = [f'<span class="tag n">{html.escape(l)} {n}</span>'
               for l, n in j["why"] if n < 0]
        width = max(0, min(100, j["score"]))
        return f"""<div class="job{' top' if top else ''}">
<div class="bar" style="width:{width}%"></div>
<div class="row1"><div class="t">{html.escape(j['title'])}</div><div class="sc">{j['score']}</div></div>
<div class="meta"><b>{html.escape(j['company'])}</b> · {html.escape(j['location'] or 'location n/a')} · {html.escape(j['source'])}{' · ' + str(j['years_required']) + 'y required' if j['years_required'] else ''}</div>
{f'<div class="desc">{html.escape(j["summary"])}</div>' if j.get("summary") else ''}
{f'<div class="reqs"><span>Wants</span> {html.escape(j["requirements"])}</div>' if j.get("requirements") else ''}
<div class="tags">{''.join(pos + neg)}</div>
<a class="apply" href="{html.escape(j['url'])}">Open posting</a></div>"""

    body = "".join(card(j, True) for j in shortlist) or \
        '<div class="empty">Nothing cleared the shortlist threshold this run. Lower shortlist_threshold in scoring.yaml, or widen the include patterns in config.yaml.</div>'
    more = "".join(card(j) for j in rest)

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Job run — {stats['date']}</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Job run</h1>
<div class="sub">{stats['date']} · scored against scoring.yaml</div>
<div class="tape">
<div><b>{stats['scraped']}</b><span>scraped</span></div>
<div><b>{stats['verified']}</b><span>verified live</span></div>
<div><b>{stats['dead']}</b><span>failed check</span></div>
<div><b>{stats.get('filtered', 0)}</b><span>too senior</span></div>
<div><b>{len(shortlist)}</b><span>shortlist</span></div>
</div>
<h2>Shortlist</h2>{body}
<h2>Also matched</h2>{more or '<div class="empty">Nothing else above the minimum score.</div>'}
</div></body></html>"""


def render_md(shortlist, rest, stats):
    out = [f"# Job run — {stats['date']}", "",
           f"{stats['scraped']} scraped · {stats['verified']} verified live · "
           f"{stats['dead']} failed · {stats.get('filtered', 0)} too senior · "
           f"{len(shortlist)} shortlisted", "",
           "## Shortlist", ""]
    for j in shortlist:
        out.append(f"**{j['score']} — {j['title']}**")
        loc = j["location"] or "location n/a"
        yrs = f" · {j['years_required']}y required" if j["years_required"] else ""
        out.append(f"  {j['company']} · {loc}{yrs}")
        if j.get("summary"):
            out.append(f"  {j['summary']}")
        if j.get("requirements"):
            out.append(f"  WANTS: {j['requirements']}")
        out.append(f"  {' '.join(f'{l}{n:+d}' for l, n in j['why'])}")
        out.append(f"  {j['url']}")
        out.append("")
    out += ["## Also matched", ""]
    for j in rest:
        out.append(f"{j['score']} — {j['title']} · {j['company']} · {j['url']}")
    return "\n".join(out)


# ---------- main ----------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-unverified", action="store_true")
    ap.add_argument("--top", type=int, default=0)
    ap.add_argument("--new-only", action="store_true",
                    help="only jobs first seen since the last recorded run")
    ap.add_argument("--record", action="store_true",
                    help="stamp this run so the next --new-only starts from here")
    ap.add_argument("--everything", action="store_true",
                    help="report every open role, not a top-N digest: no new-only "
                         "filter, no minimum score, no cap, and include roles "
                         "verification hasn't reached yet")
    args = ap.parse_args()
    if args.everything:
        args.new_only = False
        args.include_unverified = True

    cfg = yaml.safe_load(open("scoring.yaml"))
    cats = load_categories()

    import store
    if not os.path.exists(DB):
        print(f"{DB} not found — run the collectors first")
        return
    con = store.connect(DB)
    con.row_factory = sqlite3.Row
    ensure_schema(con)
    since = last_run(con) if args.new_only else None
    has_verify = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='verify'").fetchone()
    if not has_verify:
        print("no verify table yet — run verify.py first (scoring without it is misleading)")
        con.execute("""CREATE TABLE IF NOT EXISTS verify (id TEXT PRIMARY KEY, checked_at TEXT,
            status INTEGER, live INTEGER, final_url TEXT, title_match REAL, reason TEXT,
            employer TEXT, posted TEXT, valid_through TEXT, years_required INTEGER,
            anonymous INTEGER, agency INTEGER, desc_len INTEGER, description TEXT)""")
        con.commit()

    rows = [dict(r) for r in con.execute("""
        SELECT j.*, v.checked_at, v.live, v.reason, v.title_match, v.employer,
               v.posted AS ld_posted, v.valid_through, v.years_required,
               v.anonymous, v.agency, v.description
        FROM jobs j LEFT JOIN verify v ON v.id = j.id
    """)]
    if not rows:
        print("jobs.db is empty — run the scrapers first")
        return

    ghosts = find_ghosts(rows)   # computed over ALL history, not just this week

    pool = rows
    if since:
        pool = [r for r in rows if (r["first_seen"] or "") > since]
        print(f"new since {since[:16]}: {len(pool)} of {len(rows)}")
    pool = [r for r in pool if (r.get("status") or "new") == "new"]

    # Two hard filters, applied before scoring so nothing over the bar can rank
    # its way back in on the strength of the firm or the freshness.
    excl = load_excludes()
    max_years = cfg["seniority"].get("exclude_over_years")
    cut_title, cut_years, kept = [], [], []
    for r in pool:
        if excl and excl.search(r["title"] or ""):
            cut_title.append(r)
            continue
        yrs = r["years_required"]
        if max_years is not None and yrs is not None and yrs > max_years:
            cut_years.append(r)
            continue
        kept.append(r)
    pool = kept

    # Named, not just counted — a hard filter that drops things silently is how
    # you lose a role you wanted and never find out.
    for label, rows in (("title", cut_title), (f">{max_years}y experience", cut_years)):
        if rows:
            print(f"filtered out {len(rows)} on {label}:")
            for r in sorted(rows, key=lambda x: x["company"] or "")[:12]:
                extra = f" ({r['years_required']}y)" if r["years_required"] else ""
                print(f"    {(r['company'] or '')[:26]:<28} {(r['title'] or '')[:48]}{extra}")
            if len(rows) > 12:
                print(f"    ... and {len(rows) - 12} more")

    for r in pool:
        r["posted"] = r.get("ld_posted") or r.get("posted")
        r["score"], r["why"] = score_job(r, cfg, cats, ghosts)
        # verify.py already stored the description; it just never reached the
        # digest, which meant deciding whether to apply required opening the link
        r["summary"] = summarise(r.get("description"))
        r["requirements"] = requirements(r.get("description"))

    scored = sorted(pool, key=lambda r: -r["score"])
    if not args.include_unverified:
        # Only drop roles PROVEN dead. `checked_at is None` means verify.py never
        # reached this one — it runs under a 40-minute ceiling and gets bot-blocked
        # on some hosts — and treating "not yet checked" as "gone" was quietly
        # binning a fifth of the pool, which is indistinguishable from the filter
        # being too tight. Unknown is not dead.
        scored = [r for r in scored if r["checked_at"] is None or r["live"]]

    rep = cfg["report"]
    top_n = args.top or rep["top_n"]
    min_score = rep["min_score"]
    if args.everything:
        top_n = args.top or 100000
        # 0, not negative: a dead job scores -1000, so this still keeps them out
        # without needing a separate rule
        min_score = 0
    keep = [r for r in scored if r["score"] >= min_score]
    shortlist = [r for r in keep if r["score"] >= rep["shortlist_threshold"]][:top_n]
    # identity, not equality — two rows can compare equal and dict compare is slow
    on_shortlist = {id(r) for r in shortlist}
    rest = [r for r in keep if id(r) not in on_shortlist][:top_n]

    stats = {
        "date": datetime.now(timezone.utc).strftime("%d %b %Y"),
        "scraped": len(pool),
        "verified": sum(1 for r in pool if r["live"]),
        "dead": sum(1 for r in pool if r["checked_at"] and not r["live"]),
        "ghosts": len(ghosts),
        "filtered": len(cut_title) + len(cut_years),
    }

    open("report.html", "w").write(render_html(shortlist, rest, stats))
    open("report.md", "w").write(render_md(shortlist, rest, stats))

    with open("scored.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "company", "title", "location", "source", "years", "url", "why"])
        for r in keep:
            w.writerow([r["score"], r["company"], r["title"], r["location"], r["source"],
                        r["years_required"] or "", r["url"],
                        "; ".join(f"{l}{n:+d}" for l, n in r["why"])])

    print(f"{stats['scraped']} scraped · {stats['verified']} live · {stats['dead']} failed "
          f"· {stats['filtered']} too senior · {stats['ghosts']} ghosts "
          f"· {len(shortlist)} shortlisted\n")
    for r in shortlist:
        print(f"  {r['score']:>4}  {r['company'][:26]:<28} {r['title'][:50]}")
    if args.record:
        con.execute("INSERT INTO runs (ran_at, n_new) VALUES (?, ?)",
                    (datetime.now(timezone.utc).isoformat(timespec="seconds"), len(keep)))
        con.commit()

    print("\nwrote report.html, report.md, scored.csv")


if __name__ == "__main__":
    main()
