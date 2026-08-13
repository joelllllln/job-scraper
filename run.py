#!/usr/bin/env python3
"""
run.py — the whole thing, on your own machine, one command.

    python run.py

That is it. The first time, it asks for the handful of settings it needs and
saves them to .env (which is gitignored); after that it just runs.

Why this exists rather than weekly.sh: weekly.sh is bash, so it does not run on
Windows, and the GitHub workflow deliberately turns LinkedIn off because
datacentre IPs are blocked. From a home connection LinkedIn works, which is the
single biggest source the hosted run cannot reach — so the local run is not a
fallback, it is the better one.

    python run.py                 # collect, verify, rank, email
    python run.py --render        # also open a browser for the firms whose
                                  # careers pages only exist after JavaScript
    python run.py --full          # re-check every firm's ATS from scratch
    python run.py --report-all    # email every open role, not just new ones
    python run.py --setup         # change the saved settings
    python run.py --no-email      # write the report, send nothing

Every stage is isolated and time-limited: one dead source cannot take the run
with it, and nothing hangs forever. Partial results are always kept.
"""

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

ENV_FILE = ".env"
PY = sys.executable

# Per-stage ceiling in seconds. Generous on a laptop — nothing here is charged
# by the minute, and the slow stages are slow because they are being polite.
TIMEOUTS = {"sniff": 3600, "discover": 3600, "render": 14400, "boards": 2700}
DEFAULT_TIMEOUT = 1800

SETTINGS = [
    ("DB_PASSPHRASE", "Passphrase that unlocks your job history (jobs.db.gpg) — "
                      "the same one set as a GitHub secret", True),
    ("SMTP_USER", "Your Gmail address", True),
    ("SMTP_PASS", "Gmail APP PASSWORD (16 characters, not your normal password)", True),
    ("DIGEST_TO", "Send the digest to (blank = same as your Gmail address)", False),
    ("REED_API_KEY", "Reed API key — free at reed.co.uk/developers (blank to skip)", False),
    ("ADZUNA_APP_ID", "Adzuna app id — free at developer.adzuna.com (blank to skip)", False),
    ("ADZUNA_APP_KEY", "Adzuna app key (blank to skip)", False),
]


def load_env(path=ENV_FILE):
    """Read .env into os.environ without overwriting anything already set."""
    if not os.path.exists(path):
        return False
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return True


def setup():
    """First-run questions. Written to .env, which .gitignore already covers."""
    print("\n  Setting up. This is asked once and saved to .env.\n"
          "  Gmail needs an APP PASSWORD, not your normal one:\n"
          "  myaccount.google.com -> Security -> 2-Step Verification -> App passwords\n")
    existing = {}
    if os.path.exists(ENV_FILE):
        for line in open(ENV_FILE, encoding="utf-8"):
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()

    values = dict(existing)
    for key, prompt, required in SETTINGS:
        current = existing.get(key, "")
        shown = f" [{'*' * 8 if 'PASS' in key else current}]" if current else ""
        while True:
            got = input(f"  {prompt}{shown}: ").strip()
            if not got and current:
                got = current
            if got or not required:
                break
            print("    needed — this one cannot be blank")
        if got:
            values[key] = got
    values.setdefault("SMTP_HOST", "smtp.gmail.com")
    values.setdefault("SMTP_PORT", "587")
    values.setdefault("DIGEST_TO", values.get("SMTP_USER", ""))

    with open(ENV_FILE, "w", encoding="utf-8") as fh:
        fh.write("# Written by run.py --setup. Gitignored: never commit this.\n")
        for k, v in values.items():
            fh.write(f"{k}={v}\n")
    print(f"\n  Saved to {ENV_FILE}. Run `python run.py` any time from now on.\n")


