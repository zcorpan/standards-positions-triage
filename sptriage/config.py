"""Paths and settings. Optional overrides in `sptriage.toml` at the repo root."""

import os
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "mozilla/standards-positions"
OWNER, NAME = REPO.split("/")
PROJECT_NUMBER = 262
CONTRIBUTOR_TEAM = "standards-contributor"
STATUSES = [
    "Unscreened",
    "Needs assignees",
    "Needs proposed position",
    "Position is proposed",
    "Needs a PR",
    "Blocked",
]

_DEFAULTS = {
    "cache_dir": str(ROOT / ".cache"),
    "state_dir": str(ROOT / ".state"),
    "plans_dir": str(ROOT / "plans"),
    "review_dir": str(ROOT / "review"),
    "firefox_path": "~/git/mozilla/firefox",
    "bcd_path": "~/git/mdn/browser-compat-data",
    "sp_path": "~/git/mozilla/standards-positions",
    "stall_days": 90,
    "ping_days": 30,
    "close_days": 7,
}


def _load():
    conf = dict(_DEFAULTS)
    path = Path(os.environ.get("SPTRIAGE_CONFIG", ROOT / "sptriage.toml"))
    if path.exists():
        with open(path, "rb") as f:
            conf.update(tomllib.load(f))
    for key in ("cache_dir", "state_dir", "plans_dir", "review_dir", "firefox_path", "bcd_path"):
        conf[key] = Path(os.path.expanduser(conf[key]))
    return conf


CONF = _load()


def cache_dir():
    CONF["cache_dir"].mkdir(parents=True, exist_ok=True)
    return CONF["cache_dir"]


def state_dir():
    CONF["state_dir"].mkdir(parents=True, exist_ok=True)
    return CONF["state_dir"]


def plans_dir():
    CONF["plans_dir"].mkdir(parents=True, exist_ok=True)
    return CONF["plans_dir"]


def review_dir():
    CONF["review_dir"].mkdir(parents=True, exist_ok=True)
    return CONF["review_dir"]


def firefox_path():
    p = CONF["firefox_path"]
    return p if (p / "mots.yaml").exists() else None


def bcd_path():
    p = CONF["bcd_path"]
    return p if (p / "api").is_dir() else None
