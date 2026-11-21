# 2026-11-21 — A job panel polled into its Reports card, which the job's own page doesn't have

- **Symptom:** found by `/code-review high` on the 4.3b branch, never seen by the operator. Reports' Inactive → suspend
  panel (`_inactive_suspend_run.html`, #156) polled `hx-target="#inactive-suspend"`. Opened from the Jobs tray, the job's
  own page (`/jobs/<id>`) loads the panel into `#job-panel`, so the first poll had no target: the panel stayed at its
  first render, never showing progress or the result there. The new suspended-licenses panel copied it.
- **Cause:** the panel was written for the card it starts in. `_bulk_ou_run.html`, `_offboard_run.html` and
  `_incident_run.html` poll with `hx-target="this" hx-swap="outerHTML"`, which works wherever the panel is loaded.
- **Why not caught:** the tests await the job's task and read the status route's HTML; none loads `/jobs/<id>` in a
  browser and lets htmx poll. The a11y run visits a job page, but with a finished job, which doesn't poll.
- **Fix:** both panels poll themselves (`hx-target="this"`, outerHTML, no outer wrapper); the license panel's "Preview
  again" is a link to the Reports card. Each flow's apply test asserts the poll targets itself.
- **Prevention:** those two assertions only. `_remind_run.html` (`#remind-<key>`) and `_group_remove_run.html`
  (`#member-job`) still poll a card's id; a tripwire that every `PANELS` status template polls `this` would hold them all.
