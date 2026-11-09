"""Queue ordering and the compact queue table."""

from . import checks, term
from .chromestatus import urgency

WEIGHT = {"ready-close": 50, "status-mismatch": 20, "ping-unanswered": 30, "cc-unanswered": 12, "stalled": 10, "missing-bug": 5,
          "title": 2, "unlabeled": 3, "missing-fields": 1, "proposed": 0, "closed-no-position": 2}


def score(issue, findings, ctx):
    s = urgency(ctx.chromestatus.get(issue["number"]), ctx.stable)
    codes = {c for c, _ in findings}
    s += sum(WEIGHT.get(c, 0) for c in codes)
    if issue["status"] == "Unscreened":
        s += 15 + min(ctx.days(issue["created"]) // 30, 15)
    return s


def queue(ctx, status=None, check=None, include_closed=False, numbers=None):
    rows = []
    for n, issue in ctx.issues.items():
        if numbers and n not in numbers:
            continue
        if issue["state"] != "OPEN" and not include_closed:
            continue
        if status and (issue["status"] or "").lower() != status.lower():
            continue
        f = checks.run(issue, ctx)
        if check and not any(c == check for c, _ in f):
            continue
        rows.append((score(issue, f, ctx), issue, f))
    rows.sort(key=lambda r: (-r[0], -r[1]["number"]))
    return rows


STATUS_ABBR = {"Unscreened": "unscr", "Needs assignees": "n-asg", "Needs proposed position": "n-pos",
               "Position is proposed": "prop", "Needs a PR": "n-pr", "Blocked": "block", None: "-"}


def table(rows, ctx, limit=20, verbose=False):
    lines = []
    for s, issue, f in rows[:limit]:
        cs = ctx.chromestatus.get(issue["number"])
        stage = ""
        if cs:
            m = cs.get("end") or cs.get("start")
            stage = f"{cs['stage'][:8]}{'/M' + str(m) if m else ''}"
        codes = sorted({c for c, _ in f}, key=lambda c: -WEIGHT.get(c, 0))
        title = issue["title"][:60]
        status = STATUS_ABBR.get(issue['status'], (issue['status'] or '')[:5])
        code_s = f"{','.join(codes)[:44]:44}"
        if codes and codes[0] == "ready-close":
            code_s = term.c(code_s, "green")
        lines.append(f"{term.issue(issue['number'], 5)} {status:5} {term.c(f'{stage:16}', 'dim')} {code_s} {title}")
        lines += term.url_lines(term.issue_url(issue["number"]), " " * 6)
        if verbose:
            for c, msg in f:
                lines.append(term.c(f"{'':28}{c}: {msg}", "dim"))
    total = len(rows)
    if total > limit:
        lines.append(f"… {total - limit} more (--limit)")
    return "\n".join(lines)
