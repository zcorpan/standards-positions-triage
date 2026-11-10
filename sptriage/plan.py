"""Triage plans: validate, show, apply. A plan is plans/N.json; the first version seen is kept as N.draft.json
so differences between what was drafted and what was applied get logged as corrections."""

import difflib
import json
import re
import shutil
import unicodedata
import urllib.parse
from datetime import datetime, timezone

from dataclasses import dataclass

from . import body as bodymod
from . import activities, config, data, gh, sync, term, untrusted

KEYS = {"issue", "base_updated_at", "labels_add", "labels_remove", "status", "assignees_add", "assignees_remove",
        "title", "body_fields", "comment", "close", "ask", "why", "yml", "parent"}
# A continuation's plan resolves on the issue it continues (`parent`), e.g. a locked one, then closes as its duplicate.
PARENT_KEYS = {"issue", "base_updated_at", "labels_add", "labels_remove", "body_fields", "comment", "yml", "why"}
LIST_KEYS = ("labels_add", "labels_remove", "assignees_add", "assignees_remove")
CLOSE_REASONS = {"completed": "completed", "not_planned": "not planned", "duplicate": "duplicate"}
STATE_REASONS = {"completed": "COMPLETED", "not_planned": "NOT_PLANNED", "duplicate": "DUPLICATE"}
NO_POSITION_CLOSE = ("duplicate", "invalid", "proposal withdrawn")
MENTION = re.compile(r"(?<![\w/@.`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")
LINK_HOSTS = {"bugzilla.mozilla.org", "bugzil.la", "github.com", "chromestatus.com", "webstatus.dev", "developer.mozilla.org",
              "caniuse.com", "web-platform-dx.github.io", "wpt.fyi", "searchfox.org", "groups.google.com", "lists.w3.org"}
URL = re.compile(r"https?://[^\s)\]>]+")


class PlanError(ValueError):
    pass


def path(n, kind=""):
    return config.plans_dir() / f"{n}{kind}.json"


def load(n):
    p = path(n)
    if not p.exists():
        raise PlanError(f"no plan {p}")
    plan = json.loads(p.read_text())
    draft = path(n, ".draft")
    if not draft.exists():
        shutil.copy(p, draft)
    return plan


def pending():
    return sorted(int(p.stem) for p in config.plans_dir().glob("*.json") if p.stem.isdigit())


def save(n, plan):
    path(n).write_text(json.dumps(plan, indent=1, ensure_ascii=False) + "\n")


@dataclass
class Op:
    kind: str  # label+ label- assign+ assign- title field status comment yml close
    arg: object
    noop: bool = False  # already true on GitHub (or on activities.yml main)
    issue: int = None  # the parent's number for a continuation's parent ops, else None


def _edit_ops(sub, iss, tag=None):
    out = []
    for l in sub.get("labels_add") or []:
        out.append(Op("label+", l, l in iss["labels"], tag))
    for l in sub.get("labels_remove") or []:
        out.append(Op("label-", l, l not in iss["labels"], tag))
    for a in sub.get("assignees_add") or []:
        out.append(Op("assign+", a, a in iss["assignees"], tag))
    for a in sub.get("assignees_remove") or []:
        out.append(Op("assign-", a, a not in iss["assignees"], tag))
    if sub.get("title"):
        out.append(Op("title", sub["title"], sub["title"] == iss["title"], tag))
    b = bodymod.Body(iss["body"])
    for fid, v in (sub.get("body_fields") or {}).items():
        out.append(Op("field", (fid, v), b.value(fid) == expected_field(iss, fid, v), tag))
    if sub.get("status"):
        out.append(Op("status", sub["status"], sub["status"] == iss["status"], tag))
    if sub.get("comment"):
        out.append(Op("comment", sub["comment"], any((c["body"] or "").strip() == sub["comment"].strip()
                                                    for c in iss["comments"]), tag))
    return out


def _yml_done(n, y):
    try:
        return activities.matches(n, y)
    except Exception:
        return False


