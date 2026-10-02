# 2026-09-25 — A tenant switch left the old tenant's previews, Builder result and a queued directory read live

- **Symptom:** a review (R5, R9, R11) replayed a confirm step across `/setup/switch`: a signature
  Apply previewed on example.com wrote example.com's people through example.org's credentials, and a
  Builder `add delegate` previewed with bare names ran on the new tenant, where GAM completes a bare
  name with the active domain. A directory read queued on the `UserCache` lock during the switch
  stored example.com's users (or groups) as example.org's for the TTL, and `/builder/results` and its
  CSV kept serving the old tenant's rows, judged "external" against the new tenant's domains.
- **Cause:** `_verify_and_activate` swapped the connector and invalidated the two caches but left
  `AppState.previews` and `builder_last_result`; `Previews.take` compared only the form and the TTL.
  `UserCache.get` read its generation after acquiring the lock, but the caller had bound `fetch` to
  the old connector before queuing, so a switch while it waited passed the generation check.
- **Why not caught:** the switch tests checked the caches only. The cache race test covered the
  background revalidate and a single in-flight fetch, never a waiter queued across an `invalidate`.
- **Fix:** `AppState.activate` (the one place the connector changes) drops the caches and, on a new
  domain, clears held previews and the Builder's last result; `Previews.bind` ties each held preview
  to the active domain and `take` refuses a mismatch; `UserCache.get` reads the generation on entry.
  Also from the same review: offboarding drops the directory list again when its run finishes, and
  user detail serves the cached list with its age (`stale_ok`) instead of blocking on `print users`.
- **Prevention:** `tests/test_tenant_switch.py` — a signature Apply and a Builder Run previewed
  before a switch write nothing after it; the Builder result and held previews are gone after a
  switch; a users and a groups read queued on the lock across a switch is not kept (deterministic,
  gated fakes). Offline only: no live switch between two real tenants has been run.
