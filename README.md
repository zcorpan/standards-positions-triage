# standards-positions-triage

Terminal triage for [mozilla/standards-positions](https://github.com/mozilla/standards-positions), working from [project 262](https://github.com/orgs/mozilla/projects/262) and [chromestatus's pending Gecko reviews](https://chromestatus.com/reports/external_reviews/gecko).

The work is split in two:

- `sptriage`, a Python CLI, fetches data, runs checks, ranks issues and applies changes.
- A Claude Code skill (`.claude/skills/sp-triage`) does the judgment calls: labels, who to ask, comment wording, and reading threads for objections.

Every write is drafted as a plan file and shown as a diff first. Nothing reaches GitHub until you approve it.

## Quick start

After [Setup](#setup), open two terminal windows, both in this repo's root (not in a standards-positions checkout, or the skill won't load):

1. **Claude:** run `claude`, then ask e.g. "what's ready to close?" or "triage the Unscreened issues". It drafts plans and shows them.
2. **Review:** run `python3 -m sptriage review` to step through the plans and apply the ones you approve. Or tick entries in `review/*.md` in your editor and run `python3 -m sptriage apply`.

Use a terminal that supports clickable links, e.g. iTerm2, Ghostty, WezTerm, kitty or VS Code's. In Terminal.app, issue URLs are printed on their own line instead.

## Setup

```
git clone git@github.com:zcorpan/standards-positions-triage.git
cd standards-positions-triage
pip install -r requirements.txt          # ruamel.yaml; Python 3.11+
gh auth refresh -h github.com -s project,read:org   # plus the default `repo` scope
python3 -m sptriage doctor
python3 -m sptriage sync                 # first run ~2 min, then incremental
```

You can triage without any write access: reading works for everyone. Applying needs triage rights on the repo and write access to project 262 (`mozilla/standards-contributor`).

Optional local checkouts make the who-to-ask hints better:
- `~/git/mozilla/firefox` gives `mots.yaml`, moz.build Bugzilla components and `r=` reviewer counts.
- `~/git/mdn/browser-compat-data` provides BCD (otherwise the npm `data.json` is fetched).

A `~/git/mozilla/standards-positions` checkout (`sp_path`) is needed to change `activities.yml`; without one it's read from GitHub.

Override these paths and the thresholds in `sptriage.toml`; see `sptriage/config.py`.

## Use

With Claude Code, run `claude` in this directory and ask, e.g., "triage the unscreened s-p issues", "what's ready to close?", or "agenda for the triage meeting".

Without Claude:

```
python3 -m sptriage queue [--status Unscreened] [--check ready-close] [-v]
python3 -m sptriage dossier 1472     # everything known about one issue
python3 -m sptriage people 1472      # who could help, with s-p experience and load
python3 -m sptriage bugs 1456        # Bugzilla bugs tracking the feature
python3 -m sptriage labels           # label gaps vs Bugzilla teams and BCD dirs
python3 -m sptriage agenda           # markdown for the meeting notes
python3 -m sptriage show [-v]         # pending plans by queue; rewrites review/*.md
python3 -m sptriage approve close    # tick a queue (close, comment, labels, decide) or N…
python3 -m sptriage review           # step through plans in a terminal, approving each
python3 -m sptriage apply            # write approved plans to GitHub, then verify each
python3 -m sptriage rebase 1472      # an issue changed: show what, and move the plan's base
```

Plans are reviewed in `review/close.md`, `comment.md`, `labels.md`, `decide.md` and `errors.md`: tick `[x]` in an editor, or use `approve`. `apply` writes only ticked entries whose plan hasn't changed since. Colors and links appear on a TTY (`--color always|never`, `NO_COLOR`). Terminal.app has no clickable links, so issue URLs get their own dimmed line there (`SPTRIAGE_LINKS=osc8|url` overrides the detection).

Some resolutions also need `activities.yml`: for issues below 1110 its `position` overrides the label on the dashboard, and a rationale can go stale. Validation says when a plan needs a `yml` block. `apply` then writes the GitHub edits, commits the yml change on a new branch in a worktree under `.cache/sp-worktree/` (your checkout isn't touched), and prints the push and `gh pr create` commands. The close waits until the change is on main; run `apply` again after the merge.

A later issue that continues a locked one (a "continuation") is resolved on the locked issue through the plan's `parent` block, then closed as its duplicate.

## Data sources

- GitHub: issues, labels, the project 262 Status field, assignable users, and `mozilla` org members (to tell Mozillians apart from others).
- chromestatus `external_reviews/gecko` and per-feature details: stage, milestones, spec, Safari view, wpt dirs, web-feature id.
- web-features `data.json` and BCD: web-feature ids, per-key Firefox support, MDN URLs, and Bugzilla links from `impl_url`.
- Bugzilla: bugs whose see_also or URL points at the issue or spec, and component → team.
- mozilla/firefox: module owners and peers, and reviewers of the implementing directory.

`data/taxonomy.yml` maps spec hosts to venue and topic, BCD dirs to topic labels, and Bugzilla teams to team labels. `data/people.yml` maps GitHub logins to Gecko nicks.

## Local state

These directories are gitignored and stay on your machine:
- `.cache/`: fetched data
- `plans/`: drafted and applied plans
- `review/`: the review queues
- `.state/`:
  - `journal.jsonl`: every write, with your login, and whether GitHub matched the plan afterwards
  - `progress.json`, `yml-branches.json`: plans waiting for a yml PR
  - `corrections.jsonl`: where an applied plan differed from the draft
  - `skill-notes.md`: notes for improving the skill

`python3 -m sptriage improve` summarizes the corrections and notes. The skill offers a PR only when a pattern recurs or guidance was wrong.

See [docs/ci.md](docs/ci.md) for running this from a bot account later.