def gpg(args):
    """Run gpg, returning True on success. False if it is not installed."""
    try:
        return subprocess.call(["gpg"] + args, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except (OSError, subprocess.SubprocessError):
        return False


def unlock_db():
    """Restore jobs.db from the encrypted copy in the repo.

    The database is the one thing here that cannot be regenerated: every role
    ever seen, how often, and which ones you marked applied. It is gitignored
    and committed only as jobs.db.gpg, so on a fresh clone the plain file is
    absent — and until now run.py simply started with an empty database and
    said nothing. A silent fresh start looks identical to a working run right
    up until the digest re-offers you a job you applied for a month ago.

    Only ever restores when jobs.db is missing, so a local database in use is
    never overwritten by an older committed copy.
    """
    if os.path.exists("jobs.db") or not os.path.exists("jobs.db.gpg"):
        return False
    key = os.environ.get("DB_PASSPHRASE", "")
    if not key:
        print("!! jobs.db.gpg is here but DB_PASSPHRASE is not set, so your job\n"
              "   history cannot be opened. This run will start from an empty\n"
              "   database. Set DB_PASSPHRASE in .env (`python run.py --setup`).")
        return False
    if gpg(["--batch", "--yes", "--quiet", "--passphrase", key, "-o", "jobs.db",
            "-d", "jobs.db.gpg"]):
        print("history: restored from jobs.db.gpg")
        return True
    print("!! could not decrypt jobs.db.gpg — wrong DB_PASSPHRASE, or gpg is not\n"
          "   installed (Windows: gnupg.org/download, or `winget install GnuPG.GnuPG`).\n"
          "   Starting from an empty database rather than guessing.")
    return False


def lock_db():
    """Re-encrypt, so the history you just added survives the next clone."""
    key = os.environ.get("DB_PASSPHRASE", "")
    if not key or not os.path.exists("jobs.db"):
        return
    if gpg(["--batch", "--yes", "--quiet", "--passphrase", key, "-c",
            "--cipher-algo", "AES256", "-o", "jobs.db.gpg", "jobs.db"]):
        print("history: jobs.db.gpg updated — commit it to keep this run's results")


def stage(name, cmd, failures, timeout=None):
    """Run one step. Never lets a failure stop the rest of the run."""
    print(f"\n=== {name} · {datetime.now().strftime('%H:%M:%S')} ===", flush=True)
    started = time.time()
    try:
        rc = subprocess.call([PY] + cmd, timeout=timeout or TIMEOUTS.get(name, DEFAULT_TIMEOUT))
    except subprocess.TimeoutExpired:
        print(f"!! {name} hit its time limit — continuing with what it collected")
        failures.append(name)
        return
    except KeyboardInterrupt:
        raise
    except Exception as e:
        print(f"!! {name} could not start: {type(e).__name__}: {e}")
        failures.append(name)
        return
    took = int(time.time() - started)
    if rc == 0:
        print(f"--- {name} ok ({took}s)")
    else:
        print(f"!! {name} failed (exit {rc}) after {took}s — continuing")
        failures.append(name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--setup", action="store_true", help="change the saved settings")
    ap.add_argument("--render", action="store_true",
                    help="also use a browser on firms whose careers pages need JavaScript")
    ap.add_argument("--render-limit", type=int, default=250,
                    help="how many firms to render in one go (default 250)")
    ap.add_argument("--full", action="store_true", help="re-check every firm's ATS")
    ap.add_argument("--report-all", action="store_true",
                    help="email every open role, not just this week's new ones")
    ap.add_argument("--no-email", action="store_true", help="write the report, send nothing")
    ap.add_argument("--no-linkedin", action="store_true",
                    help="skip LinkedIn (use if it starts rate limiting you)")
    args = ap.parse_args()

    if args.setup or not os.path.exists(ENV_FILE):
        setup()
    load_env()
    if args.no_email:
        for k in ("SMTP_HOST", "SMTP_USER", "DIGEST_TO"):
            os.environ.pop(k, None)

    print(f"\n### local run {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")
    print(f"### python {sys.version.split()[0]} on {sys.platform}")
    print("### LinkedIn: " + ("skipped" if args.no_linkedin else "ON (the reason to run "
                              "this at home — GitHub's runners are blocked)"))

    # Gates. A broken config or a broken parser should stop the run before it
    # spends an hour collecting, not after.
    for gate, why in (("validate.py", "config is invalid"), ("selftest.py", "selftest failed")):
        if subprocess.call([PY, gate], stdout=subprocess.DEVNULL if gate == "selftest.py" else None):
            print(f"\n!! {why} — nothing was changed. Run `python {gate}` to see why.")
            return 1
    print("config + selftest: ok")

    unlock_db()
    subprocess.call([PY, "-c", "import store; p = store.backup();"
                             " print(f'backup: {p}' if p else 'backup: no database yet')"])

    failures = []
    stage("check firms", ["check_firms.py", "--only-new", "--fix"], failures)
    stage("sniff", ["sniff.py"] + (["firms.csv", "--recheck"] if args.full else []), failures)
    stage("discover", ["discover.py"] + (["--recheck"] if args.full else []), failures)

    if args.render:
        # The expensive one, and the only way to read the 86% of careers pages
        # that draw their listings in JavaScript. Anything it finds is written
        # to sniffed.csv, so the next run reads that firm through its API and
        # never needs the browser again.
        stage("render", ["render.py", "--limit", str(args.render_limit)], failures)

    # Cheap, no browser: Next.js / Nuxt / Redux sites serialise their job list
    # into the HTML we already fetch, so a large share of the "needs JavaScript"
    # pile is readable without one.
    stage("embedded jobs", ["embedded.py"], failures)

    stage("ats endpoints", ["scrape.py"], failures)
    stage("workday", ["workday.py"], failures)
    stage("reed+bullhorn", ["feeds.py", "--all"], failures)
    boards = ["boards.py", "--hours", "192"] + (["--no-linkedin"] if args.no_linkedin else [])
    stage("boards", boards, failures)
    stage("efinancial", ["efc.py", "--limit", "200"], failures)

    # Anything collected out of band — the Railway LinkedIn worker, or the
    # browser stage above. No-ops when the files are absent.
    for label, path in (("linkedin inbox", "linkedin_inbox.csv"),
                        ("rendered inbox", "rendered_inbox.csv"),
                        ("embedded inbox", "embedded_inbox.csv")):
        if os.path.exists(path):
            stage(label, ["inbox.py", "--file", path], failures)

    stage("verify", ["verify.py"], failures)
    stage("reparse", ["verify.py", "--reparse"], failures)

    if args.report_all:
        stage("score", ["score.py", "--everything"], failures)
    elif len(failures) < 5:
        stage("score", ["score.py", "--new-only", "--record"], failures)
    else:
        print(f"\n!! {len(failures)} stages failed — scoring without --record so "
              f"next run still sees this week's jobs")
        stage("score", ["score.py", "--new-only"], failures)

    stage("notify", ["notify.py"], failures)

    print("\n" + "=" * 60)
    if failures:
        print(f"{len(failures)} stage(s) failed: {', '.join(failures)}")
        print("The rest still ran — the report below covers everything collected.")
    else:
        print("every stage ok")
    lock_db()
    print("report.html and report.md are in this folder; scored.csv has the full list.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nstopped — partial results are saved", file=sys.stderr)
        sys.exit(130)
