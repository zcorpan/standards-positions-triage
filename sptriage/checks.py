"""Deterministic findings for an issue. Judgment calls (is a later comment an objection?) are left to Claude."""

import re
from datetime import datetime, timezone

from . import body as bodymod
from . import config, data, sync
from .bugzilla import bugs_in

PROPOSAL_RE = re.compile(
    r"(?:\b(?:propos(?:e|ing|ed)|suggest(?:ed|ing)?|recommend(?:ed|ing)?)\b[^.\n]{0,60}?|\bposition\s*[:=]\s*|"
    r"\bmozilla\s+position\s*[:=]?\s*|\bmark(?:ing)?\s+(?:this|it)\s+(?:as\s+)?)"
    r"\**`?(?:position:\s*)?(positive|neutral|negative|defer)\b",
    re.I,
)
POSITIONS = {"positive", "neutral", "negative", "defer", "under consideration"}


class Ctx:
    """Shared, lazily loaded state for a run."""

    def __init__(self):
        self.issues = sync.load_issues()
        self.now = datetime.now(timezone.utc)
        self._moz = None
        self._cs = None
        self._stable = None

    @property
    def mozillians(self):
        if self._moz is None:
            self._moz = (sync.org_members() | sync.assignable() | sync.contributor_team() | set(data.people())) - {None}
        return self._moz

    def is_mozillian(self, login):
        return bool(login) and login in self.mozillians and not login.endswith("[bot]")

    @property
    def chromestatus(self):
        if self._cs is None:
            from . import chromestatus
            try:
                self._cs = chromestatus.reviews()
                self._stable = chromestatus.stable_milestone()
            except Exception as e:  # network trouble shouldn't block triage
                print(f"warning: chromestatus unavailable ({e})")
                self._cs, self._stable = {}, None
        return self._cs

    @property
    def stable(self):
        self.chromestatus
        return self._stable

    def days(self, iso):
        return (self.now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).days


def position_label(issue):
    for l in issue["labels"]:
        if l.startswith("position: "):
            return l[len("position: "):]
    return None


def proposals(issue, ctx):
    """Mozillian comments that propose a position: [(index, comment, position)]."""
    out = []
    for i, c in enumerate(issue["comments"]):
        if not ctx.is_mozillian(c["author"]):
            continue
        text = re.sub(r"(?m)^>.*$", "", c["body"] or "")  # ignore quoted text
        m = PROPOSAL_RE.search(text)
        if m:
            out.append((i, c, m.group(1).lower()))
    return out


MENTION_RE = re.compile(r"(?<![\w/@.])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))\b")


def _strip_code(text):
    return re.sub(r"`[^`\n]*`", "", re.sub(r"(?s)```.*?```", "", text))


def engaged(issue, ctx):
    """People already involved on this issue: {login: {pinged_by, pinged_at, replied, commented}}.
    A ping is an @-mention in a Mozillian's comment (or the OP's Mozillians field); `replied` means the
    person commented after their latest ping."""
    out = {}
    b = bodymod.Body(issue["body"])
    for login in MENTION_RE.findall(_strip_code(b.value("mozillians"))):
        out.setdefault(login, {"pinged_by": issue["author"], "pinged_at": issue["created"], "replied": False, "commented": False})
    for c in issue["comments"]:
        author = c["author"]
        if author in out:
            out[author]["commented"] = True
            out[author]["replied"] = True
        elif ctx.is_mozillian(author):
            out[author] = {"pinged_by": None, "pinged_at": None, "replied": True, "commented": True}
        if not ctx.is_mozillian(author):
            continue
        text = _strip_code(re.sub(r"(?m)^>.*$", "", c["body"] or ""))
        for login in {m for m in MENTION_RE.findall(text) if ctx.is_mozillian(m)} - {author}:
            e = out.setdefault(login, {"commented": False})
            e.update(pinged_by=author, pinged_at=c["at"], replied=False)
    return out


