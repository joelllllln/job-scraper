# Trading / market analyst job scraper

2319 firms, London-focused. Front office and market-facing roles only.

## Running it on your own machine

This is the way to run it. LinkedIn blocks datacentre IPs, so the hosted run on
GitHub Actions has it switched off — from a home connection it works, and it is
the biggest single source the hosted run cannot reach.

```bash
pip install -r requirements.txt
python run.py
```

The first run asks for your Gmail address and an **app password** (not your
normal password: myaccount.google.com -> Security -> 2-Step Verification ->
App passwords). It saves them to `.env`, which is gitignored. After that,
`python run.py` is the whole command. It takes 30-60 minutes and emails you
when it is done.

Works on Windows, macOS and Linux — it is Python, not bash. If one source is
down the rest still run, nothing hangs forever, and partial results are kept.

### The browser pass — worth doing once

```bash
pip install playwright && playwright install chromium
python run.py --render
```

86% of firms with no discoverable ATS serve their careers page fine and simply
have no ATS link in the HTML: the listings are drawn by JavaScript. `--render`
opens a real browser and reads what a person would see.

Do this **once**, not weekly. When it finds a firm's ATS it writes it to
`sniffed.csv`, and from then on the normal run reads that firm through its API
in milliseconds. The browser is how you discover a board, not how you read it.
Expect a few hours for the full registry; `--render-limit 100` takes a bite.

### The other flags

```bash
python run.py --report-all    # email every open role, not just new ones
python run.py --full          # re-check every firm's ATS from scratch
python run.py --no-email      # write report.html, send nothing
python run.py --no-linkedin   # if LinkedIn starts rate limiting you
python run.py --setup         # change the saved settings
```

### Worth 15 minutes: the free API keys

Four aggregators index the firms whose own sites cannot be read — which is most
of them. All free, all currently unset, and together they are worth more than
any scraping work:

| Key | Where |
|---|---|
| `REED_API_KEY` | reed.co.uk/developers |
| `ADZUNA_APP_ID`, `ADZUNA_APP_KEY` | developer.adzuna.com |
| `JOOBLE_API_KEY` | jooble.org/api/about |
| `CAREERJET_AFFID` | careerjet.co.uk/partners |

`python run.py --setup` prompts for Reed and Adzuna; the rest go in `.env` by
hand, one `KEY=value` per line.

## Run order

```bash
pip install requests pyyaml python-jobspy

python sniff.py        # 1. read every firm's careers page, fingerprint its ATS
python discover.py     # 2. token-guess fallback for anything sniff missed
python workday.py      # 3. Workday tenants (BP, Shell, Vitol, banks)
python scrape.py       # 4. all GET-based ATS endpoints
python boards.py       # 5. LinkedIn / Indeed / Google / Glassdoor via JobSpy
python efc.py --check  # 6. eFinancialCareers — check robots first
python feeds.py --all  # 7. Reed API + Bullhorn recruiter portals
python verify.py       # 8. check every job is real and live
python score.py        # 9. rank and write report.html

./weekly.sh            # or just this — runs all of the above in order

python3 validate.py    # config gate — run before anything expensive
python3 selftest.py    # 2s, no network — run after editing scoring.yaml
python3 insights.py    # what the accumulated data says about the market
python3 match.py       # rank live roles against your actual CV
python links.py unknown.csv   # link index for whatever nothing reaches
```

All of them write to the same `jobs.db` and share dedupe.

## Files

| file | what it is |
|---|---|
| `firms.csv` | the registry — name, category, domain. Edit this to add firms. |
| `config.yaml` | include/exclude title regexes. Tune this first. |
| `discover.py` | finds each firm's real ATS endpoint → `endpoints.csv` + `no_ats.csv` |
| `scrape.py` | pulls every GET-based board (`sniffed.csv` + `endpoints.csv`) and Adzuna → `jobs.db`, `latest.csv` |
| `links.py` | builds `links.md` — LinkedIn/Indeed/Glassdoor/X-ray URLs per firm |
| `links.md` | already generated, 2303 firms |

## Why discovery instead of hardcoded URLs

I don't have a reliable way to know which ATS each of 2303 firms uses, and
hardcoding tokens I can't verify would give you a list that silently returns
zero jobs. `discover.py` finds them empirically and tells you which firms have
no public endpoint at all — those go in `no_ats.csv` and you cover them via
`links.md` and Adzuna instead.

