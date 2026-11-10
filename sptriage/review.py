"""Review queues: one markdown checklist per queue under review/. Ticking an entry approves that exact
version of the plan (the entry carries a hash of it); `apply` writes only approved entries."""

import hashlib
import json
import os
import re
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field

from . import activities, config, sync, term, untrusted
from . import plan as planmod

QUEUES = {
    "close": ("Close", "Adds labels and closes; no comment."),
    "comment": ("Comment", "Posts a comment, which notifies the people it mentions."),
    "labels": ("Labels", "Labels, status or OP edits only."),
    "decide": ("Decide", "Needs your decision first (see `ask`); edit the plan or approve as is."),
    "errors": ("Errors", "Can't be applied until the plan is fixed."),
}
NEW_LABELS = "new-labels"
HELD = {"quiet", "attention", "merge"}  # yml PR still open: nothing for `apply` to do yet
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
    parent: dict = None

    @property
    def waiting(self):
        """The yml branch this plan waits on, if it's part-way applied."""
        return activities.branch_for(self.id) if self.id.isdigit() else None

    @property
    def pr(self):
        return activities.pr_status(self.waiting) if self.waiting else None

    @property
    def pr_kind(self):
        """Where a plan waiting on its yml PR stands: "quiet" (open, nothing new, or no PR yet), "attention" (changes
        requested or new comments), "merge" (approved), "closed" (unmerged), or None (merged, unknown, not waiting)."""
        pr = self.pr if self.waiting else None
        if pr is None or pr.get("state") == "MERGED":
            return None
        if not pr:
            return "quiet"
        if pr["state"] == "CLOSED":
            return "closed"
        if pr["decision"] == "CHANGES_REQUESTED" or pr["new"]:
            return "attention"
        return "merge" if pr["decision"] == "APPROVED" else "quiet"

    @property
    def approvable(self):
        if self.queue == "errors":
            return False
        if self.id == NEW_LABELS:
            return True
        return any(not o.noop for o in planmod.ops(self.plan, self.issue, self.parent)) or bool(self.plan.get("close"))


def _after_merge(e):
    rest = [term.strip(planmod._op_text(o, e.issue)) for o in planmod.ops(e.plan, e.issue, e.parent)
            if not o.noop and o.kind != "yml"]
    return " · ".join(rest) or "finish the plan"


def waiting_note(e):
    """One line on where a plan waiting on its yml PR stands, or None if it isn't waiting."""
    if not e.waiting:
        return None
    pr, k = e.pr, e.pr_kind
    if pr is None:
        return f"waiting: yml PR from branch {e.waiting} isn't on main yet; `apply` finishes it"
    if not pr:
        return f"waiting: no PR from branch {e.waiting} yet; push it and open one"
    n, then = f"PR #{pr['number']}", _after_merge(e)
    if pr["state"] == "MERGED":
        return f"{n} is merged; `apply` will: {then}"
    if k == "closed":
        return f"{n} was closed without merging; fix the plan's yml or unapprove it"
    if k == "attention":
        bits = (["changes requested"] if pr["decision"] == "CHANGES_REQUESTED" else []) + \
               ([f"{pr['new']} new comment(s)"] if pr["new"] else [])
        return (f"{n} has {' · '.join(bits)}: address them at {pr['url']} "
                f"(or ask Claude to follow up on {config.REPO}#{pr['number']})")
    if k == "merge":
        return f"{n} is approved: merge it at {pr['url']}, then `apply` will: {then}"
    return f"waiting: {n} is open, no reviews or new comments; once it's merged, `apply` will: {then}"


def _note(e):
    color = {"closed": ("red", "bold"), "attention": ("cyan", "bold"), "merge": ("green", "bold")}.get(e.pr_kind, ("yellow",))
    return term.c(waiting_note(e), *color)


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
            plan, issue, errs, warns, parent = planmod.check(n, issues)
        except (planmod.PlanError, json.JSONDecodeError) as e:
            out.append(Entry(str(n), "errors", "", {"issue": n}, None, [str(e)]))
            continue
        out.append(Entry(str(n), planmod.queue_of(plan, issue, errs, parent), plan_hash(plan), plan, issue, errs, warns,
                         parent))
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
        head = f"[#{n}]({term.issue_url(n)}) {untrusted.md_text(e.issue['title'][:70])} · {planmod.summary(e.plan, e.issue, e.parent)}"
        detail = planmod.details(e.plan, e.issue, e.parent)
        if e.waiting:
            head += f" · **{waiting_note(e)}**"
    if changed:
        head += " · **changed since approval**"
    lines = [f"- [{'x' if ticked else ' '}] {head} <!-- plan {e.id} {e.hash} -->" if e.approvable else f"- {head}"]
    for d in detail:
        d = untrusted.term_safe(d).replace("<!--", "<\\!--")  # a quoted OP line can't fake an entry marker
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
            out.append(f"        {planmod.summary(e.plan, e.issue, e.parent)}")
            if e.waiting:
                out.append("        " + _note(e))
            if verbose:
                out += ["          " + l for l in planmod.details(e.plan, e.issue, e.parent)]
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


