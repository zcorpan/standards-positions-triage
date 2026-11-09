"""Small HTTP GET helper with gzip and an on-disk TTL cache."""

import gzip
import hashlib
import json
import time
import urllib.parse
import urllib.request

from . import config

UA = "sptriage (+https://github.com/zcorpan/standards-positions-triage)"


def get(url, headers=None, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
    return data


_MEMO = {}


def cached(name, ttl, fetch):
    """Return JSON from `.cache/<name>.json` if younger than `ttl` seconds, else `fetch()` and store it.
    Memoized per process."""
    if name in _MEMO:
        return _MEMO[name]
    path = config.cache_dir() / f"{name}.json"
    if path.exists() and time.time() - path.stat().st_mtime < ttl:
        with open(path) as f:
            _MEMO[name] = json.load(f)
            return _MEMO[name]
    data = fetch()
    _MEMO[name] = data
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f)
    tmp.replace(path)
    return data


def get_json(url, ttl=None, xssi=False):
    def fetch():
        raw = get(url).decode("utf-8")
        if xssi and raw.startswith(")]}'"):
            raw = raw[raw.index("\n") + 1 :]
        return json.loads(raw)

    if ttl is None:
        return fetch()
    key = urllib.parse.urlsplit(url).netloc.split(".")[0] + "-" + hashlib.sha1(url.encode()).hexdigest()[:12]
    return cached(f"http/{key}", ttl, fetch) if _ensure_http_dir() else fetch()


def _ensure_http_dir():
    (config.cache_dir() / "http").mkdir(exist_ok=True)
    return True


def invalidate(name):
    _MEMO.pop(name, None)
    (config.cache_dir() / f"{name}.json").unlink(missing_ok=True)
