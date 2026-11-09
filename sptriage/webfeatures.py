"""web-features ids and BCD data: match an issue to features/compat keys, Firefox support, MDN, bugs."""

import json
import re
import subprocess
import urllib.parse

from . import config
from .http import cached, get

WF_URL = "https://github.com/web-platform-dx/web-features/releases/latest/download/data.json"
BCD_URL = "https://unpkg.com/@mdn/browser-compat-data/data.json"
BCD_TREES = ["api", "css", "html", "http", "javascript", "manifests", "mathml", "mediatypes", "svg",
             "webassembly", "webdriver", "webextensions"]
BUG_RE = re.compile(r"(?:bugzilla\.mozilla\.org/show_bug\.cgi\?id=|bugzil\.la/|\bbug\s*)(\d{5,8})", re.I)


def features():
    """{id: {name, spec[], caniuse[], keys[], group[], ff, baseline, redirect}} (slimmed, cached a day)."""

    def fetch():
        d = json.loads(get(WF_URL, timeout=120))
        out = {}
        for fid, f in d["features"].items():
            if f.get("kind") != "feature":
                out[fid] = {"redirect": f.get("redirect_target") or (f.get("redirect_targets") or [None])[0]}
                continue
            st = f.get("status") or {}
            out[fid] = {
                "name": f.get("name"),
                "spec": f.get("spec") if isinstance(f.get("spec"), list) else [f.get("spec")] if f.get("spec") else [],
                "caniuse": f.get("caniuse") or [],
                "keys": f.get("compat_features") or [],
                "group": f.get("group") if isinstance(f.get("group"), list) else [f.get("group")] if f.get("group") else [],
                "ff": (st.get("support") or {}).get("firefox"),
                "support": {k: v for k, v in (st.get("support") or {}).items() if k in ("chrome", "firefox", "safari")},
                "baseline": st.get("baseline"),
            }
        return out

    return cached("web-features", 86400, fetch)


def resolve(fid):
    fs = features()
    seen = set()
    while fid in fs and "redirect" in fs[fid] and fid not in seen:
        seen.add(fid)
        fid = fs[fid]["redirect"]
    return fid if fid in fs else None


def _bcd_raw():
    path = config.bcd_path()
    if path:
        head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
        return f"bcd-{head[:10] or 'local'}", lambda: _walk_local(path)
    return "bcd-npm", lambda: json.loads(get(BCD_URL, timeout=180))


def _walk_local(path):
    tree = {}
    for top in BCD_TREES:
        for f in sorted((path / top).rglob("*.json")):
            with open(f) as fh:
                _merge(tree, json.load(fh))
    return tree


def _merge(a, b):
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(a.get(k), dict):
            _merge(a[k], v)
        else:
            a[k] = v


def bcd():
    """{key: {mdn, spec[], ff, ff_bugs[], tags[]}} for every BCD compat key (cached per BCD version)."""
    name, load = _bcd_raw()

    def fetch():
        raw = load()
        out = {}

        def walk(node, key):
            c = node.get("__compat")
            if c:
                ff = (c.get("support") or {}).get("firefox")
                ff = ff[0] if isinstance(ff, list) else ff or {}
                spec = c.get("spec_url") or []
                out[key] = {
                    "mdn": c.get("mdn_url"),
                    "spec": spec if isinstance(spec, list) else [spec],
                    "ff": ff.get("version_added"),
                    "ff_flags": bool(ff.get("flags")),
                    "ff_bugs": sorted(set(BUG_RE.findall(json.dumps(ff.get("impl_url") or "")))),
                    "tags": [t.split(":", 1)[1] for t in c.get("tags") or [] if t.startswith("web-features:")],
                }
            for k, v in node.items():
                if k != "__compat" and isinstance(v, dict):
                    walk(v, f"{key}.{k}" if key else k)

        for top in BCD_TREES:
            if top in raw:
                walk(raw[top], top)
        return out

    return cached(name, 30 * 86400, fetch)


_SPEC_INDEX = {}


def spec_index():
    """{(shortname, fragment): [BCD keys]}"""
    if not _SPEC_INDEX:
        for k, v in bcd().items():
            for s in v["spec"]:
                _SPEC_INDEX.setdefault(spec_key(s), []).append(k)
    return _SPEC_INDEX


def keys_for_spec(url, limit=2):
    """Most common BCD key stems (e.g. api.Notification) among keys whose spec_url shares the spec's shortname."""
    short, _ = spec_key(url)
    if not short:
        return []
    import collections
    c = collections.Counter()
    for (s, _f), keys in spec_index().items():
        if s == short:
            for k in keys:
                c[".".join(k.split(".")[:2])] += 1
    return [k for k, _ in c.most_common(limit)]


def spec_key(url):
    """(shortname, fragment) for loose spec URL comparison across w3.org/TR, github.io, csswg drafts, whatwg."""
    if not url:
        return None, None
    u = urllib.parse.urlsplit(url.strip())
    host = u.netloc.lower().removeprefix("www.")
    parts = [p for p in u.path.split("/") if p]
    if host.endswith(".spec.whatwg.org"):
        short = host.split(".")[0]
    elif host == "w3.org" and parts[:1] == ["TR"]:
        short = parts[1] if len(parts) > 1 else ""
    else:
        short = parts[0] if parts else host
    short = re.sub(r"-\d+$", "", short.lower())
    return short, (u.fragment or None)


def match(spec_url=None, ids=(), title=None):
    """Best guesses for an issue. Returns {"features": [...], "keys": [...]} with reasons."""
    fs = features()
    found = {}

    def add(fid, why):
        fid = resolve(fid)
        if fid and fid not in found:
            found[fid] = why

    for fid in ids:
        if fid:
            add(fid, "given")
    keys = []
    short, frag = spec_key(spec_url)
    if short:
        b = bcd()
        if frag:
            keys = list(spec_index().get((short, frag), []))
            for k in keys:
                for t in b[k]["tags"]:
                    add(t, f"BCD {k} spec_url")
        if not found:
            cands = [fid for fid, f in fs.items() if "redirect" not in f and any(spec_key(s)[0] == short for s in f["spec"])]
            if len(cands) <= 3:
                for fid in cands:
                    add(fid, "same spec")
            elif title:
                words = set(re.findall(r"[a-z]{4,}", title.lower())) - {"with", "from", "support", "level", "property", "element", "attribute", "function"}
                for fid in cands:
                    common = words & set(re.findall(r"[a-z]{4,}", f"{fid} {fs[fid]['name']}".lower()))
                    if len(common) >= 2:
                        add(fid, f"guess: same spec, name shares {'/'.join(sorted(common))}")
    out = []
    for fid, why in found.items():
        f = fs[fid]
        out.append({"id": fid, "why": why, "name": f["name"], "support": f["support"], "baseline": f["baseline"],
                    "caniuse": f["caniuse"][:1], "keys": len(f["keys"])})
    b = bcd() if keys or out else {}
    key_info = []
    for k in (keys or [k for o in out[:1] for k in fs[o["id"]]["keys"][:1]]):
        if k in b:
            v = b[k]
            key_info.append({"key": k, "ff": v["ff"], "ff_flags": v["ff_flags"], "bugs": v["ff_bugs"], "mdn": v["mdn"]})
    return {"features": out, "keys": key_info[:5]}


def topics_for(keys):
    """BCD top-level dirs for compat keys, e.g. {"api", "css"}."""
    return sorted({k.split(".", 1)[0] for k in keys})
