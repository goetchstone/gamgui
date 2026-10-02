# 2026-09-23 — The Builder's Run ran the live form, not the command it previewed

- **Symptom:** a review (F14) previewed "Undelete account" for carol, loaded "Delete account", typed
  alice and clicked the stale blue Run: alice's account was deleted, with no preview and no confirm
  dialog. Previewing a suspend for carol and retyping the address suspended alice. A step added to
  a sequence after its preview also ran unpreviewed.
- **Cause:** the preview's Run posts `hx-include="#builder-form"` (the live form, including the
  hidden `cid`) with a fixed `confirmed=1`; `/builder/run` rebuilt the argv from that form, and
  `/builder/sequence/run` ran whatever `builder_sequence` held at click time. Loading another
  command never cleared the old preview.
- **Why not caught:** every Builder test posted the form it meant to run; none previewed one
  command and ran another. The failure-log entry for the bare-POST sweep counted `confirmed=1` as
  closing "a stale form", which it cannot.
- **Fix:** a Builder mutation runs only from its preview: `/preview` holds the argv under a
  single-use token (`web/previews.py`) and `/run` runs the held argv, answering an edited form or a
  used, expired or missing token with a fresh preview of what the form holds now. The sequence
  preview holds its steps the same way. Builder mutations also need `confirmed=1`
  (`confirm_step=True`). Reads are unchanged.
- **Prevention:** `test_builder_run_executes_only_the_previewed_command` (both PoC paths),
  `test_builder_run_is_single_use`, `test_builder_mutation_runs_only_from_its_preview`,
  `test_sequence_run_executes_only_the_previewed_sequence`, `test_sequence_run_is_single_use`.
  Offline only; no GAM argv changed.
