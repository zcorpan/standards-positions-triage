"""Read-only bugzilla.mozilla.org: bug search for an s-p issue, and component → team."""

import json
import re
import urllib.parse

from .http import cached, get

BMO = "https://bugzilla.mozilla.org/rest"
FIELDS = "id,summary,status,resolution,product,component,see_also,url,last_change_time"
PRODUCTS = ["Core", "Remote Protocol", "WebExtensions", "DevTools", "Firefox", "Toolkit", "Testing", "Web Compatibility", "NSS"]
BUG_RE = re.compile(r"(?:https?://)?(?:bugzilla\.mozilla\.org/show_bug\.cgi\?id=|bugzil\.la/)(\d{4,8})")


def _get(path, **params):
    q = urllib.parse.urlencode(params, doseq=True)
    return json.loads(get(f"{BMO}/{path}?{q}", headers={"Accept": "application/json"}))


def teams():
    """{"Product::Component": team_name}, cached a day."""

    def fetch():
        d = _get("product", names=PRODUCTS, include_fields="name,components.name,components.team_name")
        return {f"{p['name']}::{c['name']}": c.get("team_name") for p in d["products"] for c in p["components"]}

    return cached("bmo-teams", 86400, fetch)


def bugs_in(text):
    return sorted(set(BUG_RE.findall(text or "")), key=int)


def _short(b):
    return {k: b.get(k) for k in ("id", "summary", "status", "resolution", "product", "component")}


def bug(bid):
    d = _get("bug", id=bid, include_fields=FIELDS)
    return _short(d["bugs"][0]) if d.get("bugs") else None


def search(issue_number, spec_url=None, title=None, limit=5):
    """Candidate bugs: see_also/URL pointing at the s-p issue or spec, then a quicksearch on the title."""
    found, out = set(), []

    def take(bugs, why):
        for b in bugs:
            if b["id"] not in found and len(out) < limit:
                found.add(b["id"])
                out.append({**_short(b), "why": why})

    sp = f"standards-positions/issues/{issue_number}"
    take(_get("bug", f1="see_also", o1="substring", v1=sp, include_fields=FIELDS, limit=10)["bugs"], "see_also s-p issue")
    take(_get("bug", f1="bug_file_loc", o1="substring", v1=sp, include_fields=FIELDS, limit=10)["bugs"], "URL s-p issue")
    if spec_url and "#" in spec_url:
        frag = spec_url.split("#", 1)[1]
        if len(frag) > 6:
            take(_get("bug", f1="see_also", o1="substring", v1=frag, include_fields=FIELDS, limit=10)["bugs"], "see_also spec anchor")
            take(_get("bug", f1="bug_file_loc", o1="substring", v1=frag, include_fields=FIELDS, limit=10)["bugs"], "URL spec anchor")
    if title and len(out) < limit:
        words = [w for w in re.findall(r"[\w:-]{3,}", title) if w.lower() not in {"the", "and", "for", "api", "with", "support"}][:6]
        if words:
            q = " ".join(words)
            take(_get("bug", quicksearch=q, include_fields=FIELDS, limit=10, order="changeddate DESC")["bugs"], f"quicksearch '{q}'")
    return out
