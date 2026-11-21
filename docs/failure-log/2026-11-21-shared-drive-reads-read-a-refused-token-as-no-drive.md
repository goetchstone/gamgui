# 2026-11-21 — A shared drive read read a token Google refused as "no shared drive"

- **Symptom:** none live; found while reading GAM's source for the named reports' schedules (F7a), during review of #176.
  When Google refuses the service-account token with one of `REFRESH_PERM_ERRORS`, the shared drive permissions run
  would have said the domain has no shared drive and every listed drive's members were read, and a scheduled run would
  have shown every member as gone.
- **Cause:** `printShowSharedDrives` and `printShowSharedDriveACLs` (the 7.52.04 tag) call `buildGAPIServiceObject`, whose
  `handleOAuthTokenError(softErrors)` returns None without a word for those errors; the loop `continue`s before its
  `NO_ENTITIES_FOUND_RC` check, so GAM writes the titles alone and exits 0. Both steps took exits 0 and 60 as whole
  answers, and no row at either as "none". GAM's own none is 60 only.
- **Why not caught:** the mock modelled "no drive" (60) and a refused `drives.list` (50), never the silent token skip; and
  every drive's permissions refused, which GAM answers with 60 as for none, wasn't modelled either. The rule "no output at
  all is a refused token" (`_answered`) was known for other reads, not for a read that still writes its titles.
- **Fix:** `Step.empty_rcs`: the drives and drive-members steps take no row as an answer only at 60; at 0, `audit_read`
  raises `NoRowUnsaid` and the step is unknown, said in words. The mock's `drive_token_silent` writes the titles alone,
  exit 0, and every drive dropped now exits 60 as GAM does (the drive list then names each one unread).
- **Prevention:** `test_titles_alone_at_exit_0_are_a_token_google_refused_silently_never_no_drive`,
  `test_every_drive_dropped_is_gam_s_60_and_every_drive_is_named_unread`. A read whose "none" GAM says with its own code
  takes no row as an answer only at that code.
