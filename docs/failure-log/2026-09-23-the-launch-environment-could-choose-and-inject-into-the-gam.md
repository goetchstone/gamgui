# 2026-09-23 — The launch environment could choose, and inject into, the `gam` holding the credentials

- **Symptom:** a review found `runner.py` honoring `GAMGUI_GAM_BINARY` even in the packaged `.app`,
  `_build_env` passing `gam` a copy of the whole `os.environ` (`DYLD_*`, `PYTHON*` included), and
  `build_app.sh` signing both the app and `gam` without the hardened runtime. A same-user
  `launchctl setenv GAMGUI_GAM_BINARY ~/x` would have handed all three plaintext credentials to an
  attacker's binary on the next launch, with no Keychain prompt; `DYLD_INSERT_LIBRARIES` would have
  loaded code into the app the Keychain trusts. Not exploited.
- **Cause:** the override was added for tests and power users and never scoped to a source checkout;
  inheriting the parent environment is `create_subprocess_exec`'s default and nobody narrowed it;
  the signing step was written to quiet the Keychain, not to harden. It also signed `gam` *before*
  the `codesign --force --deep` of the app, which re-signs every nested Mach-O — so the dedicated
  `gam` signing was silently replaced every build.
- **Why not caught:** no test looked at the child's environment or at `sys.frozen`, and nothing
  checks the build's signing (the `.app` build has no automated test).
- **Fix:** the override is ignored when `sys.frozen`; `gam` gets only `ENV_ALLOWLIST` (+ `GAMCFGDIR`,
  `GAM_NO_UPDATE_CHECK`; the mock's `GAM_MOCK_*` only in a source checkout); the new
  `scripts/sign_app.sh` signs app and `gam` with `--options runtime`, `gam` after the deep sign,
  with minimal entitlements (`scripts/app.entitlements`: library validation off only, because a
  self-signed bundle has no Team ID; `gam`: upstream GAM's own set).
- **Prevention:** `tests/test_runner.py::test_gam_inherits_only_the_allowlisted_environment` (a
  real child reports its env, frozen and not) and `::test_binary_override_is_ignored_in_the_packaged_app`;
  `tests/test_build_signing.py` (runtime flag, sign order, entitlement sets, `gam.entitlements` ==
  the vendored binary's). Unproven until a hardened `.app` is built and launched: the signing ran
  only on an ad-hoc copy of a built bundle, and the app-side entitlement choice was measured with a
  hardened copy of the build Python, not the frozen app.
