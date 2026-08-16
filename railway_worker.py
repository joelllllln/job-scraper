#!/usr/bin/env python3
"""
railway_worker.py — the Railway entrypoint.

Railway exists in this project to do the one thing GitHub Actions cannot:
reach LinkedIn. Everything else stays where it is. This worker therefore has
two modes and no ambitions beyond them.

    RUN_MODE=probe      (default) one-shot. Does LinkedIn answer from this
                        host? Prints a verdict and exits. Costs seconds.

    RUN_MODE=collect    scrape LinkedIn for the configured queries and push
                        what it finds to the GitHub repo as linkedin_inbox.csv,
                        where the weekly run picks it up and dedupes it into
                        the one real database.

Why an inbox file rather than Railway keeping its own database: two databases
means the same role reaches you twice, once from each, with no shared memory
of what you have already applied to. A single append-only CSV that only this
worker ever writes keeps one source of truth and cannot conflict on push.

Environment:
    RUN_MODE            probe | collect          (default: probe)
    GITHUB_TOKEN        needed only for collect — a PAT with contents:write
    GITHUB_REPO         owner/name               (default: joelllllln/job-scraper)
    GITHUB_BRANCH       branch to push to
    JOBSPY_PROXIES      residential proxies, comma separated (see boards.py)
    HOURS               how far back to search      (default: 336, i.e. 14 days)
"""

import csv
import os
import env
import subprocess
import sys
import tempfile

REPO = os.getenv("GITHUB_REPO", "joelllllln/job-scraper")
BRANCH = os.getenv("GITHUB_BRANCH", "claude/new-session-eh2y1s")
INBOX = "linkedin_inbox.csv"
FIELDS = ["company", "title", "location", "url", "source", "posted"]


def sh(cmd, cwd=None, check=True, quiet=False):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if not quiet and r.stdout.strip():
        print(r.stdout.strip())
    if r.returncode and check:
        print(r.stderr.strip()[:2000], file=sys.stderr)
        raise SystemExit(f"command failed: {' '.join(cmd)}")
    return r


def collect():
    """Scrape LinkedIn, then push the finds into the repo as an inbox CSV."""
    import yaml
    from boards import QUERIES, LOCATION, proxies
    from scrape import build_filter
    from jobspy import scrape_jobs

    keep = build_filter(yaml.safe_load(open("config.yaml", encoding="utf-8")))
    hours = int(os.getenv("HOURS") or 336)

    rows, seen = [], set()
    for i, q in enumerate(QUERIES, 1):
        print(f"[{i}/{len(QUERIES)}] {q}", flush=True)
        try:
            df = scrape_jobs(site_name=["linkedin"], search_term=q,
                             location=LOCATION, country_indeed="UK",
                             results_wanted=40, hours_old=hours,
                             linkedin_fetch_description=False,
                             proxies=proxies(), verbose=0)
        except Exception as e:
            print(f"      ! {type(e).__name__}: {str(e)[:160]}", file=sys.stderr)
            continue
        if df is None or not len(df):
            continue
        for _, r in df.iterrows():
            j = {"company": str(r.get("company") or ""),
                 "title": str(r.get("title") or ""),
                 "location": str(r.get("location") or ""),
                 "url": str(r.get("job_url") or ""),
                 "source": "linkedin",
                 "posted": str(r.get("date_posted") or "")}
            if j["url"] in seen or not keep(j):
                continue
            seen.add(j["url"])
            rows.append(j)
        print(f"      {len(df)} rows, {len(rows)} kept so far")

    print(f"\n{len(rows)} LinkedIn roles matched the filter")
    if not rows:
        # Nothing to push is a legitimate outcome, but on LinkedIn it is far
        # more often a block than a quiet week — say so rather than exit 0
        # looking like a success.
        print("nothing matched. If this repeats, run linkedin_probe.py: a "
              "block and an empty week are indistinguishable from here.")
        return 0

    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("\nGITHUB_TOKEN not set — printing instead of pushing:\n")
        for j in rows:
            print(f"  {j['company'][:28]:<30} {j['title'][:50]:<52} {j['url']}")
        return 0

    work = tempfile.mkdtemp()
    url = f"https://x-access-token:{token}@github.com/{REPO}.git"
    sh(["git", "clone", "--depth", "1", "--branch", BRANCH, url, work], quiet=True)
    path = os.path.join(work, INBOX)

    # Append to whatever is already there and dedupe on url, so a role stays in
    # the inbox until the weekly run has had a chance to ingest it, and two
    # runs before that happens do not lose the first run's finds.
    existing, have = [], set()
    if os.path.exists(path):
        with open(path, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                existing.append(row)
                have.add(row.get("url", ""))
    fresh = [j for j in rows if j["url"] not in have]
    if not fresh:
        print("all of these are already in the inbox — nothing to push")
        return 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=FIELDS)
        w.writeheader()
        for row in existing + fresh:
            w.writerow({k: row.get(k, "") for k in FIELDS})

    sh(["git", "-C", work, "config", "user.name", "railway-linkedin"])
    sh(["git", "-C", work, "config", "user.email", "actions@users.noreply.github.com"])
    sh(["git", "-C", work, "add", INBOX])
    r = sh(["git", "-C", work, "commit", "-m",
            f"linkedin: {len(fresh)} new roles"], check=False)
    if r.returncode:
        print("nothing to commit")
        return 0
    for attempt in (1, 2, 3):
        if sh(["git", "-C", work, "push", "origin", f"HEAD:{BRANCH}"], check=False).returncode == 0:
            print(f"pushed {len(fresh)} new LinkedIn roles to {REPO}:{BRANCH}")
            return 0
        print(f"push rejected (attempt {attempt}) — rebasing onto the remote")
        sh(["git", "-C", work, "fetch", "origin", BRANCH], check=False)
        if sh(["git", "-C", work, "rebase", f"origin/{BRANCH}"], check=False).returncode:
            sh(["git", "-C", work, "rebase", "--abort"], check=False)
    print("could not push after 3 attempts", file=sys.stderr)
    return 1


def main():
    env.load()
    mode = (os.getenv("RUN_MODE") or "probe").lower()
    print(f"railway_worker: mode={mode}\n", flush=True)
    if mode == "collect":
        return collect()
    import linkedin_probe
    sys.argv = ["linkedin_probe", "--full"]
    return linkedin_probe.main()


if __name__ == "__main__":
    sys.exit(main())