def targets(plan, issue, parent=None):
    """[(number, sub-plan, issue)] in apply order: the parent first, if any."""
    out = []
    if plan.get("parent") and parent is not None:
        out.append((plan["parent"]["issue"], plan["parent"], parent))
    out.append((plan["issue"], plan, issue))
    return out


def ops(plan, issue, parent=None):
    """The concrete writes a plan makes, in apply order (parent edits, own edits, yml, close), with the ones
    already done marked noop."""
    out = []
    ts = targets(plan, issue, parent)
    for n, sub, iss in ts:
        out += _edit_ops(sub, iss, n if n != plan["issue"] else None)
    for n, sub, _ in ts:
        if sub.get("yml"):
            out.append(Op("yml", sub["yml"], _yml_done(n, sub["yml"]), n))
    if plan.get("close"):
        out.append(Op("close", plan["close"], issue["state"] == "CLOSED"))
    return out


def expected_field(issue, fid, v):
    try:
        return bodymod.Body(bodymod.Body(issue["body"]).with_fields({fid: v})).value(fid)
    except ValueError:
        return None


def final_labels(plan, issue):
    return (set(issue["labels"]) | set(plan.get("labels_add") or [])) - set(plan.get("labels_remove") or [])


def mentions(text):
    text = re.sub(r"`[^`\n]*`", "", re.sub(r"(?s)```.*?```", "", text or ""))
    return list(dict.fromkeys(MENTION.findall(text)))


SECRETS = re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\bgh[pousr]_[A-Za-z0-9]{30,}|\bgithub_pat_\w{30,}"
                     r"|\b(?:AKIA|ASIA)[A-Z0-9]{16}\b|\bsk-[A-Za-z0-9_-]{20,}"
                     r"|(?i:\b(?:api[_-]?key|token|secret|password)\b\s*[:=]\s*\S{8,})")
LOCAL_PATH = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\|~/\.|\$HOME\b)")


def text_problems(t):
    """Outbound text (comments, OP fields, titles, yml): nothing hidden, no secrets, no local paths."""
    out = []
    if untrusted.HIDDEN.search(t) or any(unicodedata.category(ch) in ("Co", "Cc") and ch not in "\n\t" for ch in t):
        out.append("hidden, control or private-use characters")
    if SECRETS.search(t):
        out.append("looks like it contains a secret")
    if LOCAL_PATH.search(t):
        out.append("contains a local path")
    return out


def mentionable(issue):
    """Lowercased logins a comment may @-mention: Mozillians (org, assignable, people.yml) and people on the issue."""
    on_issue = {issue.get("author"), *issue.get("participants", []), *(c["author"] for c in issue.get("comments", []))}
    return {x.lower() for x in (sync.org_members() | sync.assignable() | sync.contributor_team() | set(data.people())
                                | on_issue) if x}


def queue_of(plan, issue, errs, parent=None):
    """Which review queue a plan belongs in."""
    if errs:
        return "errors"
    if plan.get("ask"):
        return "decide"
    live = [o for o in ops(plan, issue, parent) if not o.noop]
    if any(o.kind == "comment" for o in live):
        return "comment"
    if any(o.kind == "close" for o in live):
        return "close"
    return "labels"


def _host_ok(url):
    host = urllib.parse.urlsplit(url).netloc.lower().removeprefix("www.")
    if host in LINK_HOSTS or host.endswith(".spec.whatwg.org") or host.endswith(".github.io"):
        return True
    return data.spec_hint(url) is not None


