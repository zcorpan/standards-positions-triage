"""Who could help on an issue: OP suggestions, s-p history in the area, and Gecko owners/reviewers."""

import collections
import re

from . import body as bodymod
from . import checks, data, firefox, sync
from .webfeatures import spec_key

BROAD_TOPICS = {"topic: API", "topic: HTML"}


def experience(ctx):
    """{login: number of s-p issues participated in} over the whole cache."""
    if not hasattr(ctx, "_exp"):
        c = collections.Counter()
        for i in ctx.issues.values():
            for p in set(i["participants"]):
                c[p] += 1
        ctx._exp = c
    return ctx._exp


def load(ctx):
    """{login: open s-p issues assigned}"""
    if not hasattr(ctx, "_load"):
        c = collections.Counter()
        for i in ctx.issues.values():
            if i["state"] == "OPEN":
                for a in i["assignees"]:
                    c[a] += 1
        ctx._load = c
    return ctx._load


def login_for_nick(nick, ctx):
    m = data.nick_to_login().get(nick.lower())
    if m:
        return m
    for login in ctx.mozillians:
        if login.lower() == nick.lower():
            return login
    return None


def _area_keys(issue, hints):
    b = bodymod.Body(issue["body"])
    short, _ = spec_key(b.value("spec-url"))
    teams = {l for l in issue["labels"] if l.startswith("team: ")} | set(hints.get("teams", []))
    topics = ({l for l in issue["labels"] if l.startswith("topic: ")} | set(hints.get("topics", []))) - BROAD_TOPICS
    return short, teams, topics


def candidates(issue, ctx, hints=None, limit=8):
    """Ranked [{login|nick, score, reasons, exp, load, assignable, new}]. `hints`: teams, topics, ownership[]."""
    hints = hints or {}
    score = collections.Counter()
    reasons = collections.defaultdict(list)
    nicks_only = {}

    def add(login, pts, why):
        if not login or login.endswith("[bot]"):
            return
        score[login] += pts
        if why not in reasons[login]:
            reasons[login].append(why)

    # already involved here: cc'd by a Mozillian, named in the OP, or commented
    here = checks.engaged(issue, ctx)
    for login, e in here.items():
        if e.get("pinged_at"):
            by = "named in OP" if e["pinged_by"] == issue["author"] and e["pinged_at"] == issue["created"] else f"cc'd by {e['pinged_by']}"
            add(login, 6, f"{by} {e['pinged_at'][:10]}" + ("" if e["replied"] else ", no reply yet"))
        elif e["commented"]:
            add(login, 4, "commented here")

    # s-p history: same spec (strong), same team label, same narrow topic
    short, teams, topics = _area_keys(issue, hints)
    for n, other in ctx.issues.items():
        if n == issue["number"]:
            continue
        o_short, _ = spec_key(bodymod.Body(other["body"]).value("spec-url")) if short else (None, None)
        w, why = 0, None
        if short and o_short == short:
            w, why = 3, f"same spec ({short})"
        elif teams & set(other["labels"]):
            w, why = 2, "/".join(sorted(teams & set(other["labels"])))
        elif topics & set(other["labels"]):
            w, why = 0.5, "/".join(sorted(topics & set(other["labels"])))
        if not w:
            continue
        for a in other["assignees"]:
            add(a, w, f"assigned #{n} ({why})")
        for _, c, _p in checks.proposals(other, ctx):
            add(c["author"], w * 1.5, f"proposed on #{n} ({why})")

    # Gecko ownership: mots owners/peers for the component, git-log reviewers for the path
    for own in hints.get("ownership", []):
        comp, path = own.get("component"), own.get("path")
        mods = firefox.modules_for(component=comp, path=path) if comp else []
        if not path and mods and mods[0]["includes"]:
            path = mods[0]["includes"][0].split("*", 1)[0].rstrip("/") or None
        for m in mods[:1]:
            for nick in m["owners"]:
                _gecko(nick, 3, f"owner {m['name']}", add, nicks_only, ctx)
            for nick in m["peers"][:12]:
                _gecko(nick, 1.5, f"peer {m['name']}", add, nicks_only, ctx)
        if path:
            groups = firefox.group_nicks()
            for nick, count in firefox.reviewers(path)[:8]:
                if nick.startswith("#") or nick.lower() in groups:
                    continue
                _gecko(nick, min(count / 20, 3), f"r= {count}× in {path}", add, nicks_only, ctx)

    exp, ld = experience(ctx), load(ctx)
    assignable = sync.assignable()
    out = []
    for login, s in score.items():
        is_moz = ctx.is_mozillian(login) or login in nicks_only.values()
        if not is_moz:
            continue
        e = exp.get(login, 0)
        adj = s * (1.3 if e >= 3 else 1.0) / (1 + ld.get(login, 0) / 8)
        involved = login in here
        out.append({"who": login, "score": round(adj, 1), "reasons": reasons[login][:3], "exp": e,
                    "load": ld.get(login, 0), "assignable": login in assignable, "involved": involved,
                    "new": e == 0 and not involved,
                    "nick_only": login in nicks_only and not ctx.is_mozillian(login)})
    out.sort(key=lambda x: -x["score"])
    regular = [x for x in out if not x["new"]][:limit]
    new = [x for x in out if x["new"]][:4]
    return {"suggest": regular, "could_ask": new}


def _gecko(nick, pts, why, add, nicks_only, ctx):
    login = login_for_nick(nick, ctx)
    if login:
        add(login, pts, why)
    else:
        key = f"{nick} (nick)"
        nicks_only[key] = key
        add(key, pts, why)


def format(c):
    def row(x):
        flags = []
        if not x["assignable"]:
            flags.append("not assignable")
        flags.append(f"s-p:{x['exp'] if x['exp'] else 'new'}")
        if x.get("involved"):
            flags.append("on issue")
        flags.append(f"load:{x['load']}")
        return f"  {x['who']:24} {x['score']:>5}  {' '.join(flags):34} {'; '.join(x['reasons'])}"

    lines = ["suggest (\"on issue\" = already cc'd or commented; don't re-ping, consider assigning):"] + ([row(x) for x in c["suggest"]] or ["  (none)"])
    if c["could_ask"]:
        lines += ["could ask (new to s-p; ask the user first):"] + [row(x) for x in c["could_ask"]]
    return "\n".join(lines)
