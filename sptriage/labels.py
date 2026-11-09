"""Label taxonomy vs live Bugzilla teams and BCD dirs: gaps, stale mappings, proposals for new labels."""

import collections
import json

from . import body as bodymod
from . import bugzilla, config, data, sync, webfeatures
from .dossier import hints

MIN_ISSUES = 3


def light_hints(issue, ctx, deep=False):
    """Hints from cached dossiers, else OP web-feature/spec URL → BCD → Gecko (plus chromestatus with `deep`)."""
    p = config.cache_dir() / "dossiers" / f"{issue['number']}.json"
    if p.exists():
        return json.loads(p.read_text())["hints"]
    b = bodymod.Body(issue["body"])
    feat = None
    cs = ctx.chromestatus.get(issue["number"]) if deep else None
    if cs:
        from . import chromestatus
        try:
            feat = chromestatus.feature(cs["feature_id"])
        except Exception:
            feat = None
    ids = [x for x in (b.value("web-feature"), (feat or {}).get("web_feature")) if x]
    wf = webfeatures.match(b.value("spec-url") or (feat or {}).get("spec"), ids=ids)
    return hints(issue, ctx, feat, wf)


def report(ctx, write_plan=False, deep=False):
    tax = data.taxonomy()
    bmo_live = set(bugzilla.teams().values()) - {None}
    team_map = tax["bmo_teams"]
    labels = sync.repo_labels()
    team_labels = {l for l in labels if l.startswith("team: ")}
    topic_labels = {l for l in labels if l.startswith("topic: ")}

    gaps_team, gaps_topic = collections.defaultdict(list), collections.defaultdict(list)
    used = collections.Counter()
    for n, issue in ctx.issues.items():
        if issue["state"] != "OPEN":
            continue
        for l in issue["labels"]:
            used[l] += 1
        if any(l.startswith("team: ") for l in issue["labels"]):
            continue
        h = light_hints(issue, ctx, deep)
        for t in h.get("team_gaps", []):
            gaps_team[t].append(n)
        for t in h.get("topic_gaps", []):
            gaps_topic[t].append(n)

    out = ["Team labels (open issues using them):"]
    for l in sorted(team_labels):
        teams = [t for t, v in team_map.items() if v == l]
        out.append(f"  {l:22} {used[l]:>3}  ← BMO {', '.join(teams) or '(no BMO team mapped)'}")
    out.append("\nBMO teams without a label (open issues whose hints point there):")
    for t in sorted(gaps_team, key=lambda t: -len(gaps_team[t])):
        out.append(f"  {t:28} {len(gaps_team[t]):>3}  e.g. {', '.join('#' + str(x) for x in gaps_team[t][:6])}")
    unmapped_live = sorted(t for t in bmo_live if t not in team_map)
    if unmapped_live:
        out.append(f"\nBMO teams not in taxonomy.yml (add as null or map them): {', '.join(unmapped_live)}")
    stale = sorted(t for t in team_map if t not in bmo_live)
    if stale:
        out.append(f"Teams in taxonomy.yml that BMO no longer has: {', '.join(stale)}")
    missing_label = sorted({v for v in team_map.values() if v and v not in labels})
    if missing_label:
        out.append(f"taxonomy.yml maps to labels that don't exist: {', '.join(missing_label)}")
    for l, why in (tax.get("ambiguous") or {}).items():
        out.append(f"Ambiguous: {l} {why} (report only; renames/splits are a human call)")

    out.append("\nTopic labels vs BCD dirs:")
    for d, l in tax["bcd_topics"].items():
        if l:
            if l not in topic_labels:
                out.append(f"  {d}: maps to missing label {l}")
        else:
            out.append(f"  {d:14} no label · {len(gaps_topic.get(d, []))} open issue(s) {', '.join('#' + str(x) for x in gaps_topic.get(d, [])[:6])}")

    proposals = []
    color = next((labels[l]["color"] for l in sorted(team_labels)), "ededed")
    tcolor = next((labels[l]["color"] for l in sorted(topic_labels)), "ededed")
    for t, ns in gaps_team.items():
        if len(ns) >= MIN_ISSUES:
            proposals.append({"name": f"team: {t}", "color": color, "description": f"Bugzilla team {t}", "issues": ns})
    for d, ns in gaps_topic.items():
        if len(ns) >= MIN_ISSUES:
            name = {"manifests": "Manifests", "mediatypes": "Media types"}.get(d, d.capitalize())
            proposals.append({"name": f"topic: {name}", "color": tcolor, "description": f"BCD {d}", "issues": ns})
    if proposals:
        out.append(f"\nProposed new labels (≥{MIN_ISSUES} open issues):")
        for p in proposals:
            out.append(f"  {p['name']!r}: {len(p['issues'])} issues")
        if write_plan:
            plan = {"create_labels": [{k: p[k] for k in ("name", "color", "description")} for p in proposals]}
            (config.plans_dir() / "labels.json").write_text(json.dumps(plan, indent=1) + "\n")
            out.append(f"  wrote {config.plans_dir() / 'labels.json'}; also map them in data/taxonomy.yml")
    else:
        out.append(f"\nNo gap reaches {MIN_ISSUES} open issues; no new labels proposed.")
    out.append("\n(Hints come from cached dossiers or OP web-feature/spec URL → BCD; run `dossier` on more issues to sharpen.)")
    return "\n".join(out)