Expect roughly: startups/vendors/crypto/prop → Greenhouse, Lever, Ashby (high
hit rate). Majors and banks → Workday (needs a per-tenant POST body — that's what
`workday.py` is for, fed by `sniff.py`). Old-line trading houses → often bespoke
or Taleo, which land in `manual.csv`.

## Adzuna

Official API, free tier, aggregates most UK boards including many Indeed
listings. Get keys at developer.adzuna.com, then:

```bash
export ADZUNA_APP_ID=... ADZUNA_APP_KEY=...
```

## On LinkedIn / Indeed / Glassdoor

All three prohibit scraping in their ToS and LinkedIn actively blocks it. The
ATS endpoints above are public by design and give cleaner data. `links.md` gives
you pre-built search URLs for those three so you check them manually. If you
want programmatic board coverage, use Adzuna's API — that's the sanctioned route.

## Extending

- Add firms: append to `firms.csv`, rerun `discover.py`.
- Workday tenants: open a Workday careers page, watch the network tab for the
  `/wday/cxs/{tenant}/{site}/jobs` POST, copy the body, add a handler.
- Companies House: bulk data filtered to SIC 46719 / 64999 / 66120 / 35140 +
  London postcodes will surface the 5–30 person shops that aren't on any board.

## Board coverage — boards.py

```bash
pip install python-jobspy
python boards.py                # LinkedIn + Indeed + Google + Glassdoor, last 7 days
python boards.py --no-linkedin  # if you get rate limited
```

Wraps JobSpy (4k stars, actively maintained). Writes into the same `jobs.db`, so
`scrape.py` and `boards.py` share dedupe — a job found on both a firm's Greenhouse
board and LinkedIn appears once.

Defaults are deliberately slow (6s between queries, no description fetch). LinkedIn
429s aggressively; if you get blocked, use `--no-linkedin` for a week or add proxies.

## Roadmap — what's still missing

Ranked by payoff:

1. **Workday handler** — unlocks the majors and banks (BP, Shell, Vitol, Trafigura,
   Goldman, Macquarie). POST to `/wday/cxs/{tenant}/{site}/jobs` with
   `{"appliedFacets":{},"limit":20,"offset":0,"searchText":"analyst"}`. One handler,
   ~40 firms.
2. **Scoring, not just filtering** — rank by title match strength, firm tier, and
   whether the description mentions Python/SQL/regulatory. Turns 200 matches into a
   ranked top 20.
3. **Alerting** — email or Telegram on new matches. Cron + a 10-line sender.
4. **Description fetch + keyword extraction** — pull JDs for matches only, extract
   required years of experience, and auto-drop anything asking for 5+ years.
5. **Fuzzy dedupe** — current key is exact company+title+location. "Analyst, Crude"
   vs "Crude Analyst" slip through. Use rapidfuzz token_set_ratio > 90.
6. **Companies House ingest** — SIC 46719 / 64999 / 66120 / 35140 + London postcodes,
   filtered to firms incorporated 2015+ with 5-50 employees. This is the real long
   tail and nobody else applying to these jobs is doing it.
7. **Application tracker** — status column in `jobs.db`, applied/rejected/interview,
   so the scraper doubles as your pipeline.
8. **Historic view** — you already store `first_seen`, so after a month you can see
   which firms hire continuously vs which posted once. Useful signal on where to focus.


## sniff.py — the fix for Workday and bespoke careers pages

`discover.py` guesses tokens. `sniff.py` reads the company's actual careers page
and pulls the ATS link out of the HTML. Higher hit rate, and it's the only way to
get Workday, whose boards are keyed by tenant + datacenter + site and cannot be
guessed from a company name.

It tries 13 common careers paths per domain and fingerprints 13 scrapable ATS
plus 12 closed ones (iCIMS, Taleo, SuccessFactors, Avature, Eploy, Oleeo,
Tribepad, Pinpoint, Applied, Jobvite, BrassRing, Phenom). Closed ones land in
`manual.csv` — cover those through `boards.py` and `links.md`.

Outputs: `sniffed.csv`, `manual.csv`, `unknown.csv`.

## workday.py

Hand-rolled. There is no maintained Workday library — the best repo on GitHub
(christopherlam888/workday-scraper) has 17 stars. Uses the documented cxs
endpoint:

```
POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
{"appliedFacets":{}, "limit":20, "offset":0, "searchText":"analyst"}
```