def validate(plan, issue, labels_planned=(), parent=None, plan_id=None):
    errs, warns = [], []
    plan_id = plan_id or plan.get("issue")
    extra = set(plan) - KEYS
    if extra:
        errs.append(f"unknown keys: {sorted(extra)}")
    if plan.get("issue") != issue["number"]:
        errs.append("issue number mismatch")
    for k in LIST_KEYS:
        if not isinstance(plan.get(k, []), list):
            errs.append(f"{k} must be a list")
    labels = set(sync.repo_labels()) | set(labels_planned)
    for l in plan.get("labels_add", []):
        if l not in labels:
            near = difflib.get_close_matches(l, list(labels), n=3, cutoff=0.5)
            errs.append(f"label {l!r} doesn't exist" + (f"; closest: {', '.join(near)}" if near else "")
                        + " (or add it to plans/labels.json via `labels`)")
    for l in plan.get("labels_remove", []):
        if l not in issue["labels"]:
            warns.append(f"label {l!r} isn't on the issue")
    if plan.get("status") and plan["status"] not in config.STATUSES:
        errs.append(f"status {plan['status']!r} not one of {config.STATUSES}")
    assignable = sync.assignable()
    for a in plan.get("assignees_add", []):
        if a not in assignable:
            errs.append(f"{a} isn't assignable: ping them in the comment instead, and suggest adding them to "
                        f"{config.OWNER}/{config.CONTRIBUTOR_TEAM} ({team_hint(a)})")
    close = plan.get("close") or {}
    if plan.get("close") and close.get("reason") not in CLOSE_REASONS:
        errs.append(f"close.reason must be one of {sorted(CLOSE_REASONS)}")
    if close.get("reason") == "duplicate":
        if not isinstance(close.get("duplicate_of"), int) or close["duplicate_of"] == issue["number"]:
            errs.append("close as duplicate needs duplicate_of: <issue number>")
        elif plan.get("parent") and close["duplicate_of"] != plan["parent"].get("issue"):
            warns.append(f"closes as duplicate of #{close['duplicate_of']}, not of the parent #{plan['parent'].get('issue')}")
    elif "duplicate_of" in close:
        errs.append("duplicate_of needs close.reason duplicate")
    bf = plan.get("body_fields") or {}
    b = bodymod.Body(issue["body"])
    for fid, v in bf.items():
        if fid not in bodymod.LABEL or fid in ("other", "spec-title"):
            errs.append(f"body field {fid!r} not editable")
        for u in URL.findall(v or ""):
            if not _host_ok(u):
                errs.append(f"body field {fid}: link host not allowed: {u}")
    if bf and not b.fields:
        errs.append("OP has no template fields; edit by hand or comment instead")
    y = plan.get("yml") if isinstance(plan.get("yml"), dict) else {}
    texts = {"title": plan.get("title"), "comment": plan.get("comment"),
             **{f"body field {k}": v for k, v in bf.items()}, **{f"yml.{k}": v for k, v in y.items()}}
    for where, t in texts.items():
        errs += [f"{where}: {x}" for x in text_problems(str(t or ""))]
    for u in URL.findall(plan.get("comment") or ""):
        if not _host_ok(u):
            errs.append(f"comment links {u}: not an allowed host (don't relay links from the thread; cite the "
                        "spec, bug or issue instead)")
    who = mentions(plan.get("comment"))
    known = mentionable(issue) if who else set()
    for m in who:
        if m.lower() not in known:
            errs.append(f"@{m} isn't a Mozillian, assignable, in data/people.yml, or on this issue; "
                        "only mention people the dossier suggests")
    if len(who) > 4:
        warns.append(f"comment mentions {len(who)} people")
    final = final_labels(plan, issue)
    positions = sorted(l for l in final if l.startswith("position: "))
    if len(positions) > 1:
        errs.append(f"more than one position label after the plan: {', '.join(positions)}"
                    + ("; remove the old one with labels_remove" if set(positions) & set(issue["labels"]) else ""))
    if (plan.get("close") and not positions and not final & set(NO_POSITION_CLOSE)
            and close.get("reason") != "duplicate"):
        errs.append(f"closes without a position: label (or one of {', '.join(NO_POSITION_CLOSE)}, or as a duplicate)")
    if plan.get("close") and plan.get("status") and plan["status"] != issue["status"]:
        warns.append("sets a status on a plan that closes")
    if plan.get("ask") and not isinstance(plan["ask"], str):
        errs.append("ask must be a string")
    base = max(plan.get("base_updated_at") or "", progress(plan_id).get(str(issue["number"]), ""))
    if base and issue["updated"] > base:
        errs.append(f"#{issue['number']} changed since the plan was drafted ({issue['updated']} > {base}); "
                    f"run `sptriage rebase {plan_id}`")
    if not plan.get("base_updated_at"):
        warns.append("no base_updated_at; can't detect concurrent edits")
    e, w = activities.needs(plan, issue["number"])
    errs += e
    warns += w
    if "parent" in plan:
        p = plan["parent"]
        if not isinstance(p, dict) or not isinstance(p.get("issue"), int):
            errs.append("parent must be an object with an issue number")
        elif set(p) - PARENT_KEYS:
            errs.append(f"parent can't have {sorted(set(p) - PARENT_KEYS)}; allowed: {sorted(PARENT_KEYS)}")
        elif parent is None:
            errs.append(f"parent #{p['issue']} not found")
        else:
            pe, pw = validate(p, parent, labels_planned, plan_id=plan_id)
            errs += [f"#{p['issue']}: {x}" for x in pe]
            warns += [f"#{p['issue']}: {x}" for x in pw]
    return errs, warns