def run(issue, ctx):
    """List of (code, message) findings."""
    f = []
    is_open = issue["state"] == "OPEN"
    labels = issue["labels"]
    status = issue["status"]
    pos = position_label(issue)
    b = bodymod.Body(issue["body"])
    props = proposals(issue, ctx)
    comments = issue["comments"]

    if is_open and props and not pos:
        i, c, p = props[-1]
        age = ctx.days(c["at"])
        later = [x for x in comments[i + 1:] if ctx.is_mozillian(x["author"]) and x["author"] != c["author"]]
        if age >= config.CONF["close_days"]:
            note = f"; {len(later)} later Mozillian comment(s) to check for objections" if later else ""
            f.append(("ready-close", f"{c['author']} proposed {p} {age}d ago{note}"))
        else:
            f.append(("proposed", f"{c['author']} proposed {p} {age}d ago (wait until {config.CONF['close_days']}d)"))
        if status in ("Unscreened", "Needs assignees", "Needs proposed position"):
            f.append(("status-mismatch", f"status {status!r} but a position is proposed → 'Position is proposed'"))
    elif is_open and status == "Position is proposed" and not pos:
        moz = [c for c in comments if ctx.is_mozillian(c["author"])]
        last = moz[-1]["at"] if moz else issue["updated"]
        if ctx.days(last) >= config.CONF["close_days"]:
            f.append(("ready-close", f"status 'Position is proposed' (wording not detected; read thread), last Mozillian comment {ctx.days(last)}d ago"))
    if is_open:
        if issue["assignees"] and status in ("Unscreened", "Needs assignees"):
            f.append(("status-mismatch", f"status {status!r} but assigned → 'Needs proposed position'"))
        if pos and pos in POSITIONS - {"under consideration"}:
            f.append(("status-mismatch", f"open with 'position: {pos}' → close, or 'Needs a PR'"))
        for l in ("duplicate", "invalid", "proposal withdrawn"):
            if l in labels:
                f.append(("status-mismatch", f"open with {l!r} label → close"))
        if not issue["project_item"]:
            f.append(("status-mismatch", "not in project 262"))
    elif status and status not in ("Needs a PR",) and pos is None and issue.get("state_reason") == "COMPLETED" and not any(
            l in labels for l in ("duplicate", "invalid", "proposal withdrawn", "tooling", "enhancement")):
        f.append(("closed-no-position", "closed as completed without a position: label"))

    # stalled assignees (approximate; the dossier has exact assignment dates)
    if is_open and issue["assignees"] and not props:
        last = issue["comments"][-1]["at"] if comments else issue["created"]
        for a in issue["assignees"]:
            if not any(c["author"] == a for c in comments) and ctx.days(last) > config.CONF["stall_days"]:
                f.append(("stalled", f"{a} assigned, no comment from them; last activity {ctx.days(last)}d ago"))

    # Mozillians cc'd/pinged who haven't replied
    if is_open and not props and not pos:
        for login, e in engaged(issue, ctx).items():
            if e.get("pinged_at") and not e["replied"] and e["pinged_by"] != issue["author"]:
                age = ctx.days(e["pinged_at"])
                if age > config.CONF["ping_days"]:
                    f.append(("cc-unanswered", f"{login} cc'd by {e['pinged_by']} {age}d ago, no reply"))

    # requester pings with no Mozillian reply
    if is_open and comments:
        tail = []
        for c in reversed(comments):
            if ctx.is_mozillian(c["author"]):
                break
            tail.append(c)
        if tail and any(c["author"] == issue["author"] for c in tail) and not ctx.is_mozillian(issue["author"]):
            age = ctx.days(tail[0]["at"])
            if age > config.CONF["ping_days"]:
                f.append(("ping-unanswered", f"{len(tail)} comment(s) incl. the requester, no Mozillian reply for {age}d"))

    # missing bug
    if pos in ("positive", "neutral") or (props and props[-1][2] in ("positive", "neutral")):
        if not b.value("bugzilla-url"):
            found = sorted({x for c in comments for x in bugs_in(c["body"])}, key=int)
            hint = f" (comments link bug {', '.join(found[:3])})" if found else ""
            f.append(("missing-bug", f"no Bugzilla URL in OP{hint}"))

    if is_open:
        if bodymod.clean_title(issue["title"]):
            f.append(("title", f"prefix: {bodymod.clean_title(issue['title'])!r}"))
        if not any(l.startswith(("topic: ", "venue: ")) for l in labels):
            f.append(("unlabeled", "no topic:/venue: label"))
        if b.form and b.missing():
            key = [m for m in b.missing() if m in ("spec-url", "web-feature", "bugzilla-url", "webkit-standards-position")]
            if key:
                f.append(("missing-fields", ", ".join(key)))
        if not b.fields:
            f.append(("missing-fields", "no template fields in OP"))
    return f