Paginates on `total`, 0.4s between pages. `--search analyst` filters server-side
if a tenant has thousands of postings.

URL parser unit-tested against BP, Shell, Macquarie and Vitol board shapes,
including the optional `/en-US/` locale segment that breaks naive regexes.

## efc.py — eFinancialCareers

No usable open-source scraper exists. Every eFC repo on GitHub has zero stars and
most are dead. eFC's own APIs (`core.ws.efinancialcareers.com`,
`candidate-search-api`) are recruiter-side and token-gated — for posting jobs and
searching CVs, not pulling listings.

So this reads the published XML sitemaps and parses the schema.org `JobPosting`
JSON-LD embedded in each job page for Google Jobs — data published specifically
to be machine-read. It checks `robots.txt` first and stops if disallowed.

```bash
python efc.py --check      # print robots verdict, fetch nothing
python efc.py --limit 200
```

2 second delay between fetches, hardcoded. If robots says no, use `boards.py` —
JobSpy's Google source surfaces a lot of eFC listings through Google Jobs, which
is the sanctioned path to the same data. The paid alternative is an Apify actor
(~$1 per 1,000 listings) if you decide the volume justifies it.

## Repos evaluated

| repo | stars | verdict |
|---|---|---|
| speedyapply/JobSpy | 4,050 | used — `boards.py` wraps it |
| PaulMcInnis/JobFunnel | 2,177 | older architecture, less coverage, skipped |
| spinlud/py-linkedin-jobs-scraper | 490 | fallback if JobSpy's LinkedIn gets blocked; headless Chrome, adaptive rate limiting |
| austinoboyle/scrape-linkedin-selenium | 537 | dead since 2022, scrapes profiles not jobs |
| christopherlam888/workday-scraper | 17 | approach borrowed, code rewritten |
| noble-ronin/ats-job-apis | 1 | endpoint cheatsheet — source for adding Breezy + BambooHR |
| all eFinancialCareers repos | 0 | nothing usable |


## feeds.py — Reed + Bullhorn

**Reed** — official jobseeker API, free key at reed.co.uk/developers. Basic auth,
key as username, empty password. Biggest UK board by volume and a lot of
commodity roles get posted there by recruiters and nowhere else.

```
GET https://www.reed.co.uk/api/1.0/search?keywords=...&locationName=London&resultsToTake=100
```

**Bullhorn** — the specialist commodity recruiters (HC Group, Proco, Mondrian,
Selby Jennings, Commodity Appointments) run their portals on Bullhorn, which has
a public read-only REST API for published jobs. `sniff.py` now pulls the cluster
number and corp token straight out of the portal HTML, so no keys are needed.

```
GET https://public-rest{cls}.bullhornstaffing.com/rest-services/{token}/search/JobOrder
    ?query=isOpen:true&fields=id,title,address,dateLastPublished&count=100
```

This is the highest-value addition. Roles at the 5-30 person shops usually never
appear anywhere except a recruiter's own portal.

`sniff.py` also now flags JobAdder, Vincere, idibu and Loxo portals into
`manual.csv` — those need a look at the network tab to find their JSON call.

## Platforms considered and skipped

| platform | why not |
|---|---|
| Careerjet / Jooble / WhatJobs | APIs exist but need partner approval; heavy overlap with Adzuna |
| Indeed Publisher API | deprecated for new applicants |
| Otta / Welcome to the Jungle | strong for startups, no public API, login-gated |
| Wellfound | US-heavy, thin on commodities |
| X / Twitter | API now paid, and desk-head hiring posts are better caught manually |
| Glassdoor standalone | JobSpy already covers it |
| Apify actors | works for everything, but ~$1 per 1,000 listings for data you can get free |

## Worth adding later

- **Companies House API** — free, official. SIC 46719 / 64999 / 66120 / 35140 filtered
  to London gives you firms that appear on no board at all. Feed the output back
  into `firms.csv` and rerun `sniff.py`. This is the real long tail.
- **FCA Register API** — free, official. Cross-reference permissions to find
  authorised commodity dealers you've never heard of.
- **Modo Energy, Climatebase, Terra.do** — small niche boards, no APIs, but
  low-volume enough to check by hand via `links.md`.


## verify.py — the part that stops you wasting applications

Aggregators are full of postings that are expired, filled, reposted for the
fifteenth time, or agency ads for a client that doesn't exist. Ranking an
unverified pile just sorts the noise, so nothing scores until it's been checked.

