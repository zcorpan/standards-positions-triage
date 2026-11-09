"""Terminal colors and OSC 8 links, only when stdout is a TTY (override with --color or SPTRIAGE_COLOR; NO_COLOR disables)."""

import contextlib
import os
import re
import sys

from . import config

_mode = None
CODES = {"green": "32", "red": "31", "yellow": "33", "blue": "34", "cyan": "36", "dim": "2", "bold": "1"}
POSITION_COLOR = {"positive": "green", "negative": "red", "neutral": "blue", "defer": "yellow",
                  "under consideration": "yellow"}
ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\]8;;[^\x1b]*\x1b\\")


def set_mode(mode):
    global _mode
    _mode = mode


@contextlib.contextmanager
def plain():
    global _mode
    old, _mode = _mode, "never"
    try:
        yield
    finally:
        _mode = old


def enabled():
    mode = _mode or os.environ.get("SPTRIAGE_COLOR") or "auto"
    if mode == "always":
        return True
    if mode == "never" or os.environ.get("NO_COLOR"):
        return False
    return sys.stdout.isatty()


def c(text, *styles):
    styles = [s for s in styles if s]
    if not styles or not enabled():
        return text
    return f"\x1b[{';'.join(CODES[s] for s in styles)}m{text}\x1b[0m"


def link(text, url):
    return f"\x1b]8;;{url}\x1b\\{text}\x1b]8;;\x1b\\" if enabled() else text


def issue_url(n):
    return f"https://github.com/{config.REPO}/issues/{n}"


def issue(n, width=0):
    text = f"#{n}"
    return " " * max(0, width - len(text)) + link(text, issue_url(n))


def label(name, sign=""):
    pos = name[len("position: "):] if name.startswith("position: ") else None
    style = POSITION_COLOR.get(pos) if pos else ("green" if sign == "+" else "red" if sign == "-" else None)
    return c(f"{sign}{name}", style, "bold" if pos else None)


def strip(text):
    return ANSI.sub("", text)
