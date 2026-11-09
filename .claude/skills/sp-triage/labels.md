# sp-triage: label gaps

- `labels` reports team/topic labels missing versus Bugzilla teams and BCD dirs (`--deep`: chromestatus too); `--write` drafts `plans/labels.json`, which `show` lists.
- New labels need the user's explicit OK and a `data/taxonomy.yml` mapping in the same change.
- Name team labels after the Bugzilla team and topic labels after the BCD dir; use MDN Web/ section names only where BCD has nothing.
- Never rename or split an existing label.
