#!/usr/bin/env python3
"""
applylist.py — the digest as a working list you can tick through.

score.py ranks everything and notify.py mails it. What neither produces is the
thing you actually sit down with: every role, in order, with the apply link,
already stripped of the ones you have said you do not want.

    python applylist.py                    # writes apply_list.html and opens it
    python applylist.py --no-open
    python applylist.py --max-age 30       # tighter staleness cut
    python applylist.py --keep-brokers
    python applylist.py --hide-recruiters  # drop them rather than flag them

Filters applied, all of them switchable above:

  brokers        broking is a different career from analysis, and a Trainee
                 Broker Programme kept topping the list
  stale          45+ days open with no closing date is an evergreen advert
  recruiters     kept but flagged, because a recruiter-posted role is still a
                 real role — it is just one where you cannot see the employer

Reads scored.csv, which score.py writes on every run.
"""

import argparse
import csv
import html
import os
import re
import sys
import webbrowser
from datetime import datetime, timezone

IN = "scored.csv"
OUT = "apply_list.html"

# Broking as a job, not brokers as employers: "Marex Data Analyst" stays,
# "Trainee Broker Programme" goes.
BROKER = re.compile(r"\bbroker\b|\bbroking\b|shipbrok", re.I)

# Agencies and search firms. A role here is real, but the employer is hidden and
# you are one of several candidates being put forward.
RECRUITER = re.compile(
    r"selby jennings|oxford knight|imperium|marlin selection|anson mccade|"
    r"dartmouth partners|kennedypearce|richard james|aej consulting|purefuel|"
    r"gradbay|durlston|insight global|greenwich partners|saragossa|aurum search|"
    r"stanford black|camber morris|mondrian alpha|cameron kennedy|orla rose|"
    r"redstone|nicholson glover|octavius|the green recruitment|ventula|"
    r"prime personnel|cititec|mccabe|edgworth|fram search|albert bow|g-20 group|"
    r"ocr alpha|qenexus|onyx alpha|intropic|lodestone|per,|private equity recruit|"
    r"bruin|aaa global|mason blake|emagine", re.I)

BANDS = [(110, "Prime", "110+"), (90, "Strong", "90–109"), (70, "Worth a look", "70–89"),
         (45, "Marginal", "45–69"), (-10_000, "Long tail", "under 45")]


def stale_days(why):
    """Days open, taken from score.py's own 'open 117d, no close date' note."""
    m = re.search(r"open (\d+)d, no close date", why or "")
    return int(m.group(1)) if m else None


