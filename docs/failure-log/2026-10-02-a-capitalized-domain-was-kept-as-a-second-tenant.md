# 2026-10-02 — A capitalized domain was kept as a second tenant

- **Symptom:** the operator's Keychain listed one tenant twice, as `Example.com` and `example.com`.
  Setup showed a two-domain switcher where Check access was expected, each launch connected to the
  capitalized copy (the first sorted), and no screen could remove either.
- **Cause:** import and verify keyed the Keychain by the domain as typed (`domain.strip()`).
  `static/setup.js` lowercases only the domain it infers from the admin email, not one typed into
  the field, so a second import under another capitalization made a second entry.
- **Why not caught:** every test and the mock used lowercase domains. The in-memory vault is
  case-sensitive, as the Keychain's `SecItem` matching is documented to be, so nothing modelled a
  second spelling, and no screen listed the Keychain's domains with a way to remove one. The case
  had been seen once and patched only for display: the header lowercases the domain ("setup kept
  whatever was typed", `_active_tenant`), which also hid which spelling was connected.
- **Fix:** import and verify lowercase the domain. The Setup switcher offers Remove for an inactive
  domain, by its exact stored spelling so an old capitalized entry stays removable. Before deleting a
  spelling that has a case twin, `SecretsVault.folds_case()` probes the real Keychain with a
  throwaway item; if it ignores case, the twins share one item set, and Remove only drops the name.
- **Prevention:** `test_import_and_verify_store_a_capitalized_domain_lowercased`,
  `test_remove_deletes_only_that_spellings_credentials_and_redraws_the_panel`, and
  `test_removing_a_case_twin_on_a_store_that_folds_case_keeps_the_items_they_share`, which uses a
  case-folding fake store because the real Keychain can't be tested here. The probe decides at
  Remove time against the real store, rather than trusting either the document or the mock. Not yet
  proven live: the operator's removal of the capitalized entry is the first real run.
