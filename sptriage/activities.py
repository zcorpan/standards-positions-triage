"""activities.yml in mozilla/standards-positions: read entries, check whether a plan needs a yml change, and
prepare that change as commits on a branch in a separate worktree (the user pushes it and opens the PR)."""

import io
import json
import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .http import get

LEGACY_MAX = 1110  # issues below this may keep `position` in the yml, where it overrides the label
RAW = "https://raw.githubusercontent.com/mozilla/standards-positions/main/activities.yml"
YML_KEYS = {"position", "rationale", "description"}
POSITIONS = {"positive", "neutral", "negative", "defer"}
REMOVES = ("duplicate", "invalid", "proposal withdrawn")

_memo = {}


class YmlError(RuntimeError):
    pass


def checkout():
    p = Path(config.CONF["sp_path"]).expanduser()
    return p if (p / "activities.yml").exists() and (p / ".git").exists() else None


def _git(args, cwd=None, check=True):
    p = subprocess.run(["git", *args], cwd=cwd or checkout(), capture_output=True, text=True)
    if check and p.returncode != 0:
        raise YmlError(f"git {' '.join(args[:2])}: {p.stderr.strip() or p.stdout.strip()}")
    return p


def fetch():
    """Update origin/main in the checkout (refs only; the working tree isn't touched)."""
    if checkout():
        _git(["fetch", "-q", "origin", "main"])
        _memo.clear()


def text(ref="origin/main"):
    """activities.yml as of `ref` in the checkout, else from GitHub main."""
    if ref not in _memo:
        if checkout():
            p = _git(["show", f"{ref}:activities.yml"], check=False)
            _memo[ref] = p.stdout if p.returncode == 0 else (checkout() / "activities.yml").read_text()
        else:
            _memo[ref] = get(RAW).decode("utf-8")
    return _memo[ref]


def _yaml():
    try:
        from ruamel.yaml import YAML
    except ImportError as e:
        raise YmlError("ruamel.yaml is needed for activities.yml (`pip install ruamel.yaml`)") from e
    y = YAML()
    y.preserve_quotes = True
    y.width = float("inf")  # same settings as the repo's activities.py, so a round trip changes nothing else
    return y


def parse(src):
    return _yaml().load(src)


def entries(ref="origin/main"):
    key = ("parsed", ref)
    if key not in _memo:
        _memo[key] = {v["issue"]: (k, dict(v)) for k, v in (parse(text(ref)) or {}).items()
                      if isinstance(v, dict) and isinstance(v.get("issue"), int)}
    return _memo[key]


def entry(n, ref="origin/main"):
    """(key, fields) for issue N's entry, or (None, None)."""
    return entries(ref).get(n, (None, None))


def matches(n, want, ref="origin/main"):
    """True when the entry for N has every field in `want` (rationale/description compared stripped)."""
    _, e = entry(n, ref)
    if e is None:
        return False
    return all(str(e.get(k) or "").strip() == str(v or "").strip() for k, v in want.items())


def _positions(labels):
    return [l[len("position: "):] for l in labels or [] if l.startswith("position: ")]


def needs(sub, n, ref="origin/main"):
    """(errs, warns) for one target of a plan: does resolving it need a yml change the plan lacks?"""
    errs, warns = [], []
    y = sub.get("yml") or {}
    if not isinstance(y, dict) or set(y) - YML_KEYS:
        return [f"yml must be an object with keys from {sorted(YML_KEYS)}"], []
    try:
        key, e = entry(n, ref)
    except Exception as ex:  # no checkout and no network: say so, don't block unrelated plans
        return [], [f"can't read activities.yml ({ex}); check by hand whether #{n} needs a yml change"]
    added, removed = _positions(sub.get("labels_add")), _positions(sub.get("labels_remove"))
    changes = bool(added or removed)
    if y.get("position"):
        if n >= LEGACY_MAX:
            errs.append(f"yml.position isn't allowed for issues >= {LEGACY_MAX}; the label is the position")
        if y["position"] not in POSITIONS:
            errs.append(f"yml.position must be one of {sorted(POSITIONS)}")
        if added and y["position"] != added[0]:
            errs.append(f"yml.position {y['position']!r} doesn't match the label position: {added[0]}")
    if e is not None and changes and e.get("position") and not y.get("position"):
        errs.append(f"activities.yml {key!r} has position: {e['position']}, which overrides the label on the dashboard; "
                    "add yml.position (goes through a PR)")
    if e is not None and changes and e.get("rationale") and "rationale" not in y:
        warns.append(f"activities.yml {key!r} has a rationale that may be stale after the position change; "
                     "add yml.rationale, or confirm it still holds")
    closes_out = (sub.get("close") or {}).get("reason") == "duplicate" or set(sub.get("labels_add") or []) & set(REMOVES)
    if e is not None and closes_out:
        warns.append(f"activities.yml {key!r} will be dropped from the dashboard by this close; remove the entry by hand "
                     "if it shouldn't stay")
    if y and e is None and not y.get("rationale") and not y.get("description"):
        errs.append(f"#{n} has no activities.yml entry; a new one needs yml.rationale or yml.description")
    return errs, warns


