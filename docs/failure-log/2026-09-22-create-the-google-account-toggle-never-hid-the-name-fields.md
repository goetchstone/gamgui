# 2026-09-22 — "Create the Google account" toggle never hid the name fields on desktop

- **Symptom:** the First/Last name fields (relevant only to account creation) were always visible at
  desktop width; ticking/unticking "Create the Google account" did nothing.
- **Cause:** the container was `class="hidden ... sm:grid ..."`. Tailwind's responsive `sm:grid` (a
  later media-query rule) overrode the base `hidden` at >=640px, so its display was always `grid`;
  the toggle flipped `hidden`, which `sm:grid` kept beating.
- **Why not caught:** every test POSTed form data directly; none rendered the page and checked the
  toggle's computed display at a real width. Found by actually using the app.
- **Fix:** drop the static `sm:grid`; obToggleCreate toggles `grid` (and `hidden`) so only one is
  present at a time. Verified in a 1000px browser: none -> grid -> none.
- **Prevention:** never pair a base display utility (`hidden`) with a responsive one (`sm:grid`) that
  overrides it — toggle the class you mean; a render-and-check-computed-display test would catch it.
