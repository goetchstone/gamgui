# 2026-09-25 — A plain-text auto-reply lost text on an unchanged save; the Builder's vacation sent its text as markup

- **Symptom:** (review 2: R10) the user page's Vacation form pre-filled a plain-text auto-reply (Gmail's
  plain-text mode, or one set without `html`) with text missing: `if a<b then call` became `if a`,
  `Reach Tom <tom@example.com> & Jerry` lost the address — and saving the form unchanged wrote the
  shortened text back. Separately, the Builder's "Set vacation auto-reply" (`build.set_vacation`) sent
  its message raw with `html`, so line breaks collapsed and a typed `<` or `&` became markup — the bug
  the offboarding step and the Vacation form were fixed for on 2026-09-23.
- **Cause:** `autoreply_text` put every body through `HTMLParser`, but `show vacation` prints the stored
  body as it is — HTML or plain text — and `Vacation` doesn't record which. The Builder's catalog
  entry was written before `autoreply_html` existed and was missed when the other two senders moved.
- **Why not caught:** the round-trip tests only fed bodies the app itself writes (HTML) and a Gmail
  HTML body; no test built `build.set_vacation`'s argv from a message with a line break or `<`.
- **Fix:** `autoreply_text` decodes a body only when it holds an email tag written as a tag, or an entity
  that stands for something; anything else is kept verbatim (and the form sends it back through
  `autoreply_html`, so the reply reads the same). `build.set_vacation` sends `autoreply_html(message)`.
- **Prevention:** `tests/test_lifecycle.py::test_a_plain_text_auto_reply_is_pre_filled_as_it_is`,
  `::test_an_html_auto_reply_is_read_as_html`, `::test_the_builders_vacation_sends_the_text_as_the_html_it_names`.
  Mock-only: that GAM's `show vacation` prints a plain-text body verbatim (and an HTML one as its HTML)
  is the review's reading of GAM, not verified on a live tenant — the heuristic is what keeps either safe.