def progress(plan_id):
    """{issue number: updated_at after our own writes} for a plan that's part-way applied (waiting on a yml PR),
    so our own edits don't count as concurrent ones."""
    p = config.state_dir() / "progress.json"
    return (json.loads(p.read_text()) if p.exists() else {}).get(str(plan_id), {})


def _set_progress(plan_id, n=None, updated=None, clear=False):
    p = config.state_dir() / "progress.json"
    st = json.loads(p.read_text()) if p.exists() else {}
    if clear:
        st.pop(str(plan_id), None)
    else:
        st.setdefault(str(plan_id), {})[str(n)] = updated
    p.write_text(json.dumps(st, indent=1) + "\n")


def team_hint(login):
    try:
        role = gh.api(f"orgs/{config.OWNER}/teams/{config.CONTRIBUTOR_TEAM}/memberships/{gh.viewer()}", check=False)
    except gh.GhError:
        role = None
    if role and role.get("role") == "maintainer":
        return f"`gh api -X PUT orgs/{config.OWNER}/teams/{config.CONTRIBUTOR_TEAM}/memberships/{login}`"
    return "ask a team maintainer"


def _op_text(o, iss):
    k, v = o.kind, o.arg
    if k in ("label+", "label-"):
        t = term.label(v, k[-1])
    elif k == "assign+":
        t = term.c(f"assign {v}", "cyan")
    elif k == "assign-":
        t = term.c(f"unassign {v}", "red")
    elif k == "title":
        t = "title"
    elif k == "field":
        t = f"OP {v[0]}"
    elif k == "status":
        t = f"status {iss['status'] or '-'} → {term.c(v, 'cyan')}"
    elif k == "comment":
        who = mentions(v)
        t = term.c("comment" + (" → " + " ".join("@" + w for w in who) if who else ""), "yellow")
    elif k == "yml":
        parts = ([term.label(f"position: {v['position']}")] if v.get("position") else []) + \
                [f for f in ("rationale", "description") if f in v]
        t = term.c("yml PR: ", "cyan") + ", ".join(parts)
    else:
        r = v.get("reason")
        t = term.c(f"close as duplicate of #{v.get('duplicate_of')}" if r == "duplicate" else f"close ({r})", "bold")
    return term.c(term.strip(t) + " (already)", "dim") if o.noop else t


def summary(plan, issue, parent=None):
    """One line, same order everywhere: parent edits, labels, assignees, title, OP fields, status, comment, yml, close.
    With a parent, each run of ops on one issue starts with its number."""
    by = {n: iss for n, _, iss in targets(plan, issue, parent)}
    all_ops = ops(plan, issue, parent)
    multi = any(o.issue for o in all_ops if o.kind != "yml")
    out, last = [], None
    for o in all_ops:
        t = _op_text(o, by.get(o.issue or plan["issue"], issue))
        if multi and o.kind != "yml" and (o.issue or plan["issue"]) != last:
            last = o.issue or plan["issue"]
            t = term.c(f"#{last}:", "bold") + " " + t
        out.append(t)
    return " · ".join(out) or term.c("(no changes)", "dim")


