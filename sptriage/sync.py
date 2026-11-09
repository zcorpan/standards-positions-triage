"""Fetch and cache GitHub issues, project fields, labels, and people lists."""

import json
import time
from datetime import datetime, timedelta, timezone

from . import config, gh
from .http import cached

DAY = 86400

ISSUE_FIELDS = """
  number title body state stateReason createdAt updatedAt closedAt
  author { login }
  labels(first:40) { nodes { name } }
  assignees(first:10) { nodes { login } }
  participants(first:60) { nodes { login } }
  projectItems(first:5) { nodes { id project { number } fieldValueByName(name:"Status") {
    ... on ProjectV2ItemFieldSingleSelectValue { name } } } }
  comments(last:10) { totalCount nodes { author { login } createdAt body url } }
"""

ISSUES_QUERY = """
query($owner:String!, $name:String!, $cursor:String, $since:DateTime) {
  repository(owner:$owner, name:$name) {
    issues(first:50, after:$cursor, filterBy:{since:$since}, orderBy:{field:UPDATED_AT, direction:ASC}) {
      pageInfo { hasNextPage endCursor }
      nodes { %s }
    }
  }
}
""" % ISSUE_FIELDS

ISSUE_QUERY = """
query($owner:String!, $name:String!, $number:Int!) {
  repository(owner:$owner, name:$name) { issue(number:$number) { %s } }
}
""" % ISSUE_FIELDS


def _issues_path():
    return config.cache_dir() / "issues.json"


def load_issues():
    path = _issues_path()
    if not path.exists():
        raise SystemExit("No issue cache; run `python3 -m sptriage sync` first.")
    with open(path) as f:
        return {int(k): v for k, v in json.load(f)["issues"].items()}


def _flatten(node):
    items = [
        {"id": i["id"], "status": (i.get("fieldValueByName") or {}).get("name")}
        for i in node["projectItems"]["nodes"]
        if i["project"]["number"] == config.PROJECT_NUMBER
    ]
    return {
        "number": node["number"],
        "title": node["title"],
        "body": node["body"] or "",
        "state": node["state"],
        "state_reason": node["stateReason"],
        "created": node["createdAt"],
        "updated": node["updatedAt"],
        "closed": node["closedAt"],
        "author": (node["author"] or {}).get("login"),
        "labels": [l["name"] for l in node["labels"]["nodes"]],
        "assignees": [a["login"] for a in node["assignees"]["nodes"]],
        "participants": [p["login"] for p in node["participants"]["nodes"]],
        "project_item": items[0]["id"] if items else None,
        "status": items[0]["status"] if items else None,
        "comment_count": node["comments"]["totalCount"],
        "comments": [
            {"author": (c["author"] or {}).get("login"), "at": c["createdAt"], "body": c["body"], "url": c["url"]}
            for c in node["comments"]["nodes"]
        ],
    }


def sync_issues(full=False, log=print):
    path = _issues_path()
    store = {"synced_at": None, "issues": {}}
    if path.exists() and not full:
        with open(path) as f:
            store = json.load(f)
    since = store["synced_at"]
    started = datetime.now(timezone.utc)
    if since:
        # small overlap for clock skew
        since = (datetime.fromisoformat(since) - timedelta(minutes=5)).isoformat()
    cursor, n, t0 = None, 0, time.time()
    while True:
        data = gh.graphql(ISSUES_QUERY, owner=config.OWNER, name=config.NAME, cursor=cursor, since=since)
        conn = data["repository"]["issues"]
        for node in conn["nodes"]:
            store["issues"][str(node["number"])] = _flatten(node)
            n += 1
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    store["synced_at"] = started.isoformat()
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(store, f)
    tmp.replace(path)
    log(f"synced {n} issue(s) in {time.time() - t0:.1f}s ({len(store['issues'])} cached)")
    return store


def refresh_issue(number):
    """Re-fetch one issue into the cache (used before writing a dossier or applying a plan)."""
    node = gh.graphql(ISSUE_QUERY, owner=config.OWNER, name=config.NAME, number=number)["repository"]["issue"]
    issue = _flatten(node)
    path = _issues_path()
    if path.exists():
        with open(path) as f:
            store = json.load(f)
        store["issues"][str(number)] = issue
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(store, f)
        tmp.replace(path)
    return issue


PROJECT_QUERY = """
query($owner:String!, $number:Int!) {
  organization(login:$owner) { projectV2(number:$number) { id
    field(name:"Status") { ... on ProjectV2SingleSelectField { id options { id name } } } } }
}
"""


def project_meta():
    def fetch():
        p = gh.graphql(PROJECT_QUERY, owner=config.OWNER, number=config.PROJECT_NUMBER)["organization"]["projectV2"]
        return {
            "project_id": p["id"],
            "status_field": p["field"]["id"],
            "options": {o["name"]: o["id"] for o in p["field"]["options"]},
        }

    return cached("project", DAY, fetch)


def repo_labels():
    def fetch():
        data = gh.api(f"repos/{config.REPO}/labels?per_page=100", paginate=True)
        return {l["name"]: {"color": l["color"], "description": l.get("description") or ""} for l in data}

    return cached("labels", DAY, fetch)


def assignable():
    def fetch():
        data = gh.api(f"repos/{config.REPO}/assignees?per_page=100", paginate=True)
        return sorted(u["login"] for u in data)

    return set(cached("assignable", DAY, fetch))


def contributor_team():
    def fetch():
        data = gh.api(f"orgs/{config.OWNER}/teams/{config.CONTRIBUTOR_TEAM}/members?per_page=100", paginate=True, check=False)
        return sorted(u["login"] for u in data or [])

    return set(cached("team", DAY, fetch))


def org_members():
    """Logins in the mozilla GitHub org (private members are visible to a member caller); cached a week."""

    def fetch():
        data = gh.api(f"orgs/{config.OWNER}/members?per_page=100", paginate=True, check=False)
        return sorted(u["login"] for u in data or [])

    return set(cached("org-members", 7 * DAY, fetch))


def locked():
    """{issue number (str): lock reason or ""} for locked issues; cached a day."""

    def fetch():
        q = f"repo:{config.REPO} is:issue is:locked"
        pages = gh.api(f"search/issues?per_page=100&q={q.replace(' ', '+').replace(':', '%3A')}", paginate=True) or []
        pages = pages if isinstance(pages, list) else [pages]
        return {str(i["number"]): i.get("active_lock_reason") or "" for pg in pages for i in pg.get("items", [])}

    return cached("locked", DAY, fetch)


def timeline(number, ttl=3600):
    """Assignment and label events for one issue."""

    def fetch():
        events = gh.api(f"repos/{config.REPO}/issues/{number}/timeline?per_page=100", paginate=True) or []
        keep = []
        for e in events:
            ev = e.get("event")
            if ev in ("assigned", "unassigned"):
                keep.append({"event": ev, "at": e["created_at"], "login": e["assignee"]["login"], "by": (e.get("actor") or {}).get("login")})
            elif ev in ("labeled", "unlabeled"):
                keep.append({"event": ev, "at": e["created_at"], "label": e["label"]["name"], "by": (e.get("actor") or {}).get("login")})
            elif ev in ("renamed", "closed", "reopened", "cross-referenced"):
                item = {"event": ev, "at": e.get("created_at"), "by": (e.get("actor") or {}).get("login")}
                if ev == "cross-referenced":
                    src = (e.get("source") or {}).get("issue") or {}
                    item["ref"] = src.get("html_url")
                keep.append(item)
        return keep

    (config.cache_dir() / "timeline").mkdir(exist_ok=True)
    return cached(f"timeline/{number}", ttl, fetch)