def apply(nums=None, dry_run=False, yes=False):
    """Write approved entries (hash must match the current plan), verify each, then refresh the queue files.
    Unless `yes`, first shows what would be written and asks."""
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
    held = []
    if any(activities.branch_for(n) for n in todo):
        for e in entries({n for n in todo if activities.branch_for(n)}, sync.load_issues()):
            if e.pr_kind in HELD:
                held.append(int(e.id))
                print(f"{term.issue(int(e.id))} {e.issue['title'][:60]}\n  {_note(e)}\n")
        todo = [n for n in todo if n not in held]
    ok, failed, waiting, yml = [], [], list(held), []
    lp = planmod.path("labels")
    labels = not nums and lp.exists() and t.get(NEW_LABELS) == plan_hash(json.loads(lp.read_text()))
    confirm = not dry_run and not yes and bool(todo or labels)
    if confirm and not _confirm(todo, labels):
        return True
    if labels:
        planmod.apply_labels_file(not dry_run)
    for n in todo:
        try:
            r = planmod.apply(n, write=not dry_run, yml_out=yml, brief=confirm)
        except (planmod.PlanError, activities.YmlError) as e:
            print(f"#{n}: {e}")
            r = "failed"
        {"done": ok, "waiting": waiting, "failed": failed}[r].append(n)
        print()
    pr = None
    if yml and not dry_run:
        try:
            pr = activities.prepare(yml)
        except activities.YmlError as e:
            print(term.c(f"yml branch not created: {e}", "red", "bold"))
    es = entries(None, sync.load_issues())
    write(es, current_approved(es))
    verb = "would apply" if dry_run else "applied"
    lines = [term.c(f"{verb} {len(ok)}", "green") + (": " + " ".join(term.issue(n) for n in ok) if ok else "")]
    if waiting:
        lines.append(term.c(f"waiting for a yml PR {len(waiting)}: " + " ".join(f"#{n}" for n in waiting), "yellow")
                     + "; once it's merged, `apply` closes them")
    if pr:
        branch, wt, cmds = pr
        lines.append(f"yml changes committed on branch {branch} (worktree {wt}). Push it and open the PR:")
        lines += ["  " + c for c in cmds]
    if failed:
        lines.append(term.c(f"failed {len(failed)}: " + " ".join(f"#{n}" for n in failed), "red", "bold"))
    if changed:
        lines.append(term.c("changed since approval, re-approve: " + " ".join(f"#{n}" for n in changed), "yellow"))
    if unapproved:
        lines.append(f"{unapproved} unapproved plan(s) left in review/")
    print("\n".join(lines))
    return not failed


def _confirm(todo, labels):
    """Show what `apply` would write, validated against fresh issues, and ask."""
    if labels:
        planmod.apply_labels_file(False)
    bad = []
    for n in todo:
        try:
            if planmod.apply(n) == "failed":
                bad.append(n)
        except (planmod.PlanError, activities.YmlError) as e:
            print(f"#{n}: {e}")
            bad.append(n)
        print()
    what = f"{len(todo)} plan(s)" + (" and new labels" if labels else "")
    if bad:
        print(term.c("won't apply: " + " ".join(f"#{n}" for n in bad), "red", "bold"))
    if not sys.stdin.isatty():
        print(f"Would write {what}; rerun with --yes to write without asking.")
        return False
    if input(f"Write {what} to GitHub? [y/N] ").strip().lower() != "y":
        print("Nothing written; run `python3 -m sptriage apply` when ready.")
        return False
    print()
    return True


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
        k = e.pr_kind
        if k in HELD:
            print(f"{term.issue(int(e.id))} {e.issue['title'][:60]}\n  {_note(e)}")
            if k == "quiet":
                i += 1
                continue
            ans = input("[o]pen PR [s]kip [q]uit ").strip().lower()[:1]
            if ans == "o":
                webbrowser.open(e.pr["url"])
                continue
            if ans == "q":
                break
            i += 1
            continue
        print(planmod.describe(e.plan, e.issue, e.errs, e.warns, parent=e.parent))
        if k == "closed":
            print("  " + _note(e))
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
    ok = current_approved(all_es)
    n = sum(1 for x in all_es if x.id in ok and x.pr_kind not in HELD)
    if n:
        print(f"\n{n} approved, including any approved earlier:\n")
        return 0 if apply() else 1  # shows them all and asks
    print("\nNothing to apply.")
    return 0
