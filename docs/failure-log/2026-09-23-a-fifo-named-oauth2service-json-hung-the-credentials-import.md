# 2026-09-23 — A FIFO named `oauth2service.json` hung the credentials import, and the app with it

- **Symptom:** a review found that importing from a folder whose `oauth2service.json` is a FIFO
  never returned, and every other request stalled behind it until a writer opened the FIFO.
  Reproduced offline with `os.mkfifo`; not seen live.
- **Cause:** `_open_in_dir` (`core/setup.py`) opened the file blocking and only then did the
  `S_ISREG` check — the comment even said "a fifo would block", but the open that blocks runs first.
  `POST /setup/import` called the synchronous `import_dir` straight on the event loop, so one blocked
  `open` froze the whole server.
- **Why not caught:** the import tests covered symlinks, unreadable and vanishing files, never a
  non-regular one; and no route test checked where the blocking work runs.
- **Fix:** open `O_NONBLOCK` (a FIFO opens at once and is refused as not regular; the wipe's
  `O_WRONLY` fails `ENXIO`), then restore blocking mode for the regular file; the route runs
  `import_dir` via `asyncio.to_thread`. Invariant #5's pin + `O_NOFOLLOW` are unchanged.
- **Prevention:** `test_a_fifo_named_like_a_credential_is_refused_without_blocking` (core),
  `test_import_route_refuses_a_fifo_credential_quickly` and
  `test_import_route_runs_the_filesystem_import_off_the_event_loop` (route); the `fifo` fixture's
  watchdog releases a blocked reader so a regression fails on time instead of hanging the suite.
