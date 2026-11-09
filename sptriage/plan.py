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
from . import config, data, gh, sync, term

KEYS = {"issue", "base_updated_at", "labels_add", "labels_remove", "status", "assignees_add", "assignees_remove",
        "title", "body_fields", "comment", "close", "ask", "why"}
LIST_KEYS = ("labels_add", "labels_remove", "assignees_add", "assignees_remove")
CLOSE_REASONS = {"completed": "completed", "not_planned": "not planned"}
STATE_REASONS = {"completed": "COMPLETED", "not_planned": "NOT_PLANNED"}
NO_POSITION_CLOSE = ("duplicate", "invalid", "proposal withdrawn")
MENTION = re.compile(r"(?<![\w/@.`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")
LINK_HOSTS = {"bugzilla.mozilla.org", "bugzil.la", "github.com", "chromestatus.com", "webstatus.dev", "developer.mozilla.org",
              "caniuse.com", "web-platform-dx.github.io", "wpt.fyi", "searchfox.org", "groups.google.com", "lists.w3.org"}
HIDDEN = re.compile(r"[​-‏‪-‮⁠-⁤⁦-⁩﻿\U000e0000-\U000e007f]")
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
    kind: str  # label+ label- assign+ assign- title field status comment close
    arg: object
    noop: bool = False  # already true on GitHub


def ops(plan, issue):
    """The concrete writes a plan makes, in apply order, with the ones GitHub already reflects marked noop."""
    out = []
    for l in plan.get("labels_add") or []:
        out.append(Op("label+", l, l in issue["labels"]))
    for l in plan.get("labels_remove") or []:
        out.append(Op("label-", l, l not in issue["labels"]))
    for a in plan.get("assignees_add") or []:
        out.append(Op("assign+", a, a in issue["assignees"]))
    for a in plan.get("assignees_remove") or []:
        out.append(Op("assign-", a, a not in issue["assignees"]))
    if plan.get("title"):
        out.append(Op("title", plan["title"], plan["title"] == issue["title"]))
    b = bodymod.Body(issue["body"])
    for fid, v in (plan.get("body_fields") or {}).items():
        out.append(Op("field", (fid, v), b.value(fid) == expected_field(issue, fid, v)))
    if plan.get("status"):
        out.append(Op("status", plan["status"], plan["status"] == issue["status"]))
    if plan.get("comment"):
        out.append(Op("comment", plan["comment"], any((c["body"] or "").strip() == plan["comment"].strip()
                                                      for c in issue["comments"])))
    if plan.get("close"):
        out.append(Op("close", plan["close"].get("reason"), issue["state"] == "CLOSED"))
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


def queue_of(plan, issue, errs):
    """Which review queue a plan belongs in."""
    if errs:
        return "errors"
    if plan.get("ask"):
        return "decide"
    live = [o for o in ops(plan, issue) if not o.noop]
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


def validate(plan, issue, labels_planned=()):
    errs, warns = [], []
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
    if plan.get("close") and plan["close"].get("reason") not in CLOSE_REASONS:
        errs.append(f"close.reason must be one of {sorted(CLOSE_REASONS)}")
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
    texts = [plan.get("title") or "", plan.get("comment") or "", *[str(v) for v in bf.values()]]
    for t in texts:
        if HIDDEN.search(t) or any(unicodedata.category(ch) == "Co" for ch in t):
            errs.append("hidden or private-use Unicode in plan text")
            break
    for u in URL.findall(plan.get("comment") or ""):
        if not _host_ok(u):
            warns.append(f"comment links an unusual host: {u}")
    final = final_labels(plan, issue)
    positions = sorted(l for l in final if l.startswith("position: "))
    if len(positions) > 1:
        errs.append(f"more than one position label after the plan: {', '.join(positions)}"
                    + ("; remove the old one with labels_remove" if set(positions) & set(issue["labels"]) else ""))
    if plan.get("close") and not positions and not final & set(NO_POSITION_CLOSE):
        errs.append(f"closes without a position: label (or one of {', '.join(NO_POSITION_CLOSE)})")
    if plan.get("close") and plan.get("status") and plan["status"] != issue["status"]:
        warns.append("sets a status on a plan that closes")
    if plan.get("ask") and not isinstance(plan["ask"], str):
        errs.append("ask must be a string")
    if plan.get("base_updated_at") and issue["updated"] > plan["base_updated_at"]:
        errs.append(f"issue changed since the plan was drafted ({issue['updated']} > {plan['base_updated_at']}); "
                    f"run `sptriage rebase {issue['number']}`")
    if not plan.get("base_updated_at"):
        warns.append("no base_updated_at; can't detect concurrent edits")
    return errs, warns


def team_hint(login):
    try:
        role = gh.api(f"orgs/{config.OWNER}/teams/{config.CONTRIBUTOR_TEAM}/memberships/{gh.viewer()}", check=False)
    except gh.GhError:
        role = None
    if role and role.get("role") == "maintainer":
        return f"`gh api -X PUT orgs/{config.OWNER}/teams/{config.CONTRIBUTOR_TEAM}/memberships/{login}`"
    return "ask a team maintainer"


def _op_text(o, issue):
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
        t = f"status {issue['status'] or '-'} → {term.c(v, 'cyan')}"
    elif k == "comment":
        who = mentions(v)
        t = term.c("comment" + (" → " + " ".join("@" + w for w in who) if who else ""), "yellow")
    else:
        t = term.c(f"close ({v})", "bold")
    return term.c(term.strip(t) + " (already)", "dim") if o.noop else t


def summary(plan, issue):
    """One line, same order everywhere: status, labels, assignees, title, OP fields, comment, close."""
    return " · ".join(_op_text(o, issue) for o in ops(plan, issue)) or term.c("(no changes)", "dim")