Per job it fetches the URL and checks:

| check | catches |
|---|---|
| HTTP status, redirect chain | dead links, deleted postings |
| 15 closed/filled/expired markers | "no longer accepting applications" |
| title overlap between stored title and page title | silent redirects to a generic listings page |
| schema.org JSON-LD `validThrough` | postings past their own expiry date |
| `hiringOrganization` vs stored company | aggregator got the employer wrong |
| years-of-experience regex on the description | roles wanting 7 years dressed up as "analyst" |
| anonymous-employer patterns | "a leading commodity trading house" |
| agency language ("our client", "employment business") | recruiter ads vs the firm hiring direct |
| repost history across `first_seen` | ghost jobs — same role posted 3+ times over 60+ days |

Results go to a `verify` table. Nothing is deleted — failures are recorded with a
reason, so you can see what got filtered and why.

Range parsing is deliberate: "3-5 years" reads as a floor of 3, not 5, and across
several mentions it takes the lowest — that's the actual bar, not the wish list.

```bash
python verify.py             # anything unverified
python verify.py --recheck   # re-verify everything (weekly is sensible)
```

## score.py — ranking

Reads `scoring.yaml`. Every weight is tunable without touching code. Eight
components: title tier, seniority, firm category, description signals,
provenance, freshness, verification result, ghost penalty.

Two things worth knowing about the rubric as shipped:

- **Applying direct beats an agency.** A Greenhouse or Workday posting scores +8;
  the same role via Reed or Indeed scores 0. If a job appears both ways, dedupe
  keeps whichever landed first — run `scrape.py` before `boards.py` so the direct
  link wins.
- **Your regulatory background is scored explicitly.** Any JD mentioning REMIT,
  EMIR, market abuse, surveillance or the regulator gets +10. That's the one
  signal where you're competing against almost nobody.

Every score is explained. Each job carries the rules that fired and what each was
worth, so when something ranks high you can see why and adjust the yaml rather
than guess.

Outputs `report.html` (read this on your phone), `report.md`, `scored.csv`.

Tested end to end on synthetic data: ghost detection fires on a role reposted 4
times over 5 months, a 7-year "Quantitative Analyst" drops 33 points, an
anonymous 95-day-old agency ad lands at 37 while a fresh direct Ashby posting
matching your background lands at 109.


## Running it weekly

`weekly.sh` is the whole pipeline in order. Stages that fail don't stop the run —
a broken Workday tenant shouldn't cost you the LinkedIn results. Logs land in
`logs/YYYY-MM-DD.log`, kept 90 days.

```bash
chmod +x weekly.sh
./weekly.sh              # normal weekly run
./weekly.sh --full       # also re-sniff every firm's ATS (monthly is plenty)
./weekly.sh --resend-all # re-send the digest covering every open role, from the
                         # database as it stands — no collection, no verification,
                         # and the run is not recorded, so it can't hide next
                         # week's genuinely new jobs. Seconds, not half an hour.
```

Cron, Sunday 07:00:

```
0 7 * * 0 cd /path/to/jobs && ./weekly.sh
```

Or run it in the cloud with the included `.github/workflows/weekly.yml` — put the
API keys in repo secrets, and it commits `jobs.db` and `report.html` back each
week so state persists. One caveat: LinkedIn blocks datacentre IPs and GitHub's
runners are the most blocked of all, so the workflow forces `--no-linkedin`. Run
`boards.py` at home occasionally if you want LinkedIn coverage.

### Only new ones

`score.py --new-only` reads the `runs` table and shows only jobs first seen since
the last recorded run. `--record` stamps the run. `weekly.sh` uses both, so each
digest is strictly the delta.

Ghost detection still looks at the full history, not just the week — a repost
pattern is only visible over months.

### Closing the loop

The delta only stays small if the pile shrinks:

```bash
python track.py list                      # what's open
python track.py applied kpler market      # fuzzy match, confirms before bulk edits
python track.py ignored goldman
python track.py stats                     # pipeline summary + recent runs
```

Anything not `new` is excluded from future digests.

### Delivery and the silent-failure problem

`notify.py` sends the digest by email (any SMTP) or Telegram. Telegram is less
hassle on a phone.

