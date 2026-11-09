"""Summarize logged corrections and skill notes, so recurring patterns stand out without reading raw logs."""

import collections
import json

from . import config

RECUR = 3


def _key(d, ctx):
    f = d["field"]
    if "dropped" in d and f != "comment":
        what = f"dropped {d['dropped']!r} from {f}"
    elif "added" in d and f != "comment":
        what = f"added {d['added']!r} to {f}"
    elif f == "comment":
        what = "comment dropped" if d.get("dropped") else "comment added" if d.get("added") else "comment rewritten"
    else:
        what = f"{f}: {d.get('draft')!r} → {d.get('final')!r}"
    where = (ctx or {}).get("why") or []
    return what, where[0] if where else None


def summary():
    p = config.state_dir() / "corrections.jsonl"
    notes = config.state_dir() / "skill-notes.md"
    out = []
    c = collections.Counter()
    issues = collections.defaultdict(list)
    total = 0
    if p.exists():
        for line in p.read_text().splitlines():
            e = json.loads(line)
            total += 1
            for d in e["diffs"]:
                k = _key(d, e.get("context"))
                c[k] += 1
                issues[k].append(e["issue"])
    applied = len(list((config.plans_dir() / "applied").glob("*[0-9].json"))) if (config.plans_dir() / "applied").exists() else 0
    out.append(f"{total} corrected plan(s) out of {applied} applied.")
    recurring = [(k, n) for k, n in c.most_common() if n >= RECUR]
    if recurring:
        out.append(f"Recurring (≥{RECUR}×), worth a data or skill change:")
        for (what, where), n in recurring:
            out.append(f"  {n}× {what}" + (f" (hint from {where})" if where else "") + f"  #{', #'.join(map(str, issues[(what, where)][-5:]))}")
    once = [(k, n) for k, n in c.most_common() if n < RECUR]
    if once:
        out.append(f"Below threshold ({len(once)} kinds); don't act on these alone.")
    if notes.exists() and notes.read_text().strip():
        out.append("\nskill-notes.md:\n" + notes.read_text().strip())
    else:
        out.append("No skill notes.")
    return "\n".join(out)
