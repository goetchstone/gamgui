# 2026-09-25 — The jobs tray hid under the Users table, stayed open under focus, and named every Stop "Stop"

- **Symptom:** a review (R2-R4) opened the header's jobs tray over two running jobs on `/users`: the
  table's sticky "ORG UNIT … STATUS" row painted across the tray, and a real click on the second job's
  Stop re-sorted the table instead of stopping the job. Tabbing past the tray's last row moved focus onto
  controls the still-open tray covered (WCAG 2.4.11), and Escape no longer closed it once focus was out.
  The tray's buttons were all named "Stop". Separately, adding a delegate by Enter left focus on the new
  Add button and a screen reader heard nothing of "Added …".
- **Cause:** the `<header>`'s `backdrop-blur` makes it a stacking context; at `z-auto` it paints under
  `<main>`'s later positioned content, so the panel's `z-50` counted only inside the header. `app.js`
  closed the tray on Escape only with focus inside it, and on no focusout. The `stop` macro rendered a
  bare "Stop". The delegate success notice had no `data-announce`, and `said` kept a line forever, so
  the same line coming back after something else was never news.
- **Why not caught:** the tray's axe screen and keyboard test used one finished job (no Stop) and never
  Tabbed out of it or hit-tested it over a page with a sticky header. axe checks names, not duplicates
  across rows. The ratchet ran one domain, so no screen rendered the setup switcher or the tenant chip's
  admin line.
- **Fix:** `relative z-50` on the header; `app.js` closes the tray on Escape from anywhere (focus back
  to the button when it was in the tray or lost) and on a focusout to outside it; each Stop carries its
  job's title in an `sr-only` span; the delegate add's notice is `data-announce="delegates"`, and
  `said` forgets a key no element carries any more.
- **Prevention:** `test_the_open_tray_is_on_top_names_each_stop_and_closes_when_focus_leaves` (two
  running jobs on `/users`: `elementFromPoint` at every tray link and Stop lands in `#jobs-panel`, each
  Stop names its job, Tab out closes it), `test_adding_a_delegate_by_enter_is_said`, and a
  `setup/two-domains` axe screen. Headless Chrome only: WKWebView and VoiceOver unseen.