It also runs a health check, which matters more than it sounds. The real failure
mode here isn't a crash — it's a source quietly returning zero for a month after
an endpoint changed, while the digest still looks normal and you assume the
market went quiet. So each run compares per-source counts against the trailing
four-week average and flags anything that fell off a cliff.

## What's left

Ranked by what actually moves the needle now:

1. **Companies House ingest.** Free official API. SIC 46719 / 64999 / 66120 /
   35140, London, incorporated 2015+, 5-50 employees. Feed the output into
   `firms.csv` and rerun `sniff.py`. This is the real long tail — firms with no
   board presence at all, where nobody else applying is looking.
2. **JD-to-CV matching.** You have descriptions stored from `verify.py`. Scoring
   currently matches keywords; comparing the JD against your actual CV would rank
   on genuine fit and could draft the tailored opening line per application.
3. **Fuzzy dedupe.** The key is exact company+title+location. "Analyst, Crude" and
   "Crude Analyst" still slip through as two rows. `rapidfuzz` token_set_ratio > 90
   fixes it in about ten lines.
4. **Workday tenants that sniff misses.** Some firms link their board only from a
   sub-page. A one-off Google `site:myworkdayjobs.com "<firm>"` for the ~40 majors
   and banks, pasted into a `workday_manual.csv`, closes most of the gap.
5. **Salary capture.** Ashby and Adzuna both return compensation. Worth storing
   even where it's sparse — after three months you'd have a real picture of what
   these roles pay in London, which is genuinely hard to find.
6. **Hiring-velocity view.** You already store `first_seen`. After two months the
   data tells you which firms hire continuously versus which posted once and
   vanished. That's a better targeting signal than any list I gave you.


## store.py — one write path

Every collector now goes through `store.save_new()`. Three things that were
slightly wrong in each collector are now right in one place:

- **Canonical dedupe.** "Analyst, Crude" and "Crude Analyst" are one job. The key
  sorts significant title tokens, so word order, punctuation and `Ltd` / `Group`
  suffixes stop mattering. In testing, 40 submitted rows collapsed to 35.
- **Source preference.** If a role arrives from both Indeed and the firm's own
  Greenhouse board, the row keeps the Greenhouse URL — you apply direct, even
  when the aggregator found it first.
- **Salary.** Adzuna, Reed and Ashby all disclose it. Sparse per-job, but after
  three months it's a real picture of London pay for these roles, which is
  otherwise very hard to find.

## selftest.py

71 assertions, under a second, no network. Run it after touching `scoring.yaml` or
any parser — it catches the class of bug where everything still runs but silently
ranks the wrong things.

Covers dedupe and provenance upgrade, the years-of-experience parser (including
"3-5 years" reading as a floor of 3), title-similarity rejecting a redirect to a
listings page, closed-marker and anonymous-employer detection, JSON-LD
extraction, and eight rubric behaviours: an 8-year role must lose to a 2-year one,
dead must drop out of contention, direct must beat aggregator, a JD mentioning
REMIT or surveillance must beat one that doesn't.

## insights.py

The database stops being a job list after a couple of months and becomes a
dataset about the market. Four views:

- **Hiring velocity** — who posts continuously versus who posted once and went
  quiet. Continuous posters have real churn and will have another opening within
  weeks; one-off posters are worth a speculative approach instead.
- **Salary** — median, quartiles, and analyst-title-only median from whatever
  disclosed it.
- **Title language** — which words these roles are actually named with, which is
  how your CV and search terms should be phrased.
- **Funnel** — applied versus seen, and how many roles more than one source found.

## match.py

`score.py` ranks on the rubric. `match.py` ranks on you. Save your CV as plain
text in `cv.txt`, and it compares it against every stored job description:
relative fit, the three CV lines worth leading with for that role, and what the
JD emphasises that your CV never mentions.

Local TF-IDF, no API needed. Scores are relative to the best match in the batch,
not percentages — it's a ranking. With `ANTHROPIC_API_KEY` set, `--draft` also
writes a grounded opening line per role.

`cv.txt` is gitignored. Don't commit it.

## companies_house.py

The long tail. Free official API, SIC 46719 / 46711 / 64999 / 66120 / 35140 /
35230, London postcodes, active companies only, junk-name filter.

```bash
export CH_API_KEY=...
python3 companies_house.py --preview
python3 companies_house.py --append     # adds to firms.csv
```

Domains come out blank — Companies House doesn't hold websites, and a wrong guess
is worse than none because `sniff.py` would chase it. Fill in the ones that look
interesting by hand, then rerun `sniff.py`.

