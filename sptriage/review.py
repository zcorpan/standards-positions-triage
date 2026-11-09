"""Review queues: one markdown checklist per queue under review/. Ticking an entry approves that exact
version of the plan (the entry carries a hash of it); `apply` writes only approved entries."""

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from . import config, sync, term
from . import plan as planmod

QUEUES = {
    "close": ("Close", "Adds labels and closes; no comment."),
    "comment": ("Comment", "Posts a comment, which notifies the people it mentions."),
    "labels": ("Labels", "Labels, status or OP edits only."),
    "decide": ("Decide", "Needs your decision first (see `ask`); edit the plan or approve as is."),
    "errors": ("Errors", "Can't be applied until the plan is fixed."),
}
NEW_LABELS = "new-labels"
ENTRY = re.compile(r"^- \[([ xX])\] .*<!-- plan (\S+) (\w+) -->\s*$")


@dataclass
class Entry:
    id: str
    queue: str
    hash: str
    plan: dict = field(default_factory=dict)
    issue: dict = None
    errs: list = field(default_factory=list)
    warns: list = field(default_factory=list)

    @property
    def approvable(self):
        if self.queue == "errors":
            return False
        if self.id == NEW_LABELS:
            return True
        return any(not o.noop for o in planmod.ops(self.plan, self.issue)) or bool(self.plan.get("close"))


