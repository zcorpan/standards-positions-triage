"""sptriage: triage helper for mozilla/standards-positions. Run `python3 -m sptriage <command> -h`."""

import argparse
import sys

from . import config


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sptriage", description=__doc__)
    ap.add_argument("--color", choices=["auto", "always", "never"], help="colors and links (default: auto, on a TTY)")
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

    s = sub.add_parser("show", help="pending plans by review queue; rewrites review/*.md (no GitHub writes)")
    s.add_argument("numbers", nargs="*", type=int)
    s.add_argument("-v", action="store_true", help="show OP diffs, comment text and why")

    s = sub.add_parser("approve", help="tick plans in review/*.md: queue names (close, comment, labels, decide), numbers, or all")
    s.add_argument("targets", nargs="+")
    s = sub.add_parser("unapprove", help="untick plans in review/*.md")
    s.add_argument("targets", nargs="+")

    s = sub.add_parser("review", help="step through plans in a terminal, approving each")
    s.add_argument("numbers", nargs="*", type=int)

    s = sub.add_parser("apply", help="show approved plans, ask, write them to GitHub, then verify each")
    s.add_argument("numbers", nargs="*", type=int, help="only these (still must be approved)")
    s.add_argument("--dry-run", action="store_true", help="validate and show what would be written")
    s.add_argument("-y", "--yes", action="store_true", help="write without showing the plans and asking first")

    s = sub.add_parser("rebase", help="after the issue changed: show what changed and move the plan's base forward")
    s.add_argument("number", type=int)

    sub.add_parser("agenda", help="markdown for the triage meeting notes")
    sub.add_parser("improve", help="summarize corrections and skill notes")
    sub.add_parser("doctor", help="check auth scopes and local data sources")

    a = ap.parse_args(argv)
    from . import untrusted
    untrusted.install()  # issue text can't send escape sequences to the terminal
    if a.color:
        from . import term
        term.set_mode(a.color)

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
        from . import body, bugzilla, untrusted
        issue = ctx.issues[a.number]
        print("Bug summaries and OP values are third-party data (JSON-quoted).")
        b = body.Body(issue["body"])
        listed = b.value("bugzilla-url")
        if listed:
            print(f"OP lists {untrusted.q(listed)}")
        for bid in sorted({x for c in issue["comments"] for x in bugzilla.bugs_in(c["body"])}, key=int):
            bug = bugzilla.bug(bid)
            if bug:
                print(f"comment links bug {bid}: {untrusted.q(bug['summary'])} [{bug['status']}{' ' + bug['resolution'] if bug['resolution'] else ''}] {bug['product']}::{bug['component']}")
        for bug in bugzilla.search(a.number, b.value("spec-url"), b.value("spec-title") or issue["title"]):
            print(f"{bug['id']}: {untrusted.q(bug['summary'])} [{bug['status']}{' ' + bug['resolution'] if bug['resolution'] else ''}] "
                  f"{bug['product']}::{bug['component']} ({bug['why']})")
    elif a.cmd == "labels":
        from . import labels
        print(labels.report(ctx, a.write, a.deep))
    elif a.cmd == "show":
        from . import review
        print(review.show(set(a.numbers) or None, a.v))
    elif a.cmd in ("approve", "unapprove"):
        from . import review
        print(review.approve(a.targets, on=a.cmd == "approve"))
    elif a.cmd == "review":
        from . import review
        return review.interactive(set(a.numbers) or None)
    elif a.cmd == "apply":
        from . import review
        return 0 if review.apply(set(a.numbers) or None, a.dry_run, a.yes) else 1
    elif a.cmd == "rebase":
        from . import plan
        print(plan.rebase(a.number))
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
