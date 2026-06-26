"""Click-to-insert operator chips for GAM's search-query fields.

Each Google query language is different (Admin SDK user search vs ChromeOS device search vs Drive
v3 `q`), so the operators here were researched and adversarially verified against the official
Google docs before shipping — wrong operators would mislead an admin. Snippets carry realistic
example values; the *_NOTE strings explain the value/operator rules shown under the chips.
"""

from __future__ import annotations

# gam print users query <QueryUser>  — Admin SDK Directory users.list 'query'
USER_QUERY_HINTS = [
    "isSuspended=true", "isAdmin=true", "isEnrolledIn2Sv=false",
    "orgUnitPath=/Sales", "orgUnitPath='/Sales/Field Reps'",
    "email:admin*", "givenName:Jan*", "orgTitle:Manager",
    "orgDepartment=Engineering", "manager='boss@example.com'",
]
USER_QUERY_NOTE = (
    "field=value — '=' exact, ':' contains, ':prefix*' starts-with (givenName/familyName/email only). "
    "Space-separate clauses to AND them; single-quote values with spaces. orgUnitPath is an exact "
    "match (not recursive)."
)

# gam print cros query <QueryCrOS>  — Admin SDK Directory chromeosdevices.list 'query'
CROS_QUERY_HINTS = [
    "status:provisioned", "asset_id:1234", 'user:"tom sawyer"', "location:seattle",
    "recent_user:user@domain.com", "sync:2026-01-01..",
    "last_user_activity:2026-06-01..2026-06-30", "aue:2026-01-01..2026-12-31",
    "chrome_version:111", 'public_model_name:"Pixelbook Go"',
]
CROS_QUERY_NOTE = (
    "field:value (colon only — no =, <, >). Quote multi-word values. Dates are YYYY-MM-DD with '..' "
    "ranges (sync:2026-01-01.. = on/after). Space-separate to AND. "
    "status: provisioned | disabled | deprovisioned | managed."
)

# gam <user> print filelist query <QueryDriveFile>  — Drive API v3 files.list 'q'
DRIVE_QUERY_HINTS = [
    "'me' in owners", "name contains 'budget'", "fullText contains 'confidential'",
    "mimeType='application/vnd.google-apps.folder'", "trashed=false", "starred=true",
    "'user@domain.com' in writers", "sharedWithMe=true",
    "modifiedTime > '2026-01-01T00:00:00'", "and", "or",
]
DRIVE_QUERY_NOTE = (
    "Drive query — single-quote string values; booleans (trashed/starred/sharedWithMe) are bare "
    "true/false. Combine clauses with the and/or chips. Timestamps are RFC3339, quoted."
)
