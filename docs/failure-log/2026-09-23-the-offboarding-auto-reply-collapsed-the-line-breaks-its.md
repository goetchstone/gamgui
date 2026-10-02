# 2026-09-23 — The offboarding auto-reply collapsed the line breaks its preview showed

- **Symptom:** a review (F12) previewed a message with a blank line between two paragraphs: the
  block headed "Auto-reply senders will receive" showed two paragraphs (`whitespace-pre-line`), and
  the command sent `message 'Line one.\r\n\r\nPlease contact …' html` — one run-on paragraph in
  HTML. A typed `<` or `&` would have been markup.
- **Cause:** the raw text went out with `html`; GAM strips `\r` and turns only the two characters
  `\n` (not a real line break) into `<br/>` (`setVacation`, read from the vendored build).
- **Why not caught:** no test looked at the message element of the vacation argv; the runbook listed
  it as a known gotcha instead of a defect.
- **Fix:** `lifecycle.autoreply_html` escapes the text (`&`, `<`, `>`, and a backslash as `&#92;` so
  GAM's `\n` rule can't fire) and joins its lines with `<br/>`; the preview block still shows the
  text.
- **Prevention:** `test_offboard_autoreply_is_sent_as_the_text_the_preview_shows`. How Gmail renders
  the reply is not verified live (first live run: send the leaver a test mail).
