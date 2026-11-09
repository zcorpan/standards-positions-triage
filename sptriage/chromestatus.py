"""chromestatus.com: Chromium features waiting on a Mozilla review, and per-feature details."""

import re

from .http import get_json

BASE = "https://chromestatus.com/api/v0"
STAGE_ORDER = ["shipping", "origin-trial", "dev-trial", "wide-review", "prototyping", "incubating", "shipped"]
ISSUE_RE = re.compile(r"mozilla/standards-positions/(?:issues|pull)/(\d+)", re.I)
POSITION = {1: "shipped", 2: "in development", 3: "support", 4: "public skepticism", 5: "no signal",
            6: "under consideration", 7: "oppose", 8: "neutral", 9: "n/a"}


def reviews():
    """{issue number: {feature_id, name, stage, start, end}} for features whose Gecko review is pending."""
    data = get_json(f"{BASE}/external_reviews/gecko", ttl=6 * 3600, xssi=True)
    out = {}
    for r in data.get("reviews", []):
        m = ISSUE_RE.search(r.get("review_link") or "")
        if not m:
            continue
        n = int(m.group(1))
        item = {
            "feature_id": r["feature"]["id"],
            "name": r["feature"]["name"],
            "stage": r.get("current_stage"),
            "start": r.get("estimated_start_milestone"),
            "end": r.get("estimated_end_milestone"),
        }
        # several features can point at one issue; keep the most urgent
        if n not in out or urgency(item, 0) > urgency(out[n], 0):
            out[n] = item
    return out


def stable_milestone():
    try:
        return int(get_json(f"{BASE}/channels", ttl=86400, xssi=True)["stable"]["mstone"])
    except Exception:
        return None


def urgency(item, stable):
    """Higher is more urgent: shipping soon beats incubating; already shipped sorts low."""
    if not item:
        return 0
    stage = item.get("stage") or ""
    base = {"shipping": 60, "origin-trial": 40, "dev-trial": 25, "wide-review": 20, "prototyping": 15,
            "incubating": 10, "shipped": 5}.get(stage, 0)
    m = item.get("end") or item.get("start")
    if stage in ("shipping", "origin-trial") and m and stable:
        delta = m - stable
        # estimates far in the past are usually stale
        base += 30 if -2 <= delta <= 2 else 15 if 2 < delta <= 6 else 0
    return base


def feature(fid):
    d = get_json(f"{BASE}/features/{fid}", ttl=86400, xssi=True)
    b = d.get("browsers") or {}

    def view(key):
        v = (b.get(key) or {}).get("view") or {}
        return {"url": v.get("url"), "text": v.get("text")}

    wpt = re.findall(r"wpt\.fyi/results/([^\s?#]+)", d.get("wpt_descr") or "")
    intents = [s.get("intent_thread_url") for s in d.get("stages") or [] if s.get("intent_thread_url")]
    return {
        "id": fid,
        "name": d.get("name"),
        "url": f"https://chromestatus.com/feature/{fid}",
        "summary": (d.get("summary") or "").strip(),
        "spec": d.get("spec_link") or (d.get("standards") or {}).get("spec"),
        "explainers": d.get("explainer_links") or [],
        "web_feature": d.get("web_feature") or None,
        "wpt_dirs": wpt,
        "blink_components": d.get("blink_components") or [],
        "feature_type": d.get("feature_type"),
        "safari": view("safari"),
        "firefox": view("ff"),
        "tag_review": d.get("tag_review"),
        "intents": intents[-2:],
    }
