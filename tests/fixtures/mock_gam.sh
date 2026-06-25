#!/bin/sh
# Mock `gam` binary for offline tests. Behavior is driven by the argument vector and a couple of
# env vars set by the test:
#   GAM_MOCK_FIXTURES  - directory holding the *.json fixtures to echo back
#   GAM_MOCK_REFRESH   - if set, simulate an OAuth token refresh by rewriting oauth2.txt in GAMCFGDIR
#
# It also asserts (for authenticated calls) that GAMCFGDIR was set, so the materialization path is
# genuinely exercised.

set -eu

case "${1:-}" in
  version)
    echo "GAM 7.46.02 - mock"
    exit 0
    ;;
  MOCKFAIL)
    # MOCKFAIL <kind> -> emit a representative stderr line and exit non-zero.
    case "${2:-unknown}" in
      notfound) echo "ERROR: 404: Entity User does not exist - notFound" 1>&2 ;;
      scope)    echo "ERROR: 403: Request had insufficient authentication scopes" 1>&2 ;;
      rate)     echo "ERROR: 429: userRateLimitExceeded - rate limit" 1>&2 ;;
      auth)     echo "ERROR: invalid_grant: Token has been expired or revoked" 1>&2 ;;
      *)        echo "ERROR: something unexpected happened" 1>&2 ;;
    esac
    exit 1
    ;;
esac

# Read commands -> echo the matching fixture.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "users" ]; then
  cat "$GAM_MOCK_FIXTURES/print_users.json"
  exit 0
fi
if [ "${1:-}" = "info" ] && [ "${2:-}" = "user" ]; then
  if [ "${3:-}" = "bob@example.com" ]; then
    cat "$GAM_MOCK_FIXTURES/info_user_suspended.json"
  else
    cat "$GAM_MOCK_FIXTURES/info_user.json"
  fi
  exit 0
fi
if [ "${1:-}" = "print" ] && [ "${2:-}" = "group-members" ]; then
  cat "$GAM_MOCK_FIXTURES/group_members.json"
  exit 0
fi

# `gam user <email> show vacation` (no formatjson) -> parseable text, like real GAM.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && [ "${4:-}" = "vacation" ]; then
  cat <<'EOF'
User: someone@example.com, Vacation:
  Enabled: True
  Contacts Only: False
  Domain Only: False
  Subject: Out of office
  Message:
    I am away until next week.
EOF
  exit 0
fi

# `gam report users ...` -> usage CSV (with a leading progress line, like real GAM).
if [ "${1:-}" = "report" ] && [ "${2:-}" = "users" ]; then
  cat <<'EOF'
Getting Reports for the customer
email,date,accounts:used_quota_in_mb,drive:num_items_created,gmail:num_emails_received,gmail:num_emails_sent
alice@example.com,2026-06-16,608873,1,125,10
bob@example.com,2026-06-16,1048576,0,5,0
carol@example.com,2026-06-16,2048,2,40,3
EOF
  exit 0
fi

# `gam user <email> show signature` (no formatjson) -> text.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && [ "${4:-}" = "signature" ]; then
  cat <<'EOF'
SendAs Address: <someone@example.com>
  IsPrimary: True
  Default: True
  Signature:
    Best,<br>Alice
EOF
  exit 0
fi

# `gam print groups member <email>` -> CSV of the user's group emails.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "groups" ] && [ "${3:-}" = "member" ]; then
  printf 'email\nsales@example.com\nstaff@example.com\n'
  exit 0
fi

# `gam print groups [fields ...]` -> NDJSON list of groups.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "groups" ]; then
  printf '%s\n' \
    '{"email":"sales@example.com","name":"Sales","directMembersCount":3}' \
    '{"email":"staff@example.com","name":"Staff","directMembersCount":10}' \
    '{"email":"it@example.com","name":"IT","directMembersCount":2}'
  exit 0
fi

# `gam user <email> print messages ...` -> formatjson NDJSON; one row carries an Amazon SES
# Return-Path header so the mailbox-search flow has an envelope-sender to surface.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "messages" ]; then
  case "$*" in
    *FAILME*) echo "ERROR: 400: Bad Request - precondptionFailed: a representative GAM failure detail" 1>&2; exit 1 ;;
  esac
  # `print messages` has no formatjson mode — real GAM returns CSV. One row carries an Amazon SES
  # Return-Path header so the mailbox-search flow has an envelope-sender to surface.
  cat <<'EOF'
