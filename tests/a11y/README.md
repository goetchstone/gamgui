# tests/a11y — the axe-core accessibility ratchet

`tests/test_a11y.py` runs [axe-core](https://github.com/dequelabs/axe-core) over every main screen
of the mock-backed app in headless Chrome (`make a11y`, CI's `a11y` job) and holds its serious and
critical violations to `baseline.json`: `{screen: {rule id: affected node count}}`. A new rule or
screen, or a higher count, fails; so does a lower one, until the baseline is rewritten to lock the
gain in (`A11Y_UPDATE_BASELINE=1 .venv/bin/python -m pytest -m a11y`). The baseline only shrinks.

## Vendored: axe-core 4.13.0 (MPL-2.0)

| File | What |
|---|---|
| `axe.min.js` | unmodified, from the npm tarball `https://registry.npmjs.org/axe-core/-/axe-core-4.13.0.tgz` |
| `axe.min.js.sha256` | its SHA-256; the test refuses a file that doesn't match |
| `LICENSE`, `LICENSE-3RD-PARTY.txt` | axe-core's licence (MPL-2.0) and its bundled MIT dependencies', from the same tarball |

Checked when vendored (2026-09-24): the tarball matched the registry's `dist.integrity`
(`sha512-UzGt8zg7Ny8djbYMhxl2zuEevVa7r2gJjYY5Lwr1xM7+XU2nd6CkIWFTVcCIbAP63vSz71NaVyyuSk9lHKcy0A==`),
and `axe.min.js` matched the package's own `sri-history.json` entry for 4.13.0
(`sha256-wk8Je9L0UdT5M+i8fY1Tn4ZyouvLXMn58+7IypRwoME=`). It is test tooling only: nothing here
ships in the `.app` or is served by the app — the test injects it over the DevTools protocol.

**Updating:** download the new release's tarball from the registry, check it against the
registry's `dist.integrity`, replace the three files, rewrite `axe.min.js.sha256`
(`shasum -a 256 axe.min.js > axe.min.js.sha256`) and update this file. A new axe can add or change
rules, so re-record the baseline in the same commit and say which counts moved and why — that is
the one time the baseline may grow.
