"""Markdown for the monthly triage meeting notes ("Next" section)."""

from . import checks, rank, untrusted

URL = "https://github.com/mozilla/standards-positions/issues/"


def build(ctx, per=6):
    rows = rank.queue(ctx)
    by = {}
    for s, issue, f in rows:
        for code, msg in f:
            by.setdefault(code, []).append((issue, msg))
    out = []

    def section(title, items):
        if items:
            out.append(f"* {title}")
            for issue, msg in items[:per]:
                out.append(f"  * [{untrusted.md_text(issue['title'])}]({URL}{issue['number']}) ({msg})")
            if len(items) > per:
                out.append(f"  * … {len(items) - per} more")

    soon = []
    for s, issue, f in rows:
        cs = ctx.chromestatus.get(issue["number"])
        if cs and cs["stage"] == "shipping" and ctx.stable:
            m = cs.get("start") or cs.get("end")
            if m and -1 <= m - ctx.stable <= 4:  # older estimates are usually stale
                soon.append((m, issue, f"Chromium shipping M{m}"))
    soon = [(i, msg) for _, i, msg in sorted(soon, key=lambda x: x[0])]
    section("Shipping soon in Chromium", soon)
    section("Ready to resolve (proposal ≥7 days old)", by.get("ready-close", []))
    section("Requester waiting on us", by.get("ping-unanswered", []))
    section("Stalled assignees", by.get("stalled", []))
    unscreened = sorted((i for i in ctx.issues.values() if i["state"] == "OPEN" and i["status"] == "Unscreened"),
                        key=lambda i: i["created"])
    out.append(f"* Unscreened: {len(unscreened)} open")
    for i in unscreened[:3]:
        out.append(f"  * [{untrusted.md_text(i['title'])}]({URL}{i['number']}) (opened {i['created'][:10]})")
    return "\n".join(out)