User,id,Subject,From,Date,Return-Path
someone@example.com,msg_1001,Your receipt,billing@vendor.example,"Mon, 23 Jun 2026 14:29:10 +0000",<0101019ef4e29302-4b960d36-aba1-4a59-9f22-123f07e3fce8-000000@us-west-2.amazonses.com>
someone@example.com,msg_1002,Weekly digest,news@vendor.example,"Tue, 24 Jun 2026 09:00:00 +0000",<bounce@vendor.example>
EOF
  exit 0
fi

# `gam user <email> print forwardingaddresses` -> plain CSV (forwardingEmail + verification).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "forwardingaddresses" ]; then
  printf 'User,forwardingEmail,verificationStatus\n%s,fwd@example.com,accepted\n' "${2:-}"
  exit 0
fi

# `gam user <email> print delegates` (no formatjson) -> plain CSV, like real GAM.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "delegates" ]; then
  printf 'User,delegateAddress,delegationStatus\n%s,assistant@example.com,accepted\n%s,backup@example.com,accepted\n' "${2:-}" "${2:-}"
  exit 0
fi

# `gam user <email> print calendaracls primary formatjson` -> NDJSON of calendar access rules.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendaracls" ]; then
  cat "$GAM_MOCK_FIXTURES/calendar_acls.json"
  exit 0
fi

# `gam print resources ...` -> NDJSON of resource (room) calendars.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "resources" ]; then
  cat "$GAM_MOCK_FIXTURES/resources.json"
  exit 0
fi

# `gam all users print calendars ...` -> real GAM shape: CSV with a `primaryEmail` sibling column
# next to the per-row `JSON` blob (the owning user is NOT inside the JSON). Used for name search.
if [ "${1:-}" = "all" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendars" ]; then
  cat "$GAM_MOCK_FIXTURES/all_calendars.csv"
  exit 0
fi

# `gam user <email> print calendars ...` -> NDJSON of a user's calendars.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendars" ]; then
  cat "$GAM_MOCK_FIXTURES/user_calendars.json"
  exit 0
fi

# `gam calendars <id> print calendaracls|events ...` -> NDJSON (standalone calendar form).
if [ "${1:-}" = "calendars" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendaracls" ]; then
  case "${2:-}" in
    *orphan*) cat "$GAM_MOCK_FIXTURES/calendar_acls_orphan.json" ;;  # sole owner is a suspended user
    *)        cat "$GAM_MOCK_FIXTURES/calendar_acls.json" ;;
  esac
  exit 0
fi
if [ "${1:-}" = "calendars" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "events" ]; then
  cat "$GAM_MOCK_FIXTURES/events.json"
  exit 0
fi

# `gam user <admin> check serviceaccount` -> simulate a fully-authorized service account.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "check" ] && [ "${4:-}" = "serviceaccount" ]; then
  cat <<'EOF'
System time status: PASS
Service account private key authentication: PASS
https://www.googleapis.com/auth/admin.directory.user: PASS
https://www.googleapis.com/auth/admin.directory.group: PASS
https://www.googleapis.com/auth/gmail.settings.basic: PASS
All scopes PASS
EOF
  exit 0
fi

# `gam print datatransfers olduser <email>` -> CSV; a *pending* user has an in-flight transfer.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "datatransfers" ]; then
  case "${4:-}" in
    *pending*) cat "$GAM_MOCK_FIXTURES/datatransfers_pending.csv" ;;
    *)         printf 'id,oldOwnerUserEmail,newOwnerUserEmail,overallTransferStatusCode,application\n' ;;
  esac
  exit 0
fi

# `gam user <assignee> create tasklist title <t> returnidonly` -> echo a fake tasklist id.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "create" ] && [ "${4:-}" = "tasklist" ]; then
  echo "MockTasklist_abc123"
  exit 0
fi

# Anything else is treated as a mutation: optionally simulate a token refresh, then succeed.
if [ -n "${GAM_MOCK_REFRESH:-}" ] && [ -n "${GAMCFGDIR:-}" ] && [ -f "$GAMCFGDIR/oauth2.txt" ]; then
  printf 'refreshed-token-payload\n' > "$GAMCFGDIR/oauth2.txt"
fi
echo "ok"
exit 0