def age_days(why):
    m = re.search(r"\b(\d+)d old", why or "")
    return int(m.group(1)) if m else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="src", default=IN)
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--max-age", type=int, default=45,
                    help="drop postings open longer than this with no close date")
    ap.add_argument("--keep-brokers", action="store_true")
    ap.add_argument("--hide-recruiters", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args()

    if not os.path.exists(args.src):
        print(f"{args.src} not found — run `python score.py --everything` first",
              file=sys.stderr)
        return 1
    with open(args.src, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        print(f"{args.src} is empty")
        return 1

    kept, cut = [], {"broker": 0, "stale": 0, "recruiter": 0, "nolink": 0}
    seen_urls = set()
    for r in rows:
        url = (r.get("url") or "").strip()
        title, co, why = r.get("title", ""), r.get("company", ""), r.get("why", "")
        if not url:
            cut["nolink"] += 1
            continue
        key = url.rstrip("/").lower()
        if key in seen_urls:            # same posting under two firm names
            continue
        seen_urls.add(key)
        if not args.keep_brokers and BROKER.search(title):
            cut["broker"] += 1
            continue
        open_for = stale_days(why)
        if open_for is not None and open_for > args.max_age:
            cut["stale"] += 1
            continue
        is_rec = bool(RECRUITER.search(co))
        if is_rec and args.hide_recruiters:
            cut["recruiter"] += 1
            continue
        try:
            score = int(float(r.get("score") or 0))
        except ValueError:
            score = 0
        kept.append({"score": score, "co": co, "title": title, "url": url,
                     "loc": (r.get("location") or "").strip(),
                     "years": (r.get("years") or "").strip(),
                     "rec": is_rec,
                     "unver": "site blocked the check" in why,
                     "age": age_days(why)})

    kept.sort(key=lambda x: -x["score"])

    parts, band_i = [], -1
    for i, j in enumerate(kept, 1):
        b = next(k for k, (floor, _, _) in enumerate(BANDS) if j["score"] >= floor)
        if b != band_i:
            if band_i >= 0:
                parts.append("</tbody></table></div></section>")
            parts.append(f'<section><h2>{BANDS[b][1]}<span class="rng">{BANDS[b][2]}'
                         f'</span></h2><div class="scroll"><table><tbody>')
            band_i = b
        chips = ""
        if j["rec"]:
            chips += '<span class="c r" title="Posted by a recruiter, not the employer">rec</span>'
        if j["unver"]:
            chips += '<span class="c u" title="The site refused an automated check — not proof it is dead">unverified</span>'
        if j["years"]:
            chips += f'<span class="c y">{html.escape(j["years"])}y</span>'
        if j["age"] is not None and j["age"] >= 30:
            chips += f'<span class="c a">{j["age"]}d old</span>'
        parts.append(
            f'<tr><td class="n">{i}</td><td class="s">{j["score"]}</td>'
            f'<td class="co">{html.escape(j["co"])}</td>'
            f'<td class="ti"><a href="{html.escape(j["url"])}" target="_blank" '
            f'rel="noopener">{html.escape(j["title"])}</a>'
            f'<span class="lo">{html.escape(j["loc"]) or "location not stated"}</span></td>'
            f'<td class="fl">{chips}</td></tr>')
    parts.append("</tbody></table></div></section>")

    stamp = datetime.now(timezone.utc).strftime("%d %B %Y")
    cuts = " · ".join(f"{v} {k}" for k, v in cut.items() if v)
    page = TEMPLATE.format(css=CSS, n=len(kept), stamp=stamp,
                           cuts=html.escape(cuts or "nothing"),
                           body="".join(parts))
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"{len(kept)} roles -> {args.out}   (removed: {cuts or 'nothing'})")
    if not args.no_open:
        webbrowser.open("file://" + os.path.abspath(args.out))
    return 0


CSS = """
:root{--paper:#FCFCFA;--panel:#fff;--ink:#15181B;--body:#2C3238;--muted:#5B6470;
--rule:#E3E5E1;--soft:#F1F2EF;--yes:#1B6B4C;--warn:#8A6A16;--warn-bg:#F8F1DE;
--rec:#5B6470;--rec-bg:#EEEFEC}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){
--paper:#111417;--panel:#171B1F;--ink:#EDEFEC;--body:#C6CCD1;--muted:#8E979F;
--rule:#2A3036;--soft:#1C2228;--yes:#6FC79C;--warn:#D9B357;--warn-bg:#2A2314;
--rec:#8E979F;--rec-bg:#222831}}
:root[data-theme="dark"]{--paper:#111417;--panel:#171B1F;--ink:#EDEFEC;
--body:#C6CCD1;--muted:#8E979F;--rule:#2A3036;--soft:#1C2228;--yes:#6FC79C;
--warn:#D9B357;--warn-bg:#2A2314;--rec:#8E979F;--rec-bg:#222831}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--body);
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
font-size:15px;line-height:1.45;-webkit-font-smoothing:antialiased}
.wrap{max-width:920px;margin:0 auto;padding:44px 20px 80px;display:flex;
flex-direction:column;gap:34px}
header{border-bottom:2px solid var(--ink);padding-bottom:16px;display:flex;
flex-direction:column;gap:9px}
.eyebrow{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;
letter-spacing:.14em;text-transform:uppercase;color:var(--muted)}
h1{font-family:ui-serif,"Iowan Old Style",Palatino,Georgia,serif;font-weight:600;
font-size:clamp(30px,5.5vw,42px);line-height:1.05;letter-spacing:-.015em;
color:var(--ink);margin:0}
.key{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted)}
.key b{color:var(--ink)}
h2{font-family:ui-serif,"Iowan Old Style",Palatino,Georgia,serif;font-weight:600;
font-size:22px;color:var(--ink);margin:0 0 8px;display:flex;align-items:baseline;
gap:10px}
.rng{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;
letter-spacing:.08em;color:var(--muted);text-transform:uppercase}
section{display:flex;flex-direction:column}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse}
td{padding:9px 10px 9px 0;border-bottom:1px solid var(--rule);vertical-align:top}
tr:hover td{background:var(--soft)}
.n{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;
color:var(--muted);width:40px;font-variant-numeric:tabular-nums;text-align:right;
padding-right:12px}
.s{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:14px;font-weight:600;
color:var(--yes);width:46px;font-variant-numeric:tabular-nums}
.co{font-weight:600;color:var(--ink);width:30%;font-size:14px}
.ti{font-size:14px}
.ti a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule)}
.ti a:hover{border-bottom-color:var(--yes);color:var(--yes)}
.ti a:focus-visible{outline:2px solid var(--yes);outline-offset:2px}
.lo{display:block;font-size:12px;color:var(--muted);margin-top:1px}
.fl{white-space:nowrap;text-align:right}
.c{display:inline-block;font-family:ui-monospace,Menlo,Consolas,monospace;
font-size:10px;letter-spacing:.05em;padding:2px 6px;border-radius:3px;
margin-left:4px;text-transform:uppercase}
.c.r{background:var(--rec-bg);color:var(--rec)}
.c.u,.c.y,.c.a{background:var(--warn-bg);color:var(--warn)}
footer{border-top:1px solid var(--rule);padding-top:16px;font-size:13px;
color:var(--muted)}
@media (max-width:620px){.co{width:auto;display:block}.fl{text-align:left}}
"""

TEMPLATE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Apply List</title><style>{css}</style></head><body><div class="wrap">
<header>
<div class="eyebrow">Job run · {stamp} · every role, with its link</div>
<h1>Apply List</h1>
<div class="key">
<span><b>{n}</b> roles</span>
<span><span class="c r">rec</span> recruiter, not the employer</span>
<span><span class="c u">unverified</span> site refused the check</span>
<span>removed: {cuts}</span>
</div>
</header>
{body}
<footer>Titles are links — click through to apply. Rebuild any time with
<code>python applylist.py</code>.</footer>
</div></body></html>"""


if __name__ == "__main__":
    sys.exit(main())