def details(plan, issue):
    """Indented detail lines: title change, OP diff, comment text, ask, why."""
    out = []
    if plan.get("title"):
        out.append(f"title: {issue['title']!r} → {plan['title']!r}")
    if plan.get("body_fields"):
        try:
            new = bodymod.Body(issue["body"]).with_fields(plan["body_fields"])
            diff = list(difflib.unified_diff(issue["body"].splitlines(), new.splitlines(), lineterm="", n=0))[2:]
            out += [term.c(l, "green" if l.startswith("+") else "red" if l.startswith("-") else "dim")
                    for l in diff if not l.startswith("@@")]
        except ValueError as e:
            out.append(term.c(f"OP edit not possible: {e}", "red"))
    if plan.get("comment"):
        out += ["| " + l for l in plan["comment"].splitlines()]
    if plan.get("ask"):
        out.append(term.c(f"ask: {plan['ask']}", "yellow", "bold"))
    for k, v in (plan.get("why") or {}).items():
        out.append(term.c(f"why {k}: {v}", "dim"))
    return out


def describe(plan, issue, errs=(), warns=(), verbose=True):
    n = plan["issue"]
    lines = [f"{term.issue(n)} {issue['title'][:60]}", *term.url_lines(term.issue_url(n), "  "), "  " + summary(plan, issue)]
    if verbose:
        lines += ["    " + l for l in details(plan, issue)]
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
    """(plan, issue, errs, warns) for plan N, against the cached issue unless one is given."""
    plan = load(n)
    issue = (ctx_issues or {}).get(n) or sync.refresh_issue(n)
    planned_labels = []
    lp = path("labels")
    if lp.exists():
        planned_labels = [l["name"] for l in json.loads(lp.read_text()).get("create_labels", [])]
    errs, warns = validate(plan, issue, planned_labels)
    return plan, issue, errs, warns


def apply(n, write=False):
    """Validate against a fresh copy of the issue, run the live ops, then verify. Returns True when all is well."""
    plan, issue, errs, warns = check(n)
    print(describe(plan, issue, errs, warns))
    if errs:
        return False
    if not write:
        return True

    actor = gh.viewer()
    log = lambda op, **kw: _journal({"at": _now(), "actor": actor, "issue": n, "op": op, **kw})
    live = [o for o in ops(plan, issue) if not o.noop]
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
        new_body = bodymod.Body(issue["body"]).with_fields(fields)
        edit += ["--body-file", "-"]
    if len(edit) > 5:
        gh.run(edit, input=new_body)
        log("edit", ops=[[o.kind, o.arg] for o in live if o.kind in flags or o.kind in ("title", "field")])
    for o in live:
        if o.kind == "status":
            set_status(issue, o.arg)
            log("status", status=o.arg)
        elif o.kind == "comment":
            gh.run(["issue", "comment", str(n), "-R", config.REPO, "--body-file", "-"], input=o.arg)
            log("comment", chars=len(o.arg))
        elif o.kind == "close":
            gh.run(["issue", "close", str(n), "-R", config.REPO, "--reason", CLOSE_REASONS[o.arg]])
            log("close", reason=o.arg)

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

    after = sync.refresh_issue(n)
    results = verify(plan, issue, after, actor)
    bad = [d for ok, d in results if not ok]
    log("verify", ok=not bad, mismatches=bad)
    for ok, d in results:
        print("  " + (term.c("✓", "green") if ok else term.c("✗", "red", "bold")) + f" {d}")
    print("  " + (term.c(f"applied #{n}", "green") if not bad else term.c(f"applied #{n}, but GitHub doesn't match the plan", "red", "bold")))
    return not bad


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
        want = STATE_REASONS[plan["close"]["reason"]]
        out.append((after["state"] == "CLOSED" and after.get("state_reason") == want,
                    f"closed as {plan['close']['reason']} (is {after['state'].lower()}"
                    f"{', ' + (after.get('state_reason') or '').lower() if after['state'] == 'CLOSED' else ''})"))
    return out


def rebase(n):
    """Refresh issue N, report what changed since the plan's base, and move the base forward."""
    plan = load(n)
    try:
        cached = sync.load_issues().get(n)
    except SystemExit:
        cached = None
    issue = sync.refresh_issue(n)
    base = plan.get("base_updated_at") or ""
    lines = [f"{term.issue(n)} {issue['title'][:60]}", *term.url_lines(term.issue_url(n), "  ")]
    for c in issue["comments"]:
        if c["at"] > base:
            first = (c["body"] or "").strip().splitlines()[0][:100] if (c["body"] or "").strip() else ""
            lines.append(f"  new comment by {c['author']} {c['at'][:16]}: <untrusted>{first}</untrusted>")
    try:
        for e in sync.timeline(n, ttl=0):
            if (e.get("at") or "") > base and e["event"] in ("labeled", "unlabeled", "assigned", "unassigned", "closed", "reopened", "renamed"):
                what = e.get("label") or e.get("login") or ""
                lines.append(f"  {e['event']} {what} by {e.get('by')} {e['at'][:16]}")
    except gh.GhError as e:
        lines.append(f"  (timeline unavailable: {e})")
    if cached and cached.get("status") != issue["status"]:
        lines.append(f"  status {cached.get('status') or '-'} → {issue['status'] or '-'}")
    if len(lines) == 1:
        lines.append("  no comments or events since the base")
    plan["base_updated_at"] = issue["updated"]
    save(n, plan)
    errs, warns = validate(plan, issue)
    lines.append(describe(plan, issue, errs, warns, verbose=False))
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