def plan_hash(obj):
    d = {k: v for k, v in obj.items() if k != "why"} if isinstance(obj, dict) else obj
    return hashlib.sha256(json.dumps(d, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:10]


def ticks():
    """{id: hash} for every ticked entry in the queue files."""
    out = {}
    for f in config.review_dir().glob("*.md"):
        for line in f.read_text().splitlines():
            m = ENTRY.match(line)
            if m and m.group(1) in "xX":
                out[m.group(2)] = m.group(3)
    return out


def entries(nums=None, issues=None):
    out = []
    for n in planmod.pending():
        if nums and n not in nums:
            continue
        try:
            plan, issue, errs, warns = planmod.check(n, issues)
        except (planmod.PlanError, json.JSONDecodeError) as e:
            out.append(Entry(str(n), "errors", "", {"issue": n}, None, [str(e)]))
            continue
        out.append(Entry(str(n), planmod.queue_of(plan, issue, errs), plan_hash(plan), plan, issue, errs, warns))
    lp = planmod.path("labels")
    if lp.exists() and not nums:
        spec = json.loads(lp.read_text())
        out.append(Entry(NEW_LABELS, "labels", plan_hash(spec), spec))
    return out


def _entry_md(e, ticked, changed):
    if e.id == NEW_LABELS:
        names = ", ".join(l["name"] for l in e.plan.get("create_labels", []))
        head = f"New labels from plans/labels.json: {names}"
        detail = [f"{l['name']}: {l.get('description', '')}" for l in e.plan.get("create_labels", [])]
    elif e.issue is None:
        head, detail = f"#{e.id} (plan unreadable)", []
    else:
        n = e.plan["issue"]
        head = f"[#{n}]({term.issue_url(n)}) {e.issue['title'][:70]} · {planmod.summary(e.plan, e.issue)}"
        detail = planmod.details(e.plan, e.issue)
    if changed:
        head += " · **changed since approval**"
    lines = [f"- [{'x' if ticked else ' '}] {head} <!-- plan {e.id} {e.hash} -->" if e.approvable else f"- {head}"]
    for d in detail:
        lines.append(("  > " + d[2:]) if d.startswith("| ") else f"  - {d}")
    lines += [f"  - warning: {w}" for w in e.warns]
    lines += [f"  - **ERROR: {x}**" for x in e.errs]
    return lines


def write(es, approved):
    """Rewrite all queue files. `approved` = ids to tick; ticks on changed plans are dropped and flagged."""
    old = ticks()
    d = config.review_dir()
    with term.plain():
        for q, (title, blurb) in QUEUES.items():
            mine = [e for e in es if e.queue == q]
            lines = [f"# {title} ({len(mine)})", "", blurb + " Tick `[x]` to approve, then run `python3 -m sptriage apply`.", ""]
            for e in mine:
                tick = e.id in approved and e.approvable
                lines += _entry_md(e, tick, changed=e.id in old and old[e.id] != e.hash and not tick)
            if not mine:
                lines.append("Nothing here.")
            (d / f"{q}.md").write_text("\n".join(lines) + "\n")


def current_approved(es):
    t = ticks()
    return {e.id for e in es if t.get(e.id) == e.hash}


def _issues():
    sync.sync_issues(log=lambda *_: None)
    return sync.load_issues()


def show(nums=None, verbose=False):
    all_es = entries(None, _issues())
    approved = current_approved(all_es)
    write(all_es, approved)
    es = [e for e in all_es if not nums or (e.id.isdigit() and int(e.id) in nums)]
    out = []
    for q, (title, _) in QUEUES.items():
        mine = [e for e in es if e.queue == q]
        if not mine:
            continue
        out.append(term.c(f"{title} ({len(mine)})", "bold") + term.c(f"  review/{q}.md", "dim"))
        for e in mine:
            box = term.c("[x]", "green") if e.id in approved else "[ ]" if e.approvable else "   "
            if e.id == NEW_LABELS:
                out.append(f"  {box} new labels: " + ", ".join(l["name"] for l in e.plan.get("create_labels", [])))
                continue
            if e.issue is None:
                out.append(f"  {box} #{e.id}: " + "; ".join(e.errs))
                continue
            out.append(f"  {box} {term.issue(int(e.id), 5)} {e.issue['title'][:60]}")
            out += term.url_lines(term.issue_url(int(e.id)), "        ")
            out.append(f"        {planmod.summary(e.plan, e.issue)}")
            if verbose:
                out += ["          " + l for l in planmod.details(e.plan, e.issue)]
            out += ["        " + term.c(f"warning: {w}", "yellow") for w in e.warns]
            out += ["        " + term.c(f"ERROR: {x}", "red", "bold") for x in e.errs]
    if not out:
        return "no plans in " + str(config.plans_dir())
    n_ok = len(approved & {e.id for e in es})
    out.append(f"\n{n_ok} of {len(es)} approved. Tick entries in review/*.md (or `approve <queue|N…>`), then `apply`.")
    return "\n".join(out)


def approve(targets, on=True):
    es = entries(None, _issues())
    approved = current_approved(es)
    picked, refused = set(), []
    for t in targets:
        match = [e for e in es if t in (e.queue, e.id, "all")]
        if not match:
            refused.append(f"{t}: no such queue or pending plan")
        for e in match:
            if on and not e.approvable:
                if t not in ("all",) and t not in QUEUES:
                    refused.append(f"#{e.id}: {'has errors' if e.errs else 'nothing to apply'}")
                continue
            picked.add(e.id)
    approved = approved | picked if on else approved - picked
    write(es, approved)
    verb = "approved" if on else "unapproved"
    lines = [f"{verb} {len(picked)}: " + (", ".join("#" + i if i.isdigit() else i for i in sorted(picked, key=_key)) or "-")]
    lines += [term.c(f"skipped {r}", "yellow") for r in refused]
    lines.append(f"{len(approved)} approved in total; run `python3 -m sptriage apply` to write them.")
    return "\n".join(lines)


def _key(i):
    return (0, int(i)) if i.isdigit() else (1, i)


def apply(nums=None, dry_run=False):
    """Write approved entries (hash must match the current plan), verify each, then refresh the queue files."""
    t = ticks()
    todo, changed, unapproved = [], [], 0
    for n in planmod.pending():
        if nums and n not in nums:
            continue
        try:
            h = plan_hash(planmod.load(n))
        except (planmod.PlanError, json.JSONDecodeError):
            unapproved += 1
            continue
        if t.get(str(n)) == h:
            todo.append(n)
        elif str(n) in t:
            changed.append(n)
        else:
            unapproved += 1
    ok, failed = [], []
    lp = planmod.path("labels")
    if not nums and lp.exists() and t.get(NEW_LABELS) == plan_hash(json.loads(lp.read_text())):
        planmod.apply_labels_file(not dry_run)
    for n in todo:
        try:
            good = planmod.apply(n, write=not dry_run)
        except planmod.PlanError as e:
            print(f"#{n}: {e}")
            good = False
        (ok if good else failed).append(n)
        print()
    es = entries(None, sync.load_issues())
    write(es, current_approved(es))
    verb = "would apply" if dry_run else "applied"
    lines = [term.c(f"{verb} {len(ok)}", "green") + (": " + " ".join(term.issue(n) for n in ok) if ok else "")]
    if failed:
        lines.append(term.c(f"failed {len(failed)}: " + " ".join(f"#{n}" for n in failed), "red", "bold"))
    if changed:
        lines.append(term.c("changed since approval, re-approve: " + " ".join(f"#{n}" for n in changed), "yellow"))
    if unapproved:
        lines.append(f"{unapproved} unapproved plan(s) left in review/")
    print("\n".join(lines))
    return not failed


def interactive(nums=None):
    """Step through plans in a terminal: approve, skip, edit, quit; then offer to apply."""
    if not sys.stdin.isatty():
        print("`review` needs a terminal; otherwise tick entries in review/*.md or use `approve`.")
        return 1
    es = [e for e in entries(nums, _issues()) if e.id != NEW_LABELS]
    approved = current_approved(entries(None, sync.load_issues()))
    i = 0
    while i < len(es):
        e = es[i]
        print()
        if e.issue is None:
            print(f"#{e.id}: " + "; ".join(e.errs))
            i += 1
            continue
        print(term.c(QUEUES[e.queue][0], "bold") + f" ({i + 1}/{len(es)})" + (term.c("  approved", "green") if e.id in approved else ""))
        print(planmod.describe(e.plan, e.issue, e.errs, e.warns))
        choices = ("[a]pprove " if e.approvable else "") + "[u]napprove [s]kip [e]dit [q]uit"
        ans = input(choices + " ").strip().lower()[:1]
        if ans == "e":
            subprocess.run([os.environ.get("EDITOR", "vi"), str(planmod.path(int(e.id)))])
            approved.discard(e.id)
            es[i] = entries({int(e.id)}, sync.load_issues())[0]
            continue
        if ans == "q":
            break
        if ans == "a" and e.approvable:
            approved.add(e.id)
        elif ans == "u":
            approved.discard(e.id)
        i += 1
    all_es = entries(None, sync.load_issues())
    write(all_es, approved & {x.id for x in all_es})
    n = len(current_approved(all_es))
    if n and input(f"{n} approved. Apply now? [y/N] ").strip().lower() == "y":
        return 0 if apply() else 1
    print(f"{n} approved; run `python3 -m sptriage apply` when ready.")
    return 0