## Cadence

| when | command |
|---|---|
| weekly | `./weekly.sh` (or cron / the Actions workflow) |
| after any config edit | `python3 selftest.py` |
| monthly | `./weekly.sh --full` — re-sniff every firm's ATS |
| monthly | `python3 insights.py` |
| when you update your CV | `python3 match.py` |
| once, early | `python3 companies_house.py --append` |


## Robustness

Everything below exists because an unattended weekly job fails in specific,
boring ways, and the worst failures are the silent ones — the digest still
arrives, it just quietly stopped containing anything.

### Network — `http_client.py`

Nothing calls `requests` directly any more. One layer handles:

| failure | handling |
|---|---|
| transient 5xx, connection reset | 3 retries, exponential backoff with jitter |
| 429 | honours `Retry-After`, backs off instead of hammering |
| a host being down | circuit breaker after 6 consecutive failures, 15-minute cooldown |
| volume-based rate limiting | 0.35s minimum interval per host, thread-safe |
| a hung connection | separate connect (10s) and read (25s) timeouts |
| a 200 that's actually an error page | `json_of()` returns None rather than raising on HTML |
| mojibake in titles | encoding detection, entity unescape, NFKC normalisation |

Every call returns `None` rather than raising, so a dead endpoint costs one firm,
not the run. Circuit-broken hosts are reported at the end of `weekly.sh`.

### Database

WAL journal mode, 60s busy timeout, `synchronous=NORMAL`. A backup is taken
before every run (`backups/`, 5 kept, 60-day expiry) using SQLite's own backup
API so it's consistent even mid-write.

`verify.py` checkpoints every 20 jobs and traps Ctrl-C — a run killed at job 400
keeps the first 399 and resumes from there, because it only re-checks jobs
missing from the `verify` table.

Every insert is individually wrapped. One malformed row can't abort a batch that
already took twenty minutes to gather.

### Input validation

Rows are cleaned then validated before storage: empty or wordless titles,
absurdly long titles, titles containing markup, missing companies, and non-HTTP
URLs are all dropped with a counted reason. Report output is HTML-escaped on top
of that, so a hostile job title can't inject into `report.html`.

There's also an anomaly guard: more than 800 new rows in one run prints a loud
warning, because that means a filter regex went permissive rather than that the
market got busy.

### Config — `validate.py`

Runs before anything expensive. Catches:

- regexes that don't compile (used to crash 20 minutes into a run)
- patterns like `.*` that would match everything
- weights typed as strings — the nastiest one, because scores silently collapse
  with no error at all
- `freshness` thresholds out of order, missing `unknown` firm category
- a `shortlist_threshold` below `min_score`, which would shortlist everything
- and a live check that the filter still keeps "Commodity Analyst" and still
  drops "Head of Trading"

Tested by deliberately breaking both config files: both errors caught, exit 1.

### Orchestration — `weekly.sh`

- `flock` — a second run started by cron while the first is going exits cleanly
- gates on `validate.py` and `selftest.py`, aborting before any scraping
- per-stage `timeout` (40 min default) so one hung host can't stall the week
- per-stage isolation with a failure list, and a non-zero exit if any failed
- if 5 or more stages fail it scores **without** `--record`, so a bad week isn't
  silently swallowed from next week's digest
- traps SIGINT/SIGTERM and reports that partial results are saved
- log and backup rotation

### Empty and partial states

Every entry point was run against a fresh install with no database:

```
validate.py      -> config ok
selftest.py      -> all checks passed
score.py         -> jobs.db not found — run the collectors first
notify.py        -> no database yet — nothing to notify about
insights.py      -> no database yet — run the collectors first
match.py         -> No cv.txt found. Save your CV as plain text there and rerun.
track.py list    -> no database yet — run the collectors first
```

No stack traces, and no read-only command creates a stray database.

### Test coverage

`selftest.py` is now 71 assertions across dedupe and provenance, the ATS
fingerprint regexes (including the Workday locale segment and the Bullhorn
cluster/token pair), the verification parsers, eight rubric behaviours, ghost
detection, report escaping, the HTTP layer (breaker, sanitisation, None-safety,
HTML-page-as-JSON), store validation, WAL settings, collector wiring, and filter
sanity. Under a second, no network. `weekly.sh` refuses to run if it fails.
