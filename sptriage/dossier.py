"""Compact per-issue context for triage judgment."""

import json
import secrets

from . import body as bodymod
from . import bugzilla, checks, config, data, firefox, people, sync, webfeatures
from .rank import STATUS_ABBR

OP_CAP, COMMENT_CAP, MOZ_COMMENT_CAP, COMMENTS = 2000, 500, 4000, 8


def hints(issue, ctx, cs_feature=None, wf=None):
    """Label and ownership hints: {venue, topics, teams, team_gaps, topic_gaps, ownership[]}"""
    b = bodymod.Body(issue["body"])
    tax = data.taxonomy()
    out = {"venue": None, "topics": [], "teams": [], "team_gaps": [], "topic_gaps": [], "ownership": [], "why": []}
    spec = b.value("spec-url") or (cs_feature or {}).get("spec")
    h = data.spec_hint(spec)
    if h:
        out["venue"] = f"venue: {h['venue']}" if h.get("venue") else None
        if h.get("topic"):
            out["topics"].append(f"topic: {h['topic']}")
        out["why"].append(f"spec host {h['match']}")
    keys = [k["key"] for k in (wf or {}).get("keys", [])]
    if not keys and wf and wf.get("features"):
        keys = webfeatures.features()[wf["features"][0]["id"]]["keys"][:3]
    if not keys and spec:
        keys = webfeatures.keys_for_spec(spec)
        if keys:
            out["why"].append(f"BCD keys sharing the spec: {', '.join(keys)}")
    for top in webfeatures.topics_for(keys):
        lbl = tax["bcd_topics"].get(top)
        if lbl:
            out["topics"].append(lbl)
        else:
            out["topic_gaps"].append(top)
    for k in keys[:2]:
        o = firefox.for_bcd_key(k)
        if o and (o["component"] or o["path"]):
            out["ownership"].append(o)
    for w in (cs_feature or {}).get("wpt_dirs", [])[:2]:
        d = "/".join(w.split("/")[:-1]) if "." in w.split("/")[-1] else w
        o = firefox.for_wpt_dir(d)
        if o["component"]:
            out["ownership"].append(o)
    bmo = bugzilla.teams()
    seen = set()
    for o in out["ownership"]:
        team = bmo.get(o["component"] or "")
        if not team or team in seen:
            continue
        seen.add(team)
        lbl = tax["bmo_teams"].get(team, None)
        o["team"] = team
        if lbl:
            out["teams"].append(lbl)
        else:
            out["team_gaps"].append(team)
    for k in ("topics", "teams", "team_gaps", "topic_gaps"):
        out[k] = list(dict.fromkeys(out[k]))
    return out


def _wrap(text, cap):
    text = (text or "").strip()
    if len(text) > cap:
        text = text[:cap] + " […]"
    tag = secrets.token_hex(4)
    return f"<untrusted id={tag}>\n{text}\n</untrusted id={tag}>"