def details(plan, issue, parent=None):
    """Indented detail lines: title change, OP diff, comment text, yml text, ask, why (parent first)."""
    out = []
    for n, sub, iss in targets(plan, issue, parent):
        mine = []
        if sub.get("title"):
            mine.append(f"title: {iss['title']!r} → {sub['title']!r}")
        if sub.get("body_fields"):
            try:
                new = bodymod.Body(iss["body"]).with_fields(sub["body_fields"])
                diff = list(difflib.unified_diff(iss["body"].splitlines(), new.splitlines(), lineterm="", n=0))[2:]
                mine += [term.c(l, "green" if l.startswith("+") else "red" if l.startswith("-") else "dim")
                         for l in diff if not l.startswith("@@")]
            except ValueError as e:
                mine.append(term.c(f"OP edit not possible: {e}", "red"))
        if sub.get("comment"):
            mine += ["| " + l for l in sub["comment"].splitlines()]
        for f in ("rationale", "description"):
            if (sub.get("yml") or {}).get(f):
                key, e = _yml_entry(n)
                old = (e or {}).get(f)
                mine.append(f"yml {f} ({key or 'new entry'}):")
                if old:
                    mine += [term.c("- " + l, "red") for l in old.strip().splitlines()]
                mine += [term.c("+ " + l, "green") for l in sub["yml"][f].strip().splitlines()]
        if mine and n != plan["issue"]:
            out.append(term.c(f"#{n} {iss['title'][:60]}:", "bold"))
        elif mine and parent is not None and plan.get("parent"):
            out.append(term.c(f"#{n}:", "bold"))
        out += mine
    if plan.get("ask"):
        out.append(term.c(f"ask: {plan['ask']}", "yellow", "bold"))
    for sub in ([plan["parent"]] if plan.get("parent") else []) + [plan]:
        for k, v in (sub.get("why") or {}).items():
            out.append(term.c(f"why {k}: {v}", "dim"))
    return out


def _yml_entry(n):
    try:
        return activities.entry(n)
    except Exception:
        return None, None


def describe(plan, issue, errs=(), warns=(), verbose=True, parent=None):
    n = plan["issue"]
    lines = [f"{term.issue(n)} {issue['title'][:60]}", *term.url_lines(term.issue_url(n), "  "),
             "  " + summary(plan, issue, parent)]
    if verbose:
        lines += ["    " + l for l in details(plan, issue, parent)]
    lines += ["  " + term.c(f"warning: {w}", "yellow") for w in warns]
    lines += ["  " + term.c(f"ERROR: {e}", "red", "bold") for e in errs]
    return "\n".join(lines)


def _journal(entry):
    with open(config.state_dir() / "journal.jsonl", "a") as f:
        f.write(json.dumps(entry) + "\n")


def _corrections(n, final, ctx_hints):
    draft_p = path(n, ".draft")
    if not draft_p.exists():
        return
    draft = json.loads(draft_p.read_text())
    diffs = []
    for k in LIST_KEYS:
        a, b = set(draft.get(k, [])), set(final.get(k, []))
        for x in sorted(a - b):
            diffs.append({"field": k, "dropped": x})
        for x in sorted(b - a):
            diffs.append({"field": k, "added": x})
    for k in ("status", "title"):
        if draft.get(k) != final.get(k):
            diffs.append({"field": k, "draft": draft.get(k), "final": final.get(k)})
    if (draft.get("close") or {}).get("reason") != (final.get("close") or {}).get("reason"):
        diffs.append({"field": "close", "draft": draft.get("close"), "final": final.get("close")})
    for fid in set(draft.get("body_fields") or {}) | set(final.get("body_fields") or {}):
        if (draft.get("body_fields") or {}).get(fid) != (final.get("body_fields") or {}).get(fid):
            diffs.append({"field": f"body_fields.{fid}", "draft": (draft.get("body_fields") or {}).get(fid),
                          "final": (final.get("body_fields") or {}).get(fid)})
    dc, fc = draft.get("comment") or "", final.get("comment") or ""
    if dc != fc:
        ratio = difflib.SequenceMatcher(None, dc, fc).ratio()
        diffs.append({"field": "comment", "similarity": round(ratio, 2), "dropped": not fc, "added": not dc})
    if diffs:
        with open(config.state_dir() / "corrections.jsonl", "a") as f:
            f.write(json.dumps({"issue": n, "at": _now(), "context": ctx_hints, "diffs": diffs}) + "\n")


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def apply_labels_file(yes):
    p = path("labels")
    if not p.exists():
        return []
    spec = json.loads(p.read_text())
    existing = sync.repo_labels()
    todo = [l for l in spec.get("create_labels", []) if l["name"] not in existing]
    for l in todo:
        print(f"create label {l['name']!r} ({l.get('color', 'ededed')}): {l.get('description', '')}")
        if yes:
            gh.run(["label", "create", l["name"], "-R", config.REPO, "--color", l.get("color", "ededed"),
                    "--description", l.get("description", "")])
            _journal({"at": _now(), "actor": gh.viewer(), "op": "label-create", "label": l["name"]})
    if yes and todo:
        from .http import invalidate
        invalidate("labels")
    if yes:
        p.rename(config.plans_dir() / f"labels-applied-{_now().replace(':', '')}.json")
    return [l["name"] for l in todo]


