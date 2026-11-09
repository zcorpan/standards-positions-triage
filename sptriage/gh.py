"""GitHub access through the `gh` CLI (auth from `gh auth` or `GH_TOKEN`)."""

import json
import subprocess


class GhError(RuntimeError):
    pass


def run(args, input=None, check=True):
    p = subprocess.run(["gh", *args], input=input, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])}…: {p.stderr.strip() or p.stdout.strip()}")
    return p


def api(path, method="GET", fields=None, paginate=False, check=True):
    """REST call. Returns parsed JSON (a list when paginating arrays), or None on empty body."""
    args = ["api", path, "-X", method]
    if paginate:
        args += ["--paginate", "--slurp"]
    for k, v in (fields or {}).items():
        args += ["-f", f"{k}={v}"]
    p = run(args, check=check)
    if p.returncode != 0:
        return None
    out = p.stdout.strip()
    if not out:
        return None
    data = json.loads(out)
    if paginate and isinstance(data, list) and data and isinstance(data[0], list):
        data = [x for page in data for x in page]
    return data


def status_code(path):
    """HTTP status for a GET, e.g. 204/404 for membership checks."""
    p = run(["api", path, "-i", "--silent"], check=False)
    first = (p.stdout or "").split("\n", 1)[0]
    parts = first.split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def graphql(query, **variables):
    body = {"query": query, "variables": {k: v for k, v in variables.items() if v is not None}}
    p = run(["api", "graphql", "--input", "-"], input=json.dumps(body))
    data = json.loads(p.stdout)
    if data.get("errors"):
        raise GhError(json.dumps(data["errors"])[:500])
    return data["data"]


def viewer():
    return graphql("{viewer{login}}")["viewer"]["login"]


def scopes():
    p = run(["auth", "status", "-h", "github.com"], check=False)
    text = p.stdout + p.stderr
    for line in text.splitlines():
        if "Token scopes" in line:
            return {s.strip(" '\"") for s in line.split(":", 1)[1].split(",")}
    return set()
