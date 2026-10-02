# 2026-10-01 — Fresh setup's Copy buttons all had one name, the third time this shape shipped

- **Symptom:** Setup's fresh-setup panel (`_commands.html`) rendered one "Copy" button per command, all
  named "Copy". A screen reader's button list could not tell which command each copied. A class-wide
  scan found the same shape in four more places: a calendar's "View access & events →"
  (`_calendar_list.html`), an event's "Delete…" (`_event_results.html`), a queued Builder step's ↑ ↓ ✕
  (`_sequence.html`, named by the glyph alone; `title` is only a description) and a config folder's
  "Import" (`setup.html`).
- **Cause:** each loop rendered a bare verb or glyph, with the item named only in the row's visible text.
  The repeated-button rule was in the runbook, but nothing enforced it outside the templates already fixed.
- **Why not caught:** the earlier tripwires (the tray Stop and
  `test_a_saved_lists_repeated_buttons_name_their_item`) render only the templates they fixed. axe has no
  rule for distinct buttons that share a name, so a new loop passed every check.
- **Fix:** each button names its item in a visually hidden span, as `_command_row.html` does. Copy marks
  its visible word `[data-label]` so `copyText`'s "Copied" doesn't overwrite the hidden part. The glyphs
  are `aria-hidden` behind "Move step N up/down" and "Remove step N".
- **Prevention:** `test_no_looped_button_is_named_by_literal_text_alone` (default run) reads every
  template. It follows `{% for %}` loops, includes and macro calls inside a loop, and fails on any button
  there whose name holds no `{{ }}`. It covers the whole class, not one screen.