def check(n, ctx_issues=None):
    """(plan, issue, errs, warns, parent) for plan N, against the cached issues unless given."""
    plan = load(n)
    issue = (ctx_issues or {}).get(n) or sync.refresh_issue(n)
    parent = None
    pn = (plan.get("parent") or {}).get("issue") if isinstance(plan.get("parent"), dict) else None
    if isinstance(pn, int):
        try:
            parent = (ctx_issues or {}).get(pn) or sync.refresh_issue(pn)
        except gh.GhError:
            parent = None
    planned_labels = []
    lp = path("labels")
    if lp.exists():
        planned_labels = [l["name"] for l in json.loads(lp.read_text()).get("create_labels", [])]
    errs, warns = validate(plan, issue, planned_labels, parent)
    return plan, issue, errs, warns, parent


def _write_edits(n, iss, live, log):
    edit = ["issue", "edit", str(n), "-R", config.REPO]
    flags = {"label+": "--add-label", "label-": "--remove-label", "assign+": "--add-assignee", "assign-": "--remove-assignee"}
    fields = {}
    for o in live:
        if o.kind in flags:
            edit += [flags[o.kind], o.arg]
        elif o.kind == "title":
            edit += ["--title", o.arg]
        elif o.kind == "field":
            fields[o.arg[0]] = o.arg[1]
    new_body = None
    if fields:
        new_body = bodymod.Body(iss["body"]).with_fields(fields)
        edit += ["--body-file", "-"]
    if len(edit) > 5:
        gh.run(edit, input=new_body)
        log("edit", issue=n, ops=[[o.kind, o.arg] for o in live if o.kind in flags or o.kind in ("title", "field")])
    for o in live:
        if o.kind == "status":
            set_status(iss, o.arg)
            log("status", issue=n, status=o.arg)
        elif o.kind == "comment":
            gh.run(["issue", "comment", str(n), "-R", config.REPO, "--body-file", "-"], input=o.arg)
            log("comment", issue=n, chars=len(o.arg))


def _report(results, prefix=""):
    for ok, d in results:
        print("  " + (term.c("✓", "green") if ok else term.c("✗", "red", "bold")) + f" {prefix}{d}")
    return [d for ok, d in results if not ok]


