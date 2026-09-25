# Screenshots

The four images the root `README.md` shows. Regenerate them — never capture by hand from a real
tenant (hand captures once put the operator's company name and an internal calendar name in this
public repo):

```bash
.venv/bin/python scripts/readme_screenshots.py
```

It runs the mock-backed app (`scripts/preview_mock.py`: strict mock `gam`, fake example.com data)
and drives headless Google Chrome at 1280 px wide, 2x. Needs Google Chrome installed. Scrollbars are
hidden, so before each capture the script opens any scroll box whose content overflows it (the
offboarding preview's panel once cut the image off mid-step, above its Run button). Look at each
image before committing — the pre-commit hook's private-term check can't read pixels.

| File | Screen | Why it sells the tool |
| --- | --- | --- |
| `users.png` | Users list | the core: search, status, bulk department |
| `signatures.png` | Signature designer with the live preview | the headline use case for many visitors |
| `calendars.png` | Calendars — a name-search result + "who has access" | the "find/clean up shared calendars" win |
| `lifecycle.png` | Offboarding preview: the auto-reply and all eight steps' exact `gam` commands, down to Run offboarding | shows the guided, guarded automation |
