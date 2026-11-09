"""Gecko ownership from a local mozilla/firefox checkout: moz.build BUG_COMPONENT, mots.yaml, git-log reviewers."""

import collections
import fnmatch
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from . import config
from .http import cached

_FILES_RE = re.compile(r"^with Files\(([^)]*)\):\s*\n((?:[ \t]+.*\n?|\s*\n)*)", re.M)
_BC_RE = re.compile(r"BUG_COMPONENT\s*=\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]")
_STR_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_REVIEW_RE = re.compile(r"\br=([^\s]+)")


def root():
    return config.firefox_path()


def _glob_match(pattern, rel):
    rx = re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*\*", ".*").replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
    return re.fullmatch(rx, rel) is not None or (pattern.endswith("/**") and rel.startswith(pattern[:-3]))


@lru_cache(maxsize=None)
def _mozbuild_rules(path):
    try:
        text = Path(path).read_text(errors="replace")
    except OSError:
        return []
    rules = []
    for m in _FILES_RE.finditer(text):
        bc = _BC_RE.search(m.group(2))
        if bc:
            for pat in _STR_RE.findall(m.group(1)):
                rules.append((pat, f"{bc.group(1)}::{bc.group(2)}"))
    return rules


def bug_component(relpath):
    """Bugzilla 'Product::Component' for a path in the tree (deepest moz.build wins; later rules override)."""
    r = root()
    if not r:
        return None
    parts = Path(relpath).parts
    result = None
    for i in range(len(parts) + 1):
        d = Path(*parts[:i]) if i else Path(".")
        mb = r / d / "moz.build"
        if not mb.exists():
            continue
        rel = str(Path(*parts[i:])) if i < len(parts) else ""
        for pat, comp in _mozbuild_rules(str(mb)):
            if _glob_match(pat, rel) or (rel and _glob_match(pat, rel + "/x")):
                result = comp
    return result


def mots():
    """{"modules": [{name, includes, components, owners, peers, review_group}], "people": {nick: name}}."""
    r = root()

    def fetch():
        from ruamel.yaml import YAML

        if r:
            text = (r / "mots.yaml").read_text()
        else:
            from .http import get
            text = get("https://raw.githubusercontent.com/mozilla-firefox/firefox/main/mots.yaml").decode()
        d = YAML(typ="safe", pure=True).load(text)
        mods = []

        def walk(ms, parent=None):
            for m in ms or []:
                meta = m.get("meta") or {}
                mods.append({
                    "name": m["name"],
                    "parent": parent,
                    "includes": m.get("includes") or [],
                    "components": meta.get("components") or [],
                    "owners": [p.get("nick") for p in m.get("owners") or [] if p.get("nick")],
                    "peers": [p.get("nick") for p in m.get("peers") or [] if p.get("nick")],
                    "review_group": meta.get("review_group"),
                })
                walk(m.get("submodules"), m["name"])

        walk(d.get("modules"))
        people = {p["nick"]: p.get("name") for p in d.get("people") or [] if p.get("nick")}
        return {"modules": mods, "people": people}

    return cached("mots", 86400, fetch)


def modules_for(component=None, path=None):
    """Modules for a Bugzilla component and/or path: component matches first, then by the most specific
    matching include (longest literal prefix); component-only matches prefer the main (largest) module."""
    out = []
    for m in mots()["modules"]:
        comp = bool(component) and component in m["components"]
        spec = 0
        if path:
            for inc in m["includes"]:
                if _glob_match(inc, path) or _glob_match(inc, path + "/x"):
                    spec = max(spec, len(inc.split("*", 1)[0]))
        if comp or spec:
            out.append(((not comp), -spec, -len(m["includes"]), m))
    return [m for *_, m in sorted(out, key=lambda x: x[:3])]


def group_nicks():
    """Review-group names as they appear in `r=` lines without the `#…-reviewers` form (e.g. webidl, dom-core)."""
    out = {"webidl", "dom-core", "dom", "layout", "style", "necko", "media-playback", "webrtc", "gfx", "devtools",
           "frontend", "platform-i18n", "spidermonkey", "jsparser", "dom-worker", "dom-storage", "sheriffs", "glandium"}
    out -= {"glandium"}
    for m in mots()["modules"]:
        g = m.get("review_group")
        if g:
            out.add(g.removesuffix("-reviewers"))
    return out


def _git(*args):
    r = root()
    p = subprocess.run(["git", "-C", str(r), *args], capture_output=True, text=True)
    return p.stdout


def reviewers(relpath, n=300):
    """Top `r=` reviewers (nicks and groups) over the last `n` commits touching `relpath`."""
    if not root():
        return []

    def fetch():
        c = collections.Counter()
        for line in _git("log", f"-n{n}", "--format=%s", "--", relpath).splitlines():
            for m in _REVIEW_RE.findall(line):
                for nick in m.split(","):
                    nick = nick.strip("!.;:()[]").lstrip("#")
                    if nick:
                        if nick.endswith("-reviewers"):
                            nick = "#" + nick
                        c[nick] += 1
        return c.most_common(8)

    key = re.sub(r"[^\w.-]", "_", relpath)
    (config.cache_dir() / "reviewers").mkdir(exist_ok=True)
    return [tuple(x) for x in cached(f"reviewers/{key}", 7 * 86400, fetch)]


@lru_cache(maxsize=None)
def _webidl_index():
    r = root()
    if not r:
        return {}
    return {p.stem: str(p.relative_to(r)) for p in (r / "dom" / "webidl").glob("*.webidl")}


def impl_dir(interface):
    """Directory implementing a DOM interface, e.g. OfflineAudioContext → dom/media/webaudio."""
    r = root()
    if not r:
        return None
    out = _git("ls-files", "--", f"*/{interface}.cpp", f"*/{interface}.h", f"*/{interface}.rs")
    paths = [p for p in out.splitlines() if "/test" not in p and not p.startswith("obj")]
    if paths:
        return str(Path(sorted(paths, key=len)[0]).parent)
    return None


def for_bcd_key(key):
    """Ownership hints for a BCD key: {path, component} where we can find one."""
    parts = key.split(".")
    if parts[0] == "api" and len(parts) > 1:
        iface = parts[1]
        d = impl_dir(iface)
        webidl = _webidl_index().get(iface)
        comp = bug_component(webidl) if webidl else (bug_component(d + "/x") if d else None)
        return {"path": d, "component": comp, "via": f"{iface} ({webidl or d})"}
    if parts[0] == "css":
        return {"path": "layout/style", "component": "Core::CSS Parsing and Computation", "via": "css"}
    if parts[0] == "javascript":
        return {"path": "js/src/builtin", "component": "Core::JavaScript: Standard Library", "via": "javascript"}
    return None


def for_wpt_dir(wpt):
    """Ownership hint for a wpt path like 'html/semantics/menu'."""
    rel = f"testing/web-platform/tests/{wpt.strip('/')}"
    return {"path": None, "component": bug_component(rel), "via": f"wpt {wpt}"}