def apply(n, write=False, yml_out=None, brief=False):
    """Validate against fresh copies of the issues, then in phases: GitHub edits (parent first) and verify; the yml
    change (queued in `yml_out` for one branch per run, or waiting for its PR); the close and verify.
    `brief` prints only the plan's first line, for when the full plan was just shown.
    Returns "done", "waiting" (yml not on main yet) or "failed"."""
    plan, issue, errs, warns, parent = check(n)
    if brief:
        print("\n".join([f"{term.issue(n)} {issue['title'][:60]}" if issue else f"#{n}"]
                        + ["  " + term.c(f"ERROR: {e}", "red", "bold") for e in errs]))
    else:
        print(describe(plan, issue, errs, warns, parent=parent))
    if errs:
        return "failed"
    if not write:
        return "done"

    actor = gh.viewer()
    log = lambda op, **kw: _journal({"at": _now(), "actor": actor, "plan": n, "op": op, **{"issue": n, **kw}})
    bad = []
    for tn, sub, iss in targets(plan, issue, parent):
        live = [o for o in _edit_ops(sub, iss) if not o.noop]
        if not live:
            continue
        _write_edits(tn, iss, live, log)
        after = sync.refresh_issue(tn)
        if tn == n:
            issue = after
        _set_progress(n, tn, after["updated"])
        edits = {k: v for k, v in sub.items() if k not in ("close", "yml", "parent")}
        miss = _report(verify(edits, iss, after, actor), f"#{tn} " if tn != n else "")
        log("verify", issue=tn, ok=not miss, mismatches=miss)
        bad += miss
    if bad:
        print("  " + term.c(f"#{n}: GitHub doesn't match the plan; stopped before the yml and close", "red", "bold"))
        return "failed"

    want = [(tn, sub["yml"], iss) for tn, sub, iss in targets(plan, issue, parent) if sub.get("yml")]
    if want:
        activities.fetch()
        missing = [(tn, y, iss) for tn, y, iss in want if not activities.matches(tn, y)]
        for tn, y, _ in want:
            if (tn, y) not in [(m[0], m[1]) for m in missing]:
                print("  " + term.c("✓", "green") + f" activities.yml on main has #{tn}'s change")
        if missing:
            branch = activities.branch_for(n)
            if branch:
                print("  " + term.c(f"waiting: yml PR from branch {branch} isn't on main yet", "yellow"))
            elif yml_out is not None:
                yml_out += [(n, tn, y, bodymod.clean_title(iss["title"]) or iss["title"], n) for tn, y, iss in missing]
            return "waiting"

    if plan.get("close") and issue["state"] != "CLOSED":
        c = plan["close"]
        args = ["issue", "close", str(n), "-R", config.REPO]
        args += ["--duplicate-of", str(c["duplicate_of"])] if c["reason"] == "duplicate" else ["--reason", CLOSE_REASONS[c["reason"]]]
        gh.run(args)
        log("close", reason=c["reason"], duplicate_of=c.get("duplicate_of"))
        after = sync.refresh_issue(n)
        miss = _report(verify({"close": c}, issue, after, actor))
        log("verify", ok=not miss, mismatches=miss)
        bad += miss

    hints_p = config.cache_dir() / "dossiers" / f"{n}.json"
    ctx_hints = json.loads(hints_p.read_text())["hints"] if hints_p.exists() else {}
    ctx_hints = {k: ctx_hints.get(k) for k in ("venue", "topics", "teams", "team_gaps", "why")}
    _corrections(n, plan, ctx_hints)
    done = config.plans_dir() / "applied"
    done.mkdir(exist_ok=True)
    stamp = _now().replace(":", "")
    path(n).rename(done / f"{n}-{stamp}.json")
    if path(n, ".draft").exists():
        path(n, ".draft").rename(done / f"{n}-{stamp}.draft.json")
    _set_progress(n, clear=True)
    activities.done(n)
    print("  " + (term.c(f"applied #{n}", "green") if not bad else term.c(f"applied #{n}, but GitHub doesn't match the plan", "red", "bold")))
    return "failed" if bad else "done"


