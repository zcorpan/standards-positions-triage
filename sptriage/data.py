"""Load the repo's data files (taxonomy.yml, people.yml)."""

import urllib.parse
from functools import lru_cache

from . import config


def _yaml(name):
    from ruamel.yaml import YAML

    with open(config.ROOT / "data" / name) as f:
        return YAML(typ="safe", pure=True).load(f) or {}


@lru_cache(maxsize=None)
def taxonomy():
    return _yaml("taxonomy.yml")


@lru_cache(maxsize=None)
def people():
    """{github login: gecko nick}"""
    return {str(k): str(v) for k, v in _yaml("people.yml").items()}


@lru_cache(maxsize=None)
def nick_to_login():
    return {v.lower(): k for k, v in people().items()}


def host_key(url):
    u = urllib.parse.urlsplit(url or "")
    host = u.netloc.lower().removeprefix("www.")
    if host == "github.com":
        parts = [p for p in u.path.lower().split("/") if p]
        return "github.com/" + "/".join(parts[:2])
    return host + u.path.lower()


def spec_hint(url):
    """{venue, topic, match} for a spec URL, or None."""
    if not url:
        return None
    key = host_key(url)
    host = key.split("/", 1)[0]
    for rule in taxonomy()["spec_hosts"]:
        m = rule["match"].lower()
        if (m.startswith("github.com/") and (key == m or key.startswith(m + "/") or key.startswith(m))) or \
           (not m.startswith("github.com/") and (host == m or host.endswith("." + m) or key.startswith(m) or m in host)):
            return {"venue": rule.get("venue"), "topic": rule.get("topic"), "match": rule["match"]}
    return None
