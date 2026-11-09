"""Triage plans: validate, show, apply. A plan is plans/N.json; the first version seen is kept as N.draft.json
so differences between what was drafted and what was applied get logged as corrections."""

import difflib
import json
import re
import shutil
import unicodedata
import urllib.parse
from datetime import datetime, timezone

from . import body as bodymod
from . import config, data, gh, sync

KEYS = {"issue", "base_updated_at", "labels_add", "labels_remove", "status", "assignees_add", "assignees_remove",
        "title", "body_fields", "comment", "close", "why"}
LIST_KEYS = ("labels_add", "labels_remove", "assignees_add", "assignees_remove")
CLOSE_REASONS = {"completed": "completed", "not_planned": "not planned"}
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
            errs.append(f"label {l!r} doesn't exist (add it to plans/labels.json via `labels`, or pick an existing one)")
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
    if plan.get("base_updated_at") and issue["updated"] > plan["base_updated_at"]:
        errs.append(f"issue changed since the dossier ({issue['updated']} > {plan['base_updated_at']}); re-run `dossier`")
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


def describe(plan, issue):
    """Compact human-readable summary plus diffs for body/title."""
    out = []
    n = plan["issue"]
    parts = []
    if plan.get("status") and plan["status"] != issue["status"]:
        parts.append(f"status {issue['status']} → {plan['status']}")
    if plan.get("labels_add"):
        parts.append("+" + ", +".join(plan["labels_add"]))
    if plan.get("labels_remove"):
        parts.append("-" + ", -".join(plan["labels_remove"]))
    if plan.get("assignees_add"):
        parts.append("assign " + ", ".join(plan["assignees_add"]))
    if plan.get("assignees_remove"):
        parts.append("unassign " + ", ".join(plan["assignees_remove"]))
    if plan.get("close"):
        parts.append(f"close ({plan['close']['reason']})")
    out.append(f"#{n} {issue['title'][:60]}\n  " + ("; ".join(parts) or "(no field changes)"))
    if plan.get("title"):
        out.append(f"  title: {issue['title']!r} → {plan['title']!r}")
    if plan.get("body_fields"):
        try:
            new = bodymod.Body(issue["body"]).with_fields(plan["body_fields"])
            diff = difflib.unified_diff(issue["body"].splitlines(), new.splitlines(), lineterm="", n=1)
            out.append("  OP diff:\n" + "\n".join("    " + l for l in list(diff)[2:]))
        except ValueError as e:
            out.append(f"  OP edit not possible: {e}")
    if plan.get("comment"):
        out.append("  comment:\n" + "\n".join("    | " + l for l in plan["comment"].splitlines()))
    for k, v in (plan.get("why") or {}).items():
        out.append(f"  why {k}: {v}")
    return "\n".join(out)


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


def apply(n, yes=False):
    plan = load(n)
    issue = sync.refresh_issue(n)
    planned_labels = []
    lp = path("labels")
    if lp.exists():
        planned_labels = [l["name"] for l in json.loads(lp.read_text()).get("create_labels", [])]
    errs, warns = validate(plan, issue, planned_labels)
    print(describe(plan, issue))
    for w in warns:
        print(f"  warning: {w}")
    if errs:
        for e in errs:
            print(f"  ERROR: {e}")
        return False
    if not yes:
        return True

    actor = gh.viewer()
    log = lambda op, **kw: _journal({"at": _now(), "actor": actor, "issue": n, "op": op, **kw})
    edit = ["issue", "edit", str(n), "-R", config.REPO]
    for l in plan.get("labels_add", []):
        edit += ["--add-label", l]
    for l in plan.get("labels_remove", []):
        edit += ["--remove-label", l]
    for a in plan.get("assignees_add", []):
        edit += ["--add-assignee", a]
    for a in plan.get("assignees_remove", []):
        edit += ["--remove-assignee", a]
    if plan.get("title"):
        edit += ["--title", plan["title"]]
    new_body = None
    if plan.get("body_fields"):
        new_body = bodymod.Body(issue["body"]).with_fields(plan["body_fields"])
        edit += ["--body-file", "-"]
    if len(edit) > 5:
        gh.run(edit, input=new_body)
        log("edit", labels_add=plan.get("labels_add"), labels_remove=plan.get("labels_remove"),
            assignees_add=plan.get("assignees_add"), title=plan.get("title"), body_fields=plan.get("body_fields"))
    if plan.get("status") and plan["status"] != issue["status"]:
        set_status(issue, plan["status"])
        log("status", status=plan["status"])
    if plan.get("comment"):
        gh.run(["issue", "comment", str(n), "-R", config.REPO, "--body-file", "-"], input=plan["comment"])
        log("comment", chars=len(plan["comment"]))
    if plan.get("close"):
        gh.run(["issue", "close", str(n), "-R", config.REPO, "--reason", CLOSE_REASONS[plan["close"]["reason"]]])
        log("close", reason=plan["close"]["reason"])

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
    sync.refresh_issue(n)
    print(f"  applied #{n}")
    return True


def set_status(issue, status):
    meta = sync.project_meta()
    item = issue["project_item"]
    if not item:
        out = gh.run(["project", "item-add", str(config.PROJECT_NUMBER), "--owner", config.OWNER, "--url",
                      f"https://github.com/{config.REPO}/issues/{issue['number']}", "--format", "json"]).stdout
        item = json.loads(out)["id"]
    gh.run(["project", "item-edit", "--id", item, "--project-id", meta["project_id"], "--field-id", meta["status_field"],
            "--single-select-option-id", meta["options"][status]])