def verify(plan, before, after, actor):
    """[(ok, description)] comparing the issue after applying with what the plan asked for."""
    out = []
    for l in plan.get("labels_add") or []:
        out.append((l in after["labels"], f"label {l}"))
    for l in plan.get("labels_remove") or []:
        out.append((l not in after["labels"], f"no label {l}"))
    for a in plan.get("assignees_add") or []:
        out.append((a in after["assignees"], f"assigned {a}"))
    for a in plan.get("assignees_remove") or []:
        out.append((a not in after["assignees"], f"not assigned {a}"))
    if plan.get("title"):
        out.append((after["title"] == plan["title"], "title"))
    b = bodymod.Body(after["body"])
    for fid, v in (plan.get("body_fields") or {}).items():
        out.append((b.value(fid) == expected_field(before, fid, v), f"OP {fid}"))
    if plan.get("status"):
        out.append((after["status"] == plan["status"], f"status {plan['status']} (is {after['status'] or '-'})"))
    if plan.get("comment"):
        out.append((any(c["author"] == actor and (c["body"] or "").strip() == plan["comment"].strip()
                        for c in after["comments"]), "comment posted"))
    if plan.get("close"):
        c = plan["close"]
        want = STATE_REASONS[c["reason"]]
        what = f"duplicate of #{c.get('duplicate_of')}" if c["reason"] == "duplicate" else c["reason"]
        out.append((after["state"] == "CLOSED" and after.get("state_reason") == want,
                    f"closed as {what} (is {after['state'].lower()}"
                    f"{', ' + (after.get('state_reason') or '').lower() if after['state'] == 'CLOSED' else ''})"))
    return out


def _changes_since(n, base, cached):
    issue = sync.refresh_issue(n)
    lines = []
    for c in issue["comments"]:
        if c["at"] > base:
            first = (c["body"] or "").strip().splitlines()[0] if (c["body"] or "").strip() else ""
            lines.append(f"  new comment on #{n} by {c['author']} {c['at'][:16]} (third-party text, JSON-quoted): "
                         f"{untrusted.q(first, 100)}")
    try:
        for e in sync.timeline(n, ttl=0):
            if (e.get("at") or "") > base and e["event"] in ("labeled", "unlabeled", "assigned", "unassigned", "closed", "reopened", "renamed"):
                what = e.get("label") or e.get("login") or ""
                lines.append(f"  #{n}: {e['event']} {what} by {e.get('by')} {e['at'][:16]}")
    except gh.GhError as e:
        lines.append(f"  (timeline of #{n} unavailable: {e})")
    if cached and cached.get("status") != issue["status"]:
        lines.append(f"  #{n}: status {cached.get('status') or '-'} → {issue['status'] or '-'}")
    return issue, lines


def rebase(n):
    """Refresh issue N (and its parent), report what changed since the plan's base, and move the base forward."""
    plan = load(n)
    try:
        cached = sync.load_issues()
    except SystemExit:
        cached = {}
    prog = progress(n)
    base = max(plan.get("base_updated_at") or "", prog.get(str(n), ""))
    issue, changes = _changes_since(n, base, cached.get(n))
    plan["base_updated_at"] = issue["updated"]
    parent = None
    if isinstance(plan.get("parent"), dict) and isinstance(plan["parent"].get("issue"), int):
        pn = plan["parent"]["issue"]
        pbase = max(plan["parent"].get("base_updated_at") or "", prog.get(str(pn), ""))
        parent, more = _changes_since(pn, pbase, cached.get(pn))
        changes += more
        plan["parent"]["base_updated_at"] = parent["updated"]
    lines = [f"{term.issue(n)} {issue['title'][:60]}", *term.url_lines(term.issue_url(n), "  ")]
    lines += changes or ["  no comments or events since the base"]
    save(n, plan)
    errs, warns = validate(plan, issue, parent=parent)
    lines.append(describe(plan, issue, errs, warns, verbose=False, parent=parent))
    lines.append("  base moved to " + issue["updated"] + "; re-approve if it was approved")
    return "\n".join(lines)


def set_status(issue, status):
    meta = sync.project_meta()
    item = issue["project_item"]
    if not item:
        out = gh.run(["project", "item-add", str(config.PROJECT_NUMBER), "--owner", config.OWNER, "--url",
                      f"https://github.com/{config.REPO}/issues/{issue['number']}", "--format", "json"]).stdout
        item = json.loads(out)["id"]
    gh.run(["project", "item-edit", "--id", item, "--project-id", meta["project_id"], "--field-id", meta["status_field"],
            "--single-select-option-id", meta["options"][status]])
