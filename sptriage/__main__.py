"""sptriage: triage helper for mozilla/standards-positions. Run `python3 -m sptriage <command> -h`."""

import argparse
import sys

from . import config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sptriage", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sync", help="fetch issues (incremental) into .cache/")
    s.add_argument("--full", action="store_true", help="refetch everything, including closed issues")

    s = sub.add_parser("queue", help="ranked table of issues to look at")
    s.add_argument("--status", help="project status, e.g. Unscreened")
    s.add_argument("--check", help="only issues with this finding, e.g. ready-close, missing-bug, stalled")
    s.add_argument("--closed", action="store_true", help="include closed issues (e.g. with --check missing-bug)")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("-v", action="store_true", help="show finding details")
    s.add_argument("numbers", nargs="*", type=int)

    s = sub.add_parser("dossier", help="compact context for one issue (also .cache/dossiers/N.md)")
    s.add_argument("number", type=int)
    s.add_argument("--no-refresh", action="store_true")

    s = sub.add_parser("people", help="who could help on an issue")
    s.add_argument("number", type=int)

    s = sub.add_parser("bugs", help="Bugzilla bugs that may track an issue")
    s.add_argument("number", type=int)

    s = sub.add_parser("labels", help="label gaps vs Bugzilla teams and BCD dirs")
    s.add_argument("--write", action="store_true", help="write plans/labels.json with proposed new labels")
    s.add_argument("--deep", action="store_true", help="also fetch chromestatus feature details (slower, first run)")

    s = sub.add_parser("apply", help="validate and show plans; write to GitHub only with --yes")
    s.add_argument("numbers", nargs="*", type=int, help="default: all pending plans")
    s.add_argument("--yes", action="store_true")

    sub.add_parser("agenda", help="markdown for the triage meeting notes")
    sub.add_parser("improve", help="summarize corrections and skill notes")
    sub.add_parser("doctor", help="check auth scopes and local data sources")

    a = ap.parse_args(argv)

    if a.cmd == "sync":
        from . import sync
        sync.sync_issues(full=a.full)
        return 0

    from . import checks
    if a.cmd == "doctor":
        return doctor()
    ctx = checks.Ctx()

    if a.cmd == "queue":
        from . import rank
        rows = rank.queue(ctx, a.status, a.check, a.closed, set(a.numbers) or None)
        print(f"{len(rows)} issue(s); stable Chrome M{ctx.stable}; synced {_synced()}")
        print(rank.table(rows, ctx, a.limit, a.v))
    elif a.cmd == "dossier":
        from . import dossier
        print(dossier.build(a.number, ctx, refresh=not a.no_refresh))
    elif a.cmd == "people":
        from . import dossier, people
        issue = ctx.issues[a.number]
        print(people.format(people.candidates(issue, ctx, _cached_hints(a.number))))
    elif a.cmd == "bugs":
        from . import body, bugzilla
        issue = ctx.issues[a.number]
        b = body.Body(issue["body"])
        listed = b.value("bugzilla-url")
        if listed:
            print(f"OP lists {listed}")
        for bid in sorted({x for c in issue["comments"] for x in bugzilla.bugs_in(c["body"])}, key=int):
            bug = bugzilla.bug(bid)
            if bug:
                print(f"comment links bug {bid}: {bug['summary']} [{bug['status']}{' ' + bug['resolution'] if bug['resolution'] else ''}] {bug['product']}::{bug['component']}")
        for bug in bugzilla.search(a.number, b.value("spec-url"), b.value("spec-title") or issue["title"]):
            print(f"{bug['id']}: {bug['summary']} [{bug['status']}{' ' + bug['resolution'] if bug['resolution'] else ''}] "
                  f"{bug['product']}::{bug['component']} ({bug['why']})")
    elif a.cmd == "labels":
        from . import labels
        print(labels.report(ctx, a.write, a.deep))
    elif a.cmd == "apply":
        from . import plan
        nums = a.numbers or plan.pending()
        if not nums and not plan.path("labels").exists():
            print("no plans in", config.plans_dir())
            return 0
        plan.apply_labels_file(a.yes)
        ok = True
        for n in nums:
            try:
                ok &= plan.apply(n, a.yes)
            except plan.PlanError as e:
                print(f"#{n}: {e}")
                ok = False
            print()
        if not a.yes and ok:
            print("dry run; re-run with --yes to write")
        return 0 if ok else 1
    elif a.cmd == "agenda":
        from . import agenda
        print(agenda.build(ctx))
    elif a.cmd == "improve":
        from . import improve
        print(improve.summary())
    return 0


def _cached_hints(n):
    import json
    p = config.cache_dir() / "dossiers" / f"{n}.json"
    return json.loads(p.read_text())["hints"] if p.exists() else {}


def _synced():
    import json
    p = config.cache_dir() / "issues.json"
    if not p.exists():
        return "never"
    with open(p) as f:
        return json.load(f).get("synced_at", "?")[:16]


def doctor():
    from . import gh
    ok = True
    sc = gh.scopes()
    need = {"repo", "project", "read:org"}
    missing = need - sc
    print(f"gh scopes: {', '.join(sorted(sc)) or '?'}")
    if missing:
        ok = False
        print(f"  missing {', '.join(sorted(missing))}: run `gh auth refresh -h github.com -s {','.join(sorted(missing))}`")
    print(f"firefox checkout: {config.firefox_path() or 'not found (mots.yaml fetched remotely; no git-log reviewers)'}")
    print(f"BCD checkout: {config.bcd_path() or 'not found (npm data.json used)'}")
    try:
        import ruamel.yaml  # noqa: F401
    except ImportError:
        ok = False
        print("ruamel.yaml missing: pip install -r requirements.txt")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
