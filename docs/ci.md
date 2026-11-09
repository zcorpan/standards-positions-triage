# Running from CI (placeholder)

Nothing here runs yet. These are notes on what's already shaped for a bot, and what's still open.

## Already in place

- `sptriage` never prompts. Writes happen only with `apply --yes`.
- Auth goes through `gh`, so `GH_TOKEN` works.
- Each write in `.state/journal.jsonl` records the actor login.
- Cache, state and plans dirs are configurable in `sptriage.toml` (`cache_dir`, `state_dir`, `plans_dir`).
- Plans (`plans/N.json`) are the contract. A bot can produce them, and a maintainer applies them, or a bot does after review.
- `apply` exits non-zero when validation fails.

## Open questions

- **Identity.** A bot user in the `mozilla` org, or a GitHub App? Writing needs Issues write on the repo, and Projects write on org project 262 (an App needs the `organization_projects` permission). Assigning and team checks need `read:org`.
- **Triggers.**
  - On `issues: opened`/`edited`: run the checks (no LLM) and post one suggestion comment, or nothing. It shouldn't be noisy; maybe only for missing fields that the bot can fill from chromestatus or web-features.
  - On a schedule: `agenda` and `queue --check ready-close`, as a weekly issue or a step summary.
- **LLM step.** `anthropics/claude-code-action` with this skill could draft plans as artifacts. A maintainer would then apply them locally with `sptriage apply`, or approve them through a label or comment command.
- **What's safe to auto-apply.**
  - Candidates: stripping a title prefix, fixing an unambiguous `status-mismatch` (assigned while in `Needs assignees` → `Needs proposed position`), and filling a WebKit link that chromestatus already has.
  - Labels, assignees, positions and closing stay human-approved.
- **Caching in CI.** Use `actions/cache` for `.cache/` (the issue snapshot, web-features, BCD index). A full sync takes about 2 min. There's no Firefox checkout in CI, so `mots.yaml` is fetched remotely and the reviewer counts are skipped.
- **In-tree move.** `sptriage/` would go to `tools/triage/` in mozilla/standards-positions and the skill to `.claude/skills/sp-triage/`. `config.ROOT` and the data paths would then need to point there.