def _commit_title(key, y):
    return f"{key} ({y['position']})" if y.get("position") else f"Update {key}"


def edit_file(path, n, y, title=None):
    """Apply yml fields for issue N to the activities.yml at `path`. Returns the entry key."""
    from ruamel.yaml.comments import CommentedMap
    from ruamel.yaml.scalarstring import LiteralScalarString

    yaml = _yaml()
    data = yaml.load(path.read_text())
    key = next((k for k, v in data.items() if isinstance(v, dict) and v.get("issue") == n), None)
    if key is None:
        if not title:
            raise YmlError(f"no activities.yml entry for #{n} and no title to create one")
        key = title
        if key in data:
            raise YmlError(f"key {key!r} is already used by issue {data[key].get('issue')}")
        data[key] = CommentedMap(issue=n)
    for k in ("position", "description", "rationale"):
        if k in y:
            v = y[k]
            data[key][k] = LiteralScalarString(v.rstrip("\n") + "\n") if k != "position" else v
    data[key] = CommentedMap(sorted(data[key].items()))
    out = io.StringIO()
    yaml.dump(CommentedMap(sorted(data.items())), out)
    path.write_text(out.getvalue())
    return key


def _state():
    p = config.state_dir() / "yml-branches.json"
    return p, (json.loads(p.read_text()) if p.exists() else {})


def branch_for(plan_id):
    return _state()[1].get(str(plan_id))


def prepare(changes):
    """Commit yml changes on one new branch in a worktree under .cache/sp-worktree/.
    `changes`: [(plan_id, n, yml, title, see)] where `see` is the issue the commit body points at.
    Returns (branch, worktree, commands for the user)."""
    sp = checkout()
    if not sp:
        raise YmlError(f"yml changes need a mozilla/standards-positions checkout at sp_path ({config.CONF['sp_path']})")
    fetch()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    branch = f"triage-{stamp}-" + "-".join(str(c[1]) for c in changes)[:40]
    wt = config.cache_dir() / "sp-worktree" / branch
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(["worktree", "add", "-q", "-b", branch, str(wt), "origin/main"])
    commits = []
    try:
        for _, n, y, title, see in changes:
            key = edit_file(wt / "activities.yml", n, y, title)
            v = subprocess.run(["python3", "activities.py", "validate", "--fix"], cwd=wt, capture_output=True, text=True)
            if v.returncode != 0:
                raise YmlError(f"activities.py validate failed for #{n}: {(v.stdout + v.stderr).strip()[-500:]}")
            _git(["add", "activities.yml"], cwd=wt)
            commits.append((_commit_title(key, y), f"See #{see}."))
            _git(["commit", "-q", "-m", commits[-1][0], "-m", commits[-1][1]], cwd=wt)
    except Exception:
        _git(["worktree", "remove", "--force", str(wt)], check=False)
        _git(["branch", "-D", branch], check=False)
        raise
    p, st = _state()
    for plan_id, *_ in changes:
        st[str(plan_id)] = branch
    p.write_text(json.dumps(st, indent=1) + "\n")
    cmds = [f"git -C {wt} push -u origin {branch}",
            _pr_create(branch, commits)]
    return branch, wt, cmds


def _pr_create(branch, commits):
    """`gh pr create` with an explicit title and body: `--fill` reads the local repo, which is the triage
    checkout when the user runs it, not the worktree."""
    if len(commits) == 1:
        title, body = commits[0]
    else:
        title = "Update activities.yml"
        body = "\n".join(f"- {t}. {b}" for t, b in commits)
    return (f"gh pr create -R {config.REPO} --base main --head {branch} "
            f"--title {shlex.quote(title)} --body {shlex.quote(body)}")


def done(plan_id):
    """Forget the branch for a finished plan and remove its worktree once no plan uses it."""
    p, st = _state()
    branch = st.pop(str(plan_id), None)
    p.write_text(json.dumps(st, indent=1) + "\n")
    if branch and branch not in st.values() and checkout():
        wt = config.cache_dir() / "sp-worktree" / branch
        if wt.exists():
            _git(["worktree", "remove", "--force", str(wt)], check=False)
