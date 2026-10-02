# 2026-09-25 — The user page's auto-reply collapsed its line breaks and came back as markup

- **Symptom:** a review (U4b) found the user page's Vacation form had offboarding's 2026-09-23 bug:
  a message typed as two paragraphs went out `message 'Line one.\r\n\r\nLine two' html` — one run-on
  paragraph in HTML, and a typed `<` or `&` was markup. The other way, `show vacation` prints the
  stored HTML body, so a message saved as HTML (by this form once fixed, or in Gmail) pre-filled the
  box as `Line one.<br/><br/>…`, and saving it unchanged would have escaped the tags into the reply.
- **Cause:** `users.vacation_set` passed the textarea straight to `set_vacation(html=True)`, and the
  template put `vac.message` (the stored body) in the textarea; the offboarding fix
  (`lifecycle.autoreply_html`) was applied to that one caller only.
- **Why not caught:** the form's tests sent one-line messages (`away`), which are the same as text and
  as HTML; the failure-log entry named the offboarding step, not the other caller of `html`.
- **Fix:** the form sends `autoreply_html(message)`, and pre-fills `lifecycle.autoreply_text(body)` —
  its inverse: `<br>` (and a Gmail `<div>`/`<p>` edge) back to a line break, other tags dropped,
  entities unescaped.
- **Prevention:** `test_vacation_form_sends_its_line_breaks_and_prefills_them_back` (through the
  stateful mock: the argv's message, the pre-filled text, and an unchanged save sending the same
  body), `test_autoreply_text_reads_back_what_autoreply_html_sent`,
  `test_autoreply_text_reads_a_body_written_in_gmail`. That `show vacation` prints an HTML body as stored
  (as the mock does; not re-read from the vendored build this time), and how Gmail normalizes it, are
  not verified live — `autoreply_text` reads `<br>`, `<br/>` and Gmail's `<div>` lines alike. The Builder's "Set vacation auto-reply" still sends its slot as raw HTML.
