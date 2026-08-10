#!/usr/bin/env python3
"""
notify.py — deliver the weekly digest, and warn when a source has gone quiet.

Two jobs:

1. Send report.html somewhere you'll actually read it. Email (any SMTP) or
   Telegram. Both optional; with neither configured it prints and exits.

2. Health check. The real failure mode of a scraper like this isn't crashing —
   it's a source silently returning zero for weeks after an endpoint changes,
   while the digest still looks normal. So it compares this run's per-source
   counts against the trailing average and flags anything that fell off.

Email:
    export SMTP_HOST=smtp.gmail.com SMTP_PORT=587
    export SMTP_USER=you@gmail.com SMTP_PASS=app-password
    export DIGEST_TO=you@gmail.com

Telegram (easier on mobile):
    export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=...

    python notify.py
"""

import csv
import os
import smtplib
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

DB = "jobs.db"


def health(con):
    """Per-source counts this week vs the trailing four weeks."""
    now = datetime.now(timezone.utc)
    week = (now - timedelta(days=7)).isoformat(timespec="seconds")
    month = (now - timedelta(days=35)).isoformat(timespec="seconds")

    recent = dict(con.execute(
        "SELECT source, COUNT(*) FROM jobs WHERE first_seen > ? GROUP BY source", (week,)))
    prior = dict(con.execute(
        "SELECT source, COUNT(*) FROM jobs WHERE first_seen > ? AND first_seen <= ? GROUP BY source",
        (month, week)))

    warnings = []
    for src, before in prior.items():
        avg = before / 4.0
        now_n = recent.get(src, 0)
        if avg >= 2 and now_n == 0:
            warnings.append(f"{src}: 0 this week, averaged {avg:.1f}/wk — endpoint probably broke")
        elif avg >= 5 and now_n < avg * 0.3:
            warnings.append(f"{src}: {now_n} this week vs {avg:.1f}/wk average — check it")
    return recent, warnings


def summary(con):
    con.row_factory = sqlite3.Row
    week = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    n_new = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE first_seen > ? AND COALESCE(status,'new')='new'",
        (week,)).fetchone()[0]
    open_total = con.execute(
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(status,'new')='new'").fetchone()[0]
    applied = con.execute("SELECT COUNT(*) FROM jobs WHERE status='applied'").fetchone()[0]
    return n_new, open_total, applied


def digest_count(path="scored.csv"):
    """How many roles are actually in this digest.

    Not the same as the week's new rows: the seniority and years-of-experience
    filters drop roles after collection, so counting the database would put a
    number in the subject line that the body then contradicts.
    """
    try:
        with open(path, newline="") as fh:
            return max(0, sum(1 for _ in csv.reader(fh)) - 1)   # minus the header
    except OSError:
        return None


def send_email(subject, html_body, text_body):
    """Returns True only if it actually sent. Never raises — a mail server being
    down must not fail the run after the scraping already succeeded."""
    host, user = os.getenv("SMTP_HOST"), os.getenv("SMTP_USER")
    to = os.getenv("DIGEST_TO") or user
    if not (host and user and to):
        return False
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    # `or 587`, not a getenv default: an unset GitHub secret arrives as an empty
    # string, and int("") would raise into the catch below — silently no email.
    port = int(os.getenv("SMTP_PORT") or 587)
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(user, os.getenv("SMTP_PASS", ""))
            s.send_message(msg)
        return True
    except Exception as e:
        print(f"email failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def send_telegram(text):
    token, chat = os.getenv("TELEGRAM_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat):
        return False
    try:
        import http_client
        r = http_client.request("POST", f"https://api.telegram.org/bot{token}/sendMessage",
                                data={"chat_id": chat, "text": text[:4000],
                                      "disable_web_page_preview": True})
        return r is not None and r.status_code == 200
    except Exception as e:
        print(f"telegram failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


def main():
    if not os.path.exists(DB):
        print("no database yet — nothing to notify about", file=sys.stderr)
        return
    con = sqlite3.connect(DB)
    n_new, open_total, applied = summary(con)
    counts, warnings = health(con)

    try:
        html_body = open("report.html").read()
        text_body = open("report.md").read()
    except FileNotFoundError:
        print("no report — run score.py first", file=sys.stderr)
        return

    date = datetime.now(timezone.utc).strftime("%d %b")
    shown = digest_count()
    subject = f"{shown if shown is not None else n_new} roles — {date}"

    lines = [subject, ""]
    # generous cap: this is the plain-text half of the email, and on a --resend-all
    # run it carries the whole open list. 40 lines used to cut it off mid-shortlist.
    lines += [l for l in text_body.splitlines() if l.strip()][:200]
    lines += ["", f"open: {open_total} · applied: {applied}"]
    if warnings:
        lines += ["", "sources to check:"] + [f"  {w}" for w in warnings]
    plain = "\n".join(lines)

    print(plain)
    if warnings:
        print("\n".join(f"\nWARN  {w}" for w in warnings), file=sys.stderr)

    sent = []
    if send_email(subject, html_body, plain):
        sent.append("email")
    if send_telegram(plain):
        sent.append("telegram")
    print(f"\nsent via: {', '.join(sent) or 'nothing configured — printed above'}")


if __name__ == "__main__":
    main()
