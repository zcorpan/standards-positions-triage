"""Deterministic findings for an issue. Judgment calls (is a later comment an objection?) are left to Claude."""

import re
from datetime import datetime, timezone

from . import body as bodymod
from . import activities, config, data, sync
from .bugzilla import bugs_in

PROPOSAL_RE = re.compile(
    r"(?:\b(?:propos(?:e|ing|ed)|suggest(?:ed|ing)?|recommend(?:ed|ing)?)\b[^.\n]{0,60}?|\bposition\s*[:=]\s*|"
    r"\bmozilla\s+position\s*[:=]?\s*|\bmark(?:ing)?\s+(?:this|it)\s+(?:as\s+)?)"
    r"\**`?(?:position:\s*)?(positive|neutral|negative|defer)\b",
    re.I,
)
# A comment ending in a bare `neutral` / **positive**.
TRAILING_RE = re.compile(r"[`*_]+(positive|neutral|negative|defer)[`*_]*\s*\.?\s*$", re.I)
# "Seems reasonable" from a Mozillian usually means positive, but is often hedged: reported as soft.
SOFT_RE = re.compile(r"\b(?:seems|looks|sounds)\s+(?:quite\s+|pretty\s+|very\s+|fairly\s+)?reasonable\b", re.I)
# "changing the position from neutral to positive": the proposal is the second one.
FROM_TO_RE = re.compile(r"\bfrom\s+[`*_]*(?:positive|neutral|negative|defer)[`*_]*\s+to\s+[`*_]*(positive|neutral|negative|defer)\b", re.I)
POSITIONS = {"positive", "neutral", "negative", "defer", "under consideration"}
# Same-repo issue references in an OP: links and bare #N.
REF_RE = re.compile(r"(?:github\.com/mozilla/standards-positions/issues/|(?<![\w/&#])#)(\d{1,5})\b")


class Ctx:
    """Shared, lazily loaded state for a run."""

    def __init__(self):
        self.issues = sync.load_issues()
        self.now = datetime.now(timezone.utc)
        self._moz = None
        self._cs = None
        self._stable = None
        self._locked = None

    @property
    def locked(self):
        if self._locked is None:
            try:
                self._locked = sync.locked()
            except Exception as e:  # network trouble shouldn't block triage
                print(f"warning: locked-issue list unavailable ({e})")
                self._locked = {}
        return self._locked

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


def proposed(text):
    """(position, soft) proposed in a comment's text, or None."""
    text = re.sub(r"(?m)^>.*$", "", text or "").strip()  # ignore quoted text
    m = PROPOSAL_RE.search(text)
    if m:
        ft = FROM_TO_RE.search(text)
        return (ft or m).group(1).lower(), False
    m = TRAILING_RE.search(text)
    if m:
        return m.group(1).lower(), False
    if SOFT_RE.search(text):
        return "positive", True
    return None


def proposals(issue, ctx):
    """Mozillian comments that propose a position: [(index, comment, position, soft)]."""
    out = []
    for i, c in enumerate(issue["comments"]):
        if not ctx.is_mozillian(c["author"]):
            continue
        p = proposed(c["body"])
        if p:
            out.append((i, c, *p))
    return out


def yml_position(n):
    try:
        _, e = activities.entry(n)
    except Exception:
        return None
    return (e or {}).get("position")


def _title_key(title):
    t = re.sub(r"\([^)]*\)", "", title.lower())
    return " ".join(w for w in re.findall(r"[a-z0-9-]+", t) if w not in ("api", "the", "specification", "spec"))


def continuation_of(issue, ctx):
    """The earlier issue this one continues, or None. The OP links one earlier issue (or several, of which exactly
    one has its title in this one's title); that issue is locked or this one is labeled a duplicate; and this one
    has no position of its own."""
    if position_label(issue):
        return None
    refs = sorted(r for r in {int(x) for x in REF_RE.findall(issue["body"] or "")} if r < issue["number"] and r in ctx.issues)
    if len(refs) > 1:
        title = issue["title"].lower()
        refs = [r for r in refs if len(_title_key(ctx.issues[r]["title"])) >= 4 and _title_key(ctx.issues[r]["title"]) in title]
    if len(refs) != 1:
        return None
    if str(refs[0]) in ctx.locked or "duplicate" in issue["labels"]:
        return refs[0]
    return None


def continuation(issue, ctx, props):
    """(parent, kind, message) for a continuation; kind is "new position", "conclusion stands" or "no reply".
    Everything in this issue postdates the parent's conclusion, so any Mozillian proposal here is a new one."""
    pn = continuation_of(issue, ctx)
    if pn is None:
        return None
    par = ctx.issues[pn]
    ppos = yml_position(pn)
    where = "yml" if ppos else "label"
    ppos = ppos or position_label(par) or "none"
    lock = ctx.locked.get(str(pn))
    if lock is not None:
        head = f"continues #{pn} (locked{', ' + lock if lock else ''}; position {ppos} from the {where})"
    else:
        head = f"labeled duplicate, OP links #{pn} ({par['state'].lower()}; position {ppos} from the {where})"
    if props:
        firm = [x for x in props if not x[3]]
        _, c, p, _ = (firm or props)[-1]
        return pn, "new position", (f"{head}: {c['author']} proposed {p} {ctx.days(c['at'])}d ago → resolve on #{pn} "
                                    f"(labels, yml, OP), then close as duplicate of #{pn}")
    replies = [c for c in issue["comments"] if ctx.is_mozillian(c["author"])]
    if replies:
        return pn, "conclusion stands", (f"{head}: {replies[-1]['author']} replied without a new proposal → conclusion "
                                         f"stands: comment linking #{pn}'s position, close as duplicate of #{pn}")
    return pn, "no reply", f"{head}: no Mozillian reply yet"


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
    cont = continuation(issue, ctx, props) if is_open else None
    if cont:
        f.append(("continuation" if str(cont[0]) in ctx.locked else "duplicate", cont[2]))

    if is_open and props and not pos and not cont:
        firm = [x for x in props if not x[3]]
        i, c, p, soft = (firm or props)[-1]
        age = ctx.days(c["at"])
        later = [x for x in comments[i + 1:] if ctx.is_mozillian(x["author"]) and x["author"] != c["author"]]
        if age >= config.CONF["close_days"]:
            note = f"; {len(later)} later Mozillian comment(s) to check for objections" if later else ""
            how = "said 'reasonable' (soft positive; read it)" if soft else f"proposed {p}"
            f.append(("ready-close", f"{c['author']} {how} {age}d ago{note}"))
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
            f.append(("status-mismatch", f"open with 'position: {pos}' → close"))
        for l in ("duplicate", "invalid", "proposal withdrawn"):
            if l in labels and not cont:
                f.append(("status-mismatch", f"open with {l!r} label → close"))
        if not issue["project_item"]:
            f.append(("status-mismatch", "not in project 262"))
    elif status and status not in ("Needs a PR",) and pos is None and issue.get("state_reason") == "COMPLETED" and not any(
            l in labels for l in ("duplicate", "invalid", "proposal withdrawn", "tooling", "enhancement")):
        f.append(("closed-no-position", "closed as completed without a position: label"))

    ypos = yml_position(issue["number"])
    if ypos and pos and ypos != pos:
        f.append(("yml-position", f"activities.yml says {ypos}, the label says {pos}; the dashboard shows {ypos}"))

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
    if pos in ("positive", "neutral") or (props and props[-1][2] in ("positive", "neutral") and not props[-1][3]):
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
