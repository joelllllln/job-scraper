#!/usr/bin/env python3
"""
env.py — read .env, from whichever script you happened to run.

run.py loaded .env and nothing else did, so every stage worked when run through
run.py and silently lost its settings when run on its own. The failure is quiet
by design in each case — a missing SMTP_HOST is not an error, it means "no email
configured" — so `python notify.py` printed the digest to the terminal and said
"sent via: nothing configured" while a perfectly good .env sat two feet away.

Imported and called at the top of every entry point that reads a setting.
Never overwrites something already in the environment: a real environment
variable, or a GitHub Actions secret, always beats the file.
"""

import os

ENV_FILE = ".env"


def load(path=ENV_FILE):
    """Read KEY=value lines into os.environ. Returns True if the file existed."""
    if not os.path.exists(path):
        return False
    try:
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except OSError:
        return False
    return True
