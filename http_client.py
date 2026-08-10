#!/usr/bin/env python3
"""
http_client.py — every outbound request goes through here.

The failure modes this exists to survive, all of which will happen on a long
unattended run:

  transient 5xx / connection resets  -> retry with exponential backoff + jitter
  429 with Retry-After               -> honour it rather than hammering
  one host being down                -> circuit breaker, stop wasting the run on it
  a host rate-limiting by volume     -> per-host minimum interval between requests
  a hung connection                  -> hard timeouts on connect and read separately
  a 200 that's actually an error page -> caller-supplied validator
  mojibake in titles                 -> encoding detection + entity unescape

Use `session()` for a configured Session, or `get()` / `post_json()` for the
retry-wrapped calls. Nothing else in the codebase should call requests directly.
"""

import html as _html
import random
import re
import threading
import time
import unicodedata
from collections import defaultdict
from urllib.parse import urlparse

import requests

CONNECT_TIMEOUT = 10
READ_TIMEOUT = 25
TIMEOUT = (CONNECT_TIMEOUT, READ_TIMEOUT)

MAX_RETRIES = 3
BACKOFF_BASE = 1.6
MAX_BACKOFF = 30
MIN_INTERVAL = 0.35          # seconds between requests to the same host
BREAKER_THRESHOLD = 6        # consecutive failures before a host is skipped
BREAKER_COOLDOWN = 900       # and for how long

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 522, 524}

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_lock = threading.Lock()
_last_hit = defaultdict(float)
_fails = defaultdict(int)
_open_until = defaultdict(float)


def host_of(url):
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return url


def _throttle(host):
    """Keep a minimum gap between requests to the same host, thread-safely."""
    while True:
        with _lock:
            now = time.monotonic()
            wait = _last_hit[host] + MIN_INTERVAL - now
            if wait <= 0:
                _last_hit[host] = now
                return
        time.sleep(min(wait, 2.0))


def breaker_open(host):
    with _lock:
        return time.monotonic() < _open_until[host]


def _record(host, ok):
    with _lock:
        if ok:
            _fails[host] = 0
        else:
            _fails[host] += 1
            if _fails[host] >= BREAKER_THRESHOLD:
                _open_until[host] = time.monotonic() + BREAKER_COOLDOWN
                _fails[host] = 0


def session(pool=20):
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-GB,en;q=0.9"})
    adapter = requests.adapters.HTTPAdapter(pool_connections=pool, pool_maxsize=pool,
                                            max_retries=0)   # we do our own
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


def request(method, url, sess=None, validator=None, retries=MAX_RETRIES, **kw):
    """Returns a Response, or None if it never succeeded. Never raises."""
    if not url or not url.startswith(("http://", "https://")):
        return None
    host = host_of(url)
    if breaker_open(host):
        return None

    sess = sess or session()
    kw.setdefault("timeout", TIMEOUT)
    kw.setdefault("allow_redirects", True)
    last = None

    for attempt in range(retries + 1):
        _throttle(host)
        try:
            r = sess.request(method, url, **kw)
            last = r
        except requests.RequestException:
            _record(host, False)
            if attempt == retries:
                return None
            time.sleep(min(MAX_BACKOFF, BACKOFF_BASE ** attempt) + random.uniform(0, 0.6))
            continue

        if r.status_code == 429:
            wait = r.headers.get("Retry-After")
            try:
                delay = min(120, float(wait)) if wait else min(MAX_BACKOFF, BACKOFF_BASE ** (attempt + 2))
            except ValueError:
                delay = min(MAX_BACKOFF, BACKOFF_BASE ** (attempt + 2))
            _record(host, False)
            if attempt == retries:
                return r
            time.sleep(delay + random.uniform(0, 1.0))
            continue

        if r.status_code in RETRY_STATUS:
            _record(host, False)
            if attempt == retries:
                return r
            time.sleep(min(MAX_BACKOFF, BACKOFF_BASE ** attempt) + random.uniform(0, 0.6))
            continue

        if validator and r.status_code == 200 and not validator(r):
            _record(host, False)
            return None

        _record(host, True)
        return r

    return last


def get(url, sess=None, **kw):
    return request("GET", url, sess=sess, **kw)


def post_json(url, payload, sess=None, **kw):
    return request("POST", url, sess=sess, json=payload, **kw)


def json_of(resp):
    """Parse JSON without letting a HTML error page raise."""
    if resp is None or resp.status_code != 200:
        return None
    ctype = (resp.headers.get("content-type") or "").lower()
    if "json" not in ctype and not resp.text.lstrip()[:1] in ("{", "["):
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def text_of(resp, limit=2_000_000):
    """Decoded text with a size guard — some career pages are enormous."""
    if resp is None:
        return ""
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding or "utf-8"
    try:
        return resp.text[:limit]
    except Exception:
        return ""


_WS = re.compile(r"\s+")


def clean_text(s, limit=400):
    """Titles and locations arrive with entities, tags and stray whitespace."""
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = _html.unescape(s)
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u200b", "").replace("\xa0", " ")
    return _WS.sub(" ", s).strip()[:limit]


def breaker_report():
    with _lock:
        now = time.monotonic()
        return {h: round(t - now) for h, t in _open_until.items() if t > now}