def build(number, ctx, refresh=True):
    issue = sync.refresh_issue(number) if refresh else ctx.issues[number]
    ctx.issues[number] = issue
    b = bodymod.Body(issue["body"])
    findings = checks.run(issue, ctx)
    lines = [f"# #{number} {issue['title']}",
             f"{issue['state'].lower()} · status {issue['status']} · by {issue['author']} · opened {issue['created'][:10]} "
             f"· updated {issue['updated'][:10]} · {issue['comment_count']} comments · base_updated_at {issue['updated']}",
             f"labels: {', '.join(issue['labels']) or '-'}",
             f"assignees: {', '.join(issue['assignees']) or '-'}"]

    # assignment history (exact dates)
    try:
        tl = sync.timeline(number)
        asg = [f"{e['login']} {e['event']} {e['at'][:10]} by {e['by']}" for e in tl if e["event"] in ("assigned", "unassigned")]
        if asg:
            lines.append("assignment: " + "; ".join(asg[-4:]))
        refs = [e["ref"] for e in tl if e["event"] == "cross-referenced" and e.get("ref")]
        if refs:
            lines.append("referenced from: " + ", ".join(refs[-5:]))
    except Exception as e:
        lines.append(f"(timeline unavailable: {e})")

    lines.append("\n## OP fields" + ("" if b.form else " (legacy template)" if b.fields else " (no template fields)"))
    for fid, v in b.values().items():
        raw = (b.fields.get(fid) or {}).get("raw", "")
        shown = v or (f"(missing; raw: {raw[:80]!r})" if raw and not bodymod.is_empty(raw) else "(missing)")
        lines.append(f"- {fid}: {shown}")
    t = bodymod.clean_title(issue["title"])
    if t:
        lines.append(f"- title without prefix: {t!r}")

    lines.append("\n## Findings")
    lines += [f"- {c}: {m}" for c, m in findings] or ["- none"]

    cs = ctx.chromestatus.get(number)
    feat = None
    if cs:
        from . import chromestatus
        try:
            feat = chromestatus.feature(cs["feature_id"])
        except Exception as e:
            feat = None
            lines.append(f"(chromestatus feature unavailable: {e})")
        m = f"M{cs['start']}" + (f"–M{cs['end']}" if cs.get("end") else "") if cs.get("start") else "no milestone"
        lines.append(f"\n## chromestatus (stable M{ctx.stable})\n- {cs['name']}: {cs['stage']}, {m}")
        if feat:
            lines.append(f"- {feat['url']} · {feat['feature_type']} · spec {feat['spec']}")
            lines.append(f"- Safari view: {feat['safari']['text']} {feat['safari']['url'] or ''} · web_feature: {feat['web_feature']}"
                         f" · wpt: {', '.join(feat['wpt_dirs'][:3]) or '-'}")
            if feat["intents"]:
                lines.append(f"- intents: {', '.join(feat['intents'])}")

    ids = [b.value("web-feature"), (feat or {}).get("web_feature")]
    wf = webfeatures.match(b.value("spec-url") or (feat or {}).get("spec"), ids=[i for i in ids if i], title=b.value("spec-title") or issue["title"])
    if wf["features"] or wf["keys"]:
        lines.append("\n## web-features / BCD (matches are hints; check they're the same thing)")
        for f in wf["features"][:3]:
            sup = ", ".join(f"{k} {v}" for k, v in f["support"].items()) or "no support data"
            lines.append(f"- {f['id']} ({f['why']}): {f['name']} · baseline {f['baseline']} · {sup}"
                         + (f" · caniuse {f['caniuse'][0]}" if f["caniuse"] else ""))
        for k in wf["keys"]:
            ff = f"Firefox {k['ff']}" + (" (flag)" if k["ff_flags"] else "") if k["ff"] else "not in Firefox"
            lines.append(f"- {k['key']}: {ff}" + (f" · bugs {', '.join(k['bugs'])}" if k["bugs"] else "")
                         + (f" · {k['mdn']}" if k["mdn"] else ""))

    fill = {}
    if not b.value("webkit-standards-position") and feat and (feat["safari"]["url"] or "").startswith("https://github.com/WebKit/standards-positions/"):
        fill["webkit-standards-position"] = f"{feat['safari']['url']} (chromestatus)"
    if not b.value("web-feature"):
        if feat and feat.get("web_feature"):
            fill["web-feature"] = f"{feat['web_feature']} (chromestatus)"
        elif len(wf["features"]) == 1 and not wf["features"][0]["why"].startswith("guess"):
            fill["web-feature"] = f"{wf['features'][0]['id']} ({wf['features'][0]['why']}; check)"
    if not b.value("spec-url") and feat and feat.get("spec"):
        fill["spec-url"] = f"{feat['spec']} (chromestatus)"
    if not b.value("spec-explainer") and feat and feat.get("explainers"):
        fill["spec-explainer"] = f"{feat['explainers'][0]} (chromestatus)"
    if not b.value("caniuse-url") and wf["features"] and wf["features"][0]["caniuse"] and wf["features"][0]["why"] == "given":
        fill["caniuse-url"] = f"https://caniuse.com/{wf['features'][0]['caniuse'][0]} (web-features)"
    if not b.value("bugzilla-url"):
        bk = sorted({x for k in wf["keys"] for x in k["bugs"]}, key=int)
        bc = sorted({x for c in issue["comments"] for x in bugzilla.bugs_in(c["body"])}, key=int)
        if bc or bk:
            src = bc or bk
            fill["bugzilla-url"] = f"https://bugzilla.mozilla.org/show_bug.cgi?id={src[-1]} ({'comment' if bc else 'BCD impl_url'}; `bugs {number}` to confirm)"
    if fill:
        lines.append("\n## Fillable OP fields")
        lines += [f"- {k}: {v}" for k, v in fill.items()]

    h = hints(issue, ctx, feat, wf)
    lines.append("\n## Label hints")
    lines.append(f"- venue: {h['venue'] or '?'} · topics: {', '.join(h['topics']) or '?'} · teams: {', '.join(h['teams']) or '?'}"
                 + (f" · no team label for: {', '.join(h['team_gaps'])}" if h["team_gaps"] else "")
                 + (f" · no topic label for BCD {', '.join(h['topic_gaps'])}" if h["topic_gaps"] else ""))
    concerns = sorted(l[len("concerns: "):] for l in sync.repo_labels() if l.startswith("concerns: "))
    lines.append(f"- existing concerns: labels: {', '.join(concerns)}")
    for o in h["ownership"]:
        lines.append(f"- gecko: {o['via']} → {o.get('component') or '?'} ({o.get('team') or '?'}){' · ' + o['path'] if o.get('path') else ''}")

    bugs_c = sorted({x for c in issue["comments"] for x in bugzilla.bugs_in(c["body"])}, key=int)
    if bugs_c:
        lines.append(f"\n## Bugs linked in comments\n- {', '.join(bugs_c)}")

    lines.append("\n## People\n" + people.format(people.candidates(issue, ctx, h)))

    lines.append("\n## OP")
    lines.append(_wrap(issue["body"], OP_CAP))
    shown = issue["comments"][-COMMENTS:]
    props = checks.proposals(issue, ctx)
    older = [c for _, c, _, soft in props if not soft and c not in shown][-1:]
    for c in older:
        lines.append(f"\n## Earlier proposal\n\n### {c['author']} (Mozillian) {c['at'][:10]}")
        lines.append(_wrap(c["body"], MOZ_COMMENT_CAP))
    lines.append(f"\n## Last {len(shown)} of {issue['comment_count']} comments")
    for c in shown:
        moz = ctx.is_mozillian(c["author"])
        lines.append(f"\n### {c['author']} ({'Mozillian' if moz else 'non-Mozillian'}) {c['at'][:10]}")
        lines.append(_wrap(c["body"], MOZ_COMMENT_CAP if moz else COMMENT_CAP))

    text = "\n".join(lines)
    d = config.cache_dir() / "dossiers"
    d.mkdir(exist_ok=True)
    (d / f"{number}.md").write_text(text)
    (d / f"{number}.json").write_text(json.dumps({"hints": h, "findings": findings, "updated": issue["updated"]}))
    return text
