#!/bin/sh
# Mock `gam` binary for offline tests. Behavior is driven by the argument vector and env vars set by
# the test:
#   GAM_MOCK_FIXTURES  - directory holding the *.json fixtures to echo back
#   GAM_MOCK_REFRESH   - if set, simulate an OAuth token refresh by rewriting oauth2.txt in GAMCFGDIR
#   GAM_MOCK_ARGV_LOG  - if set, append every invocation's argv to this file (tests/helpers.py reads it)
#   GAM_MOCK_STATE     - if set, a directory where the mock keeps settings between calls the way GAM
#                        merges into them (`vacation`; the `gam_state` fixture)
#
# The mock must fail the way real GAM fails: one more permissive than GAM turns a live break into a
# green test (CLAUDE.md, "the mock lies"). So:
#   - every call except `version` must find the credentials materialized in $GAMCFGDIR;
#   - each write the app issues has a STRICT handler accepting only the argv shape the vendored grammar
#     (gamgui/resources/gam7/GamCommands.txt) allows for it, and failing a malformed one the way GAM
#     does a usage error (echo the command, "ERROR: ...", exit 2);
#   - anything unhandled FAILS. Add a handler for a new command; never make the catch-all succeed.
# Failure triggers, by argument substring: *missing*/*nonexistent* -> "Does not exist" for the user,
# group, calendar, event or delegate; *exists* -> 409 on create; plus SENDFAIL, SUBFAIL, CONFLICT409,
# FAILME, OWNACL, SWEEPFAIL, SWEEPBENIGN, SWEEPMIXED, SWEEPSLOW, SIGNOUTFAIL, FWDFAIL (see each handler). The stderr wording
# and exit codes are GAM7's shape (2 usage error, 50 action failed, 51 action not performed, 56 does
# not exist) written from its source conventions, not captured from a tenant — only a live capture
# (plan Phase 8) proves them.

set -eu

if [ -n "${GAM_MOCK_ARGV_LOG:-}" ]; then
  # NUL-separated (argv can't contain NUL): the arg count, then each arg.
  printf '%s\0' "$#" "$@" >> "$GAM_MOCK_ARGV_LOG"
fi

ARGS="$*"

usage_error() {  # GAM's usage error: the command line, then the reason; USAGE_ERROR_RC
  printf 'Command: gam %s\n\nERROR: %s\n' "$ARGS" "$1" 1>&2
  exit 2
}
missing_arg() { usage_error "Missing argument: Expected <$1>"; }
invalid_arg() { usage_error "Invalid argument: $1"; }
invalid_choice() { usage_error "Invalid choice ($1): Expected <$2>"; }
does_not_exist() {  # <entity> <name>; ENTITY_DOES_NOT_EXIST_RC
  printf '%s: %s, Does not exist\n' "$1" "$2" 1>&2
  exit 56
}
check_exists() {  # <entity> <name>: the *missing*/*nonexistent* trigger
  case "$2" in *missing*|*nonexistent*) does_not_exist "$1" "$2" ;; esac
}

is_bool() { case "${1:-}" in true|on|yes|enabled|1|false|off|no|disabled|0) return 0 ;; esac; return 1; }
bool_word() { case "$1" in true|on|yes|enabled|1) echo True ;; *) echo False ;; esac; }   # as GAM shows it
need_bool() {
  [ -n "${1:-}" ] || missing_arg "Boolean"
  is_bool "$1" || invalid_choice "$1" "true|on|yes|enabled|1|false|off|no|disabled|0"
}
need_value() { [ "$1" -ge 2 ] || missing_arg "$2"; }  # <remaining-arg-count> <what>: a keyword's value
need_date() {  # <Date> ::= YYYY-MM-DD | (+|-)N(d|w|y) | never | today
  case "${1:-}" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[+-][0-9]*[dwy]|never|today) ;;
    "") missing_arg "Date" ;;
    *) usage_error "Invalid date ($1): Expected <YYYY-MM-DD>" ;;
  esac
}
ACL_ROLES="editor|freebusy|freebusyreader|owner|reader|writer|writerwithoutprivateaccess|none"
is_acl_role() { case "${1:-}" in editor|freebusy|freebusyreader|owner|reader|writer|writerwithoutprivateaccess|none) return 0 ;; esac; return 1; }
need_acl_role() {
  [ -n "${1:-}" ] || missing_arg "CalendarACLRole"
  is_acl_role "$1" || invalid_choice "$1" "$ACL_ROLES"
}
# [sendnotifications <Boolean>] — the only thing an ACL add may carry after its scope.
acl_notify_tail() {
  if [ $# -eq 0 ]; then return 0; fi
  [ "$1" = "sendnotifications" ] || invalid_arg "$1"
  need_bool "${2:-}"
  [ $# -eq 2 ] || invalid_arg "$3"
}

case "${1:-}" in
  version)
    echo "GAM 7.48.11 - mock"
    exit 0
    ;;
esac

# Every other call is authenticated: GamGUI must have materialized the Keychain credentials into a
# private per-call GAMCFGDIR (never ~/.gam), and GAM can't act without its credential files. (The
# wording and exit code of GAM's missing-file error are approximate.)
if [ -z "${GAMCFGDIR:-}" ]; then
  echo "ERROR: mock: GAMCFGDIR is not set - an authenticated call escaped the ephemeral config" 1>&2
  exit 2
fi
if [ ! -s "$GAMCFGDIR/oauth2service.json" ]; then
  printf 'ERROR: Service Account OAuth2 File: %s, Does not exist\n' "$GAMCFGDIR/oauth2service.json" 1>&2
  exit 12
fi
if [ ! -s "$GAMCFGDIR/oauth2.txt" ]; then
  printf 'ERROR: Client OAuth2 File: %s, Does not exist\nPlease run: gam oauth create\n' "$GAMCFGDIR/oauth2.txt" 1>&2
  exit 12
fi
# GAM rewrites oauth2.txt whenever it refreshes the access token, on any authenticated call.
if [ -n "${GAM_MOCK_REFRESH:-}" ]; then
  printf 'refreshed-token-payload\n' > "$GAMCFGDIR/oauth2.txt"
fi

case "${1:-}" in
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
  MOCKSLEEP)
    # MOCKSLEEP <seconds> [pidfile] -> a call that hangs (a stalled API request), so the runner's
    # timeout/kill path runs. exec keeps this PID, so the pidfile names the process that must die.
    if [ -n "${3:-}" ]; then echo "$$" > "$3"; fi
    exec sleep "${2:-30}"
    ;;
esac

# Read commands -> echo the matching fixture.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "users" ]; then
  cat "$GAM_MOCK_FIXTURES/print_users.json"
  exit 0
fi
# `gam info user <addr>` -> THAT user's record, keyed on the address like GAM. An address that isn't in
# the directory fails the way GAM does — returning Alice for anyone was the same lie as the
# group-members bug in the failure log.
if [ "${1:-}" = "info" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  case "$3" in
    alice@example.com) cat "$GAM_MOCK_FIXTURES/info_user.json" ;;
    bob@example.com)   cat "$GAM_MOCK_FIXTURES/info_user_suspended.json" ;;
    *) grep -F "\"primaryEmail\": \"$3\"" "$GAM_MOCK_FIXTURES/print_users.json" || does_not_exist "User" "$3" ;;
  esac
  exit 0
fi
# `gam print group-members group <addr>` -> members, but ONLY for an address that is really a group.
# Real GAM fails for a user's address, and code that has to tell a person from a group (calendar
# sharing fans a group grant out to its members) would otherwise pass here and break live.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "group-members" ]; then
  case "$*" in
    *sales@example.com*|*staff@example.com*|*it@example.com*|*team@example.com*)
      cat "$GAM_MOCK_FIXTURES/group_members.json" ;;
    *empty-group@example.com*)
      printf 'email\n' ;;          # a real, but memberless, group
    *)
      echo "ERROR: 400: Bad Request - notFound: Resource Not Found: groupKey" 1>&2; exit 1 ;;
  esac
  exit 0
fi

# `gam user <email> show vacation` (no formatjson) -> parseable text, like real GAM. With GAM_MOCK_STATE
# and a stored setting for the user, the stored one in GAM's _showVacation shape (a date, else
# Started/NotSpecified while it is on); otherwise canned.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && [ "${4:-}" = "vacation" ]; then
  vdir="${GAM_MOCK_STATE:-}/vacation/$2"
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -d "$vdir" ]; then
    stored() { if [ -f "$vdir/$1" ]; then cat "$vdir/$1"; else printf '%s' "$2"; fi; }
    on="$(stored enabled False)"
    printf 'User: %s, Vacation:\n  Enabled: %s\n  Contacts Only: %s\n  Domain Only: %s\n' \
      "$2" "$on" "$(stored contactsonly False)" "$(stored domainonly False)"
    if [ -f "$vdir/start" ]; then printf '  Start Date: %s\n' "$(cat "$vdir/start")"
    elif [ "$on" = True ]; then echo "  Start Date: Started"; fi
    if [ -f "$vdir/end" ]; then printf '  End Date: %s\n' "$(cat "$vdir/end")"
    elif [ "$on" = True ]; then echo "  End Date: NotSpecified"; fi
    printf '  Subject: %s\n  Message:\n' "$(stored subject None)"
    stored message None | sed 's/^/    /'; echo
    exit 0
  fi
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

# `gam print cros ...` -> formatjson NDJSON of ChromeOS devices (Builder: Find Chromebooks).
if [ "${1:-}" = "print" ] && [ "${2:-}" = "cros" ]; then
  printf '%s\n' \
    '{"deviceId":"cros_1","serialNumber":"5CD123","status":"ACTIVE","orgUnitPath":"/Students","annotatedAssetId":"AST-1","annotatedUser":"sam@example.com","model":"HP Chromebook 11"}' \
    '{"deviceId":"cros_2","serialNumber":"5CD999","status":"DEPROVISIONED","orgUnitPath":"/Staff","annotatedAssetId":"AST-2","annotatedUser":"","model":"Acer Chromebook 314"}'
  exit 0
fi

# `gam user <email> print filelist ...` -> formatjson NDJSON of Drive files (Builder: Find Drive files).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "filelist" ]; then
  printf '%s\n' \
    '{"id":"file_1","name":"Q4 Budget","mimeType":"application/vnd.google-apps.spreadsheet","owners":"alice@example.com","modifiedTime":"2026-06-20T10:00:00Z"}' \
    '{"id":"file_2","name":"Team Folder","mimeType":"application/vnd.google-apps.folder","owners":"alice@example.com","modifiedTime":"2026-05-01T09:00:00Z"}'
  exit 0
fi

# `gam <UserTypeEntity> show backupcodes|verificationcodes` (no options) -> text; a Builder sensitive
# read. The canned codes are what the audit must never contain.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && { [ "${4:-}" = "backupcodes" ] || [ "${4:-}" = "verificationcodes" ]; }; then
  [ $# -eq 4 ] || invalid_arg "$5"
  check_exists "User" "$2"
  printf 'User: %s, Backup Verification Codes (2)\n  1: 11112222\n  2: 33334444\n' "$2"
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

# ================================ writes (strict) ================================================

# <UserAttribute>* plus the notify clause, as `gam create|add user` / `gam update user` take them. Only
# the attributes GamGUI emits are modelled; anything else is an invalid argument.
check_user_attrs() {
  while [ $# -gt 0 ]; do
    case "$1" in
      suspended|suspend|changepassword|changepasswordatnextlogin|archived|archive)
        need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      password)
        need_value $# "Password"
        case "$2" in
          random|uniquerandom) shift 2; case "${1:-}" in [0-9]*) shift ;; esac ;;
          *) shift 2 ;;
        esac ;;
      firstname|givenname|lastname|familyname|displayname|recoveryemail|recoveryphone|ou|org|orgunitpath)
        need_value $# "String"; shift 2 ;;
      notify|subject|notifypassword|from|mailbox|replyto|message|textmessage|htmlmessage)
        need_value $# "String"; shift 2 ;;
      notifyrecoveryemail|ignorenullpassword) shift ;;
      html) shift; if is_bool "${1:-}"; then shift; fi ;;
      organization)
        shift
        if [ "${1:-}" = "clear" ]; then shift; continue; fi
        while :; do  # (organization [<field> <String>]* notprimary|primary)
          case "${1:-}" in
            type|customtype|name|title|department|symbol|costcenter|location|description|domain|fulltimeequivalent)
              need_value $# "String"; shift 2 ;;
            primary|notprimary) shift; break ;;
            "") missing_arg "notprimary|primary" ;;
            *) invalid_arg "$1" ;;
          esac
        done ;;
      *) invalid_arg "$1" ;;
    esac
  done
}

# `gam create user <email> <UserAttribute>* [notify ...]` -> new account. Real GAM fails with 409 if the
# account already exists; an *exists* email mirrors that so onboarding can't silently "succeed" on a
# duplicate.
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "EmailAddress"
  user="$3"; shift 3
  check_user_attrs "$@"
  case "$user" in
    *exists*) echo "ERROR: 409: Entity already exists - duplicate: $user" 1>&2; exit 1 ;;
  esac
  echo "User: $user, Added"
  exit 0
fi

# `gam update user <email> <UserAttribute>*` -> organization / suspended / password random.
if [ "${1:-}" = "update" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  user="$3"; shift 3
  check_user_attrs "$@"
  check_exists "User" "$user"
  echo "User: $user, Updated"
  exit 0
fi

# `gam delete user <UserItem> [noactionifalias]` / `gam undelete user <UserItem> [ou|org|orgunit <path>]`
if [ "${1:-}" = "delete" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  case "${4:-}" in ""|noactionifalias) ;; *) invalid_arg "$4" ;; esac
  [ $# -le 4 ] || invalid_arg "$5"
  check_exists "User" "$3"
  echo "User: $3, Deleted"
  exit 0
fi
if [ "${1:-}" = "undelete" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  if [ $# -gt 3 ]; then
    case "$4" in ou|org|orgunit) [ -n "${5:-}" ] || missing_arg "OrgUnitPath" ;; *) invalid_arg "$4" ;; esac
    [ $# -le 5 ] || invalid_arg "$6"
  fi
  check_exists "User" "$3"
  echo "User: $3, Undeleted"
  exit 0
fi

# `gam user <email> signout` (8938). SIGNOUTFAIL: Google refuses users.signOut the way a missing
# admin.directory.user.security scope does — GAM's entityActionFailedWarning, ACTION_FAILED_RC.
signout_refused() {
  printf 'User: %s, Sign Out Failed: Not Authorized to access this resource/api\n' "$1" 1>&2
  exit 50
}
if [ "${1:-}" = "user" ] && [ "${3:-}" = "signout" ]; then
  [ $# -eq 3 ] || invalid_arg "$4"
  check_exists "User" "$2"
  case "$2" in *SIGNOUTFAIL*) signout_refused "$2" ;; esac
  echo "User: $2, Signed Out"
  exit 0
fi

# `gam user <email> deprovision|deprov [popimap] [signout] [turnoff2sv]` (7899): GAM's parser takes the
# three words in any order. Per user it deletes the app passwords, invalidates the backup codes and
# deletes the OAuth tokens, then (options) turns off 2SV, signs out, disables POP/IMAP. A failure is
# reported against the user and stops that user's remaining parts (deprovisionUser, read statically).
# The stdout wording is GAM's shape from its source conventions, not captured from a tenant.
if [ "${1:-}" = "user" ] && { [ "${3:-}" = "deprovision" ] || [ "${3:-}" = "deprov" ]; }; then
  user="$2"; signout=""; shift 3
  while [ $# -gt 0 ]; do
    case "$1" in
      popimap|turnoff2sv) ;;
      signout) signout=1 ;;
      *) invalid_arg "$1" ;;
    esac
    shift
  done
  check_exists "User" "$user"
  echo "User: $user, Application Specific Passwords: 0"
  echo "User: $user, Backup Verification Codes, Invalidated"
  echo "User: $user, Access Tokens: 0"
  if [ -n "$signout" ]; then
    case "$user" in *SIGNOUTFAIL*) signout_refused "$user" ;; esac
    echo "User: $user, Signed Out"
  fi
  echo "User: $user, Deprovisioned"
  exit 0
fi

# `gam user <email> signature <String> [html [<Boolean>]] [replyto <addr>] [default] [treatasalias <B>]
#  [name <String>] [primary]` — the signature text is required (an empty string clears it).
if [ "${1:-}" = "user" ] && { [ "${3:-}" = "signature" ] || [ "${3:-}" = "sig" ]; }; then
  [ $# -ge 4 ] || missing_arg "String"
  user="$2"; shift 4
  while [ $# -gt 0 ]; do
    case "$1" in
      html) shift; if is_bool "${1:-}"; then shift; fi ;;
      default|primary) shift ;;
      replyto|name) need_value $# "String"; shift 2 ;;
      treatasalias) need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  check_exists "User" "$user"
  echo "User: $user, SendAs Address: <$user>, Updated"
  exit 0
fi

# `gam user <email> add|create|delete delegate|delegates [convertalias] <UserEntity>`
if [ "${1:-}" = "user" ] && { [ "${4:-}" = "delegate" ] || [ "${4:-}" = "delegates" ]; }; then
  case "${3:-}" in
    add|create) verb="Add" ;;
    delete) verb="Delete" ;;
    *) echo "ERROR: mock: unhandled argv: $*" 1>&2; exit 2 ;;
  esac
  n=5
  if [ "${5:-}" = "convertalias" ]; then n=6; fi
  eval "delegate=\${$n:-}"
  [ -n "$delegate" ] || missing_arg "UserEntity"
  if [ $# -gt $n ]; then eval "invalid_arg \"\${$((n + 1))}\""; fi
  case "$delegate" in
    *missing*|*nonexistent*)
      printf 'User: %s, Delegate: %s, %s Failed: Does not exist\n' "$2" "$delegate" "$verb" 1>&2; exit 50 ;;
  esac
  echo "User: $2, Delegate: $delegate, ${verb}ed"
  exit 0
fi

# `gam user <email> vacation [<Boolean>] [subject <S>] [message <S>] [html [<B>]] [contactsonly [<B>]]
#  [domainonly [<B>]] [start|startdate <Date>|Started] [end|enddate <Date>|NotSpecified]`
# GAM (setVacation, read from the vendored build) does NOT replace the settings: it reads them
# (getVacation), overwrites only the fields the command names and writes the result back — a flag or
# date left out keeps its old value. With GAM_MOCK_STATE the mock keeps them the same way.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "vacation" ]; then
  user="$2"; shift 3
  v_on=""; v_co=""; v_do=""; v_start=""; v_end=""; v_subject=""; v_message=""; has_subject=""; has_message=""
  if is_bool "${1:-}"; then v_on="$(bool_word "$1")"; shift; fi
  while [ $# -gt 0 ]; do
    case "$1" in
      subject) need_value $# "String"; v_subject="$2"; has_subject=1; shift 2 ;;
      message|textmessage|htmlmessage) need_value $# "String"; v_message="$2"; has_message=1; shift 2 ;;
      html) shift; if is_bool "${1:-}"; then shift; fi ;;
      contactsonly) shift; v_co=True; if is_bool "${1:-}"; then v_co="$(bool_word "$1")"; shift; fi ;;
      domainonly) shift; v_do=True; if is_bool "${1:-}"; then v_do="$(bool_word "$1")"; shift; fi ;;
      start|startdate) need_value $# "Date"; [ "$2" = "Started" ] || need_date "$2"; v_start="$2"; shift 2 ;;
      end|enddate) need_value $# "Date"; [ "$2" = "NotSpecified" ] || need_date "$2"; v_end="$2"; shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  check_exists "User" "$user"
  if [ -n "${GAM_MOCK_STATE:-}" ]; then
    vdir="$GAM_MOCK_STATE/vacation/$user"; mkdir -p "$vdir"
    [ -z "$v_on" ] || printf '%s' "$v_on" > "$vdir/enabled"
    [ -z "$v_co" ] || printf '%s' "$v_co" > "$vdir/contactsonly"
    [ -z "$v_do" ] || printf '%s' "$v_do" > "$vdir/domainonly"
    [ -z "$has_subject" ] || printf '%s' "$v_subject" > "$vdir/subject"
    [ -z "$has_message" ] || printf '%s' "$v_message" > "$vdir/message"
    case "$v_start" in "") ;; Started) rm -f "$vdir/start" ;; *) printf '%s' "$v_start" > "$vdir/start" ;; esac
    case "$v_end" in "") ;; NotSpecified) rm -f "$vdir/end" ;; *) printf '%s' "$v_end" > "$vdir/end" ;; esac
  fi
  echo "User: $user, Vacation: Updated"
  exit 0
fi

# `gam user <email> add|create forwardingaddress|forwardingaddresses <EmailAddressEntity>`
if [ "${1:-}" = "user" ] && { [ "${3:-}" = "add" ] || [ "${3:-}" = "create" ]; } \
    && { [ "${4:-}" = "forwardingaddress" ] || [ "${4:-}" = "forwardingaddresses" ]; }; then
  [ -n "${5:-}" ] || missing_arg "EmailAddressEntity"
  [ $# -eq 5 ] || invalid_arg "$6"
  echo "User: $2, Forwarding Address: $5, Added"
  exit 0
fi

# `gam user <email> forward <FalseValues>` / `forward <TrueValues> <action> <EmailAddress>`
if [ "${1:-}" = "user" ] && [ "${3:-}" = "forward" ]; then
  [ -n "${4:-}" ] || missing_arg "Boolean"
  case "$4" in
    false|off|no|disabled|0) [ $# -eq 4 ] || invalid_arg "$5" ;;
    true|on|yes|enabled|1)
      actions="keep|leaveininbox|archive|delete|trash|markread"
      case "${5:-}" in
        keep|leaveininbox|archive|delete|trash|markread) ;;
        "") missing_arg "$actions" ;;
        *) invalid_choice "$5" "$actions" ;;
      esac
      [ -n "${6:-}" ] || missing_arg "EmailAddress"
      [ $# -eq 6 ] || invalid_arg "$7" ;;
    *) invalid_choice "$4" "true|on|yes|enabled|1|false|off|no|disabled|0" ;;
  esac
  check_exists "User" "$2"
  # FWDFAIL: a user without Gmail — GAM's userServiceNotEnabledWarning, SERVICE_NOT_APPLICABLE_RC (73).
  case "$2" in *FWDFAIL*) printf 'User: %s, Gmail Service/App not enabled\n' "$2" 1>&2; exit 73 ;; esac
  echo "User: $2, Forward: Updated"
  exit 0
fi

# `gam create|add alias|aliases <EmailAddressEntity> user|group|target <UniqueID>|<EmailAddress>
#  [verifynotinvitable]` / `gam delete alias|aliases [user|group|target] <EmailAddressEntity>`
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && { [ "${2:-}" = "alias" ] || [ "${2:-}" = "aliases" ]; }; then
  [ -n "${3:-}" ] || missing_arg "EmailAddressEntity"
  case "${4:-}" in
    user|group|target) ;;
    "") missing_arg "user|group|target" ;;
    *) invalid_choice "$4" "user|group|target" ;;
  esac
  [ -n "${5:-}" ] || missing_arg "EmailAddress"
  case "${6:-}" in ""|verifynotinvitable) ;; *) invalid_arg "$6" ;; esac
  [ $# -le 6 ] || invalid_arg "$7"
  case "$3" in *exists*) echo "ERROR: 409: Entity already exists - duplicate: $3" 1>&2; exit 1 ;; esac
  check_exists "User" "$5"
  echo "User: $5, Alias: $3, Added"
  exit 0
fi
if [ "${1:-}" = "delete" ] && { [ "${2:-}" = "alias" ] || [ "${2:-}" = "aliases" ]; }; then
  alias="${3:-}"
  case "$alias" in user|group|target) alias="${4:-}"; n=4 ;; *) n=3 ;; esac
  [ -n "$alias" ] || missing_arg "EmailAddressEntity"
  if [ $# -gt $n ]; then eval "invalid_arg \"\${$((n + 1))}\""; fi
  check_exists "Alias" "$alias"
  echo "Alias: $alias, Deleted"
  exit 0
fi

# `gam create|add group <EmailAddress> <GroupAttribute>*` (GamGUI sends name/description only).
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && [ "${2:-}" = "group" ]; then
  [ -n "${3:-}" ] || missing_arg "EmailAddress"
  group="$3"; shift 3
  while [ $# -gt 0 ]; do
    case "$1" in
      name|description) need_value $# "String"; shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  case "$group" in *exists*) echo "ERROR: 409: Entity already exists - duplicate: $group" 1>&2; exit 1 ;; esac
  echo "Group: $group, Added"
  exit 0
fi

# `gam update group <group> add|create [<GroupRole>] <member>` / `delete|remove [<GroupRole>] <member>`.
# Real GAM 404s if the group doesn't exist (a typo'd group address is the common onboarding mistake).
if [ "${1:-}" = "update" ] && { [ "${2:-}" = "group" ] || [ "${2:-}" = "groups" ]; }; then
  [ -n "${3:-}" ] || missing_arg "GroupEntity"
  group="$3"
  case "${4:-}" in
    add|create) verb="added" ;;
    delete|remove) verb="removed" ;;
    *) echo "ERROR: mock: unhandled argv: $*" 1>&2; exit 2 ;;
  esac
  shift 4
  role="member"
  case "${1:-}" in owner|manager|member) role="$1"; shift ;; esac
  [ $# -ge 1 ] || missing_arg "UserItem"
  [ $# -eq 1 ] || invalid_arg "$2"
  case "$group" in
    *missing*|*nonexistent*) echo "ERROR: 404: Resource Not Found: groupKey - notFound" 1>&2; exit 1 ;;
  esac
  echo "Group: $group, $verb $1 as $role"
  exit 0
fi

# `gam user <email> add|create calendaracls <UserCalendarEntity> <CalendarACLRole> <scope>
#  [sendnotifications <B>]` / `delete calendaracls <UserCalendarEntity> [<CalendarACLRole>] <scope>`
if [ "${1:-}" = "user" ] && [ "${4:-}" = "calendaracls" ]; then
  user="$2"; op="${3:-}"
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  cal="$5"; shift 5
  case "$op" in
    add|create)
      need_acl_role "${1:-}"
      [ -n "${2:-}" ] || missing_arg "CalendarACLScopeEntity"
      scope="$2"; verb="Added"; shift 2
      acl_notify_tail "$@" ;;
    delete)
      if [ $# -eq 2 ]; then need_acl_role "$1"; shift; fi
      [ $# -ge 1 ] && [ -n "$1" ] || missing_arg "CalendarACLScopeEntity"
      [ $# -eq 1 ] || invalid_arg "$2"
      scope="$1"; verb="Deleted" ;;
    *) echo "ERROR: mock: unhandled argv: $ARGS" 1>&2; exit 2 ;;
  esac
  check_exists "User" "$user"
  echo "User: $user, Calendar: $cal, Calendar ACL: (Scope: $scope), $verb"
  exit 0
fi

# `gam calendars <id> add|create calendaracls <CalendarACLRole> <scope> [sendnotifications <B>]` /
# `gam calendars <id> delete calendaracls [<CalendarACLRole>] <scope>` — standalone admin share/unshare.
if { [ "${1:-}" = "calendars" ] || [ "${1:-}" = "calendar" ]; } && { [ "${4:-}" = "calendaracls" ] || [ "${4:-}" = "acls" ]; }; then
  [ -n "${2:-}" ] || missing_arg "CalendarEntity"
  cal="$2"; op="${3:-}"; shift 4
  case "$op" in
    add|create)
      need_acl_role "${1:-}"
      [ -n "${2:-}" ] || missing_arg "CalendarACLScopeEntity"
      scope="$2"; verb="Added"; shift 2
      acl_notify_tail "$@" ;;
    delete)
      if [ $# -eq 2 ]; then need_acl_role "$1"; shift; fi
      [ $# -ge 1 ] && [ -n "$1" ] || missing_arg "CalendarACLScopeEntity"
      [ $# -eq 1 ] || invalid_arg "$2"
      scope="$1"; verb="Deleted" ;;
    *) echo "ERROR: mock: unhandled argv: $ARGS" 1>&2; exit 2 ;;
  esac
  check_exists "Calendar" "$cal"
  echo "Calendar: $cal, Calendar ACL: (Scope: $scope), $verb"
  exit 0
fi

# `gam all users delete calendaracls <UserCalendarEntity> [<CalendarACLRole>] <scope>` -> the offboarding
# sweep. Succeeds by default. OWNACL: the sweep also hits the departing user's OWN primary calendar and
# Google refuses to remove their owner ACL — the EXACT real stderr (leading spaces preserved), exit 50,
# which the connector tolerates. SWEEPFAIL: a real failure it must not tolerate. SWEEPBENIGN: a multi-user
# stderr (GAM's "Getting all/Got N" chatter, as in GamUpdate.txt, then one line per entity) where every
# line is tolerable; SWEEPMIXED: the same with one real per-user failure among them, exit 50 either way.
# SWEEPSLOW: a sweep still walking the domain when the runner's timeout fires (exec keeps the PID it kills).
if [ "${1:-}" = "all" ] && [ "${2:-}" = "users" ] && [ "${3:-}" = "delete" ] && [ "${4:-}" = "calendaracls" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  shift 5
  if [ $# -eq 2 ]; then need_acl_role "$1"; shift; fi
  [ $# -ge 1 ] && [ -n "$1" ] || missing_arg "CalendarACLScopeEntity"
  [ $# -eq 1 ] || invalid_arg "$2"
  scope="$1"
  case "$scope" in
    *SWEEPSLOW*) exec sleep 30 ;;
    *OWNACL*)
      printf '    Calendar: %s, Calendar ACL: (Scope: user:%s), Delete Failed: Cannot change your own access level.\n' "$scope" "$scope" 1>&2
      exit 50 ;;
    *SWEEPFAIL*)
      echo "ERROR: 403: Request had insufficient authentication scopes" 1>&2; exit 1 ;;
    *SWEEPBENIGN*|*SWEEPMIXED*)
      printf 'Getting all Users, may take some time on a large Google Workspace Account...\nGot 4 Users: alice@example.com - %s\n' "$scope" 1>&2
      echo "User: bob@example.com, Service not applicable/Does not exist" 1>&2
      # A user without Calendar: GAM's userCalServiceNotEnabledWarning (calendars.get primary →
      # notACalendarUser), SERVICE_NOT_APPLICABLE_RC — `all users` includes every active user.
      echo "User: dave@example.com, Calendar Service/App not enabled (3/4)" 1>&2
      case "$scope" in *SWEEPMIXED*)
        printf '    Calendar: carol@example.com, Calendar ACL: (Scope: user:%s), Delete Failed: Internal error encountered.\n' "$scope" 1>&2 ;;
      esac
      printf '    Calendar: %s, Calendar ACL: (Scope: user:%s), Delete Failed: Cannot change your own access level.\n' "$scope" "$scope" 1>&2
      exit 50 ;;
  esac
  printf 'User: alice@example.com, Calendar: alice@example.com, Calendar ACL: (Scope: user:%s), Deleted\n' "$scope"
  exit 0
fi

# `gam user <owner> remove calendars <UserCalendarEntity>` -> PERMANENTLY delete a secondary calendar.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "remove" ] && [ "${4:-}" = "calendars" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  [ $# -eq 5 ] || invalid_arg "$6"
  check_exists "Calendar" "$5"
  echo "User: $2, Calendar: $5, Removed"
  exit 0
fi

# `gam user <email> add calendars <id> <CalendarAttribute>*` -> subscribe so the calendar appears in the
# sidebar. A SUBFAIL target simulates GAM refusing to act as that recipient (partial-success path).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "add" ] && [ "${4:-}" = "calendars" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarAddEntity"
  case "$*" in
    *SUBFAIL*) echo "ERROR: 403: Not authorized to act as user" 1>&2; exit 1 ;;
  esac
  user="$2"; cal="$5"; shift 5
  while [ $# -gt 0 ]; do
    case "$1" in
      selected|hidden) need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      color|colorindex|colorid|backgroundcolor|foregroundcolor|summary) need_value $# "String"; shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  echo "User $user subscribed to calendar $cal"
  exit 0
fi

# `gam user <email> add|create event <UserCalendarEntity> [id <S>] <EventAttribute>+ [<notification>]`
if [ "${1:-}" = "user" ] && { [ "${3:-}" = "add" ] || [ "${3:-}" = "create" ]; } && [ "${4:-}" = "event" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  user="$2"; cal="$5"; shift 5
  attrs=0
  while [ $# -gt 0 ]; do
    case "$1" in
      id) need_value $# "String"; shift 2 ;;
      summary|description|location|attendee|optionalattendee|timezone|color|recurrence|resource)
        need_value $# "String"; shift 2; attrs=$((attrs + 1)) ;;
      start|starttime|end|endtime)
        need_value $# "Time"
        if [ "$2" = "allday" ]; then need_date "${3:-}"; shift 3; else shift 2; fi
        attrs=$((attrs + 1)) ;;
      allday|birthday) need_value $# "Date"; need_date "$2"; shift 2; attrs=$((attrs + 1)) ;;
      available|tentative|googlemeet|hangoutsmeet|noreminders) shift; attrs=$((attrs + 1)) ;;
      notifyattendees) shift ;;
      sendnotifications) need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      sendupdates)
        case "${2:-}" in all|externalonly|none) ;; *) invalid_choice "${2:-}" "all|externalonly|none" ;; esac
        shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  [ "$attrs" -gt 0 ] || missing_arg "EventAttribute"
  check_exists "User" "$user"
  echo "User: $user, Calendar: $cal, Event: MockEvent_1, Added"
  exit 0
fi

# `gam calendars <id> delete events [<EventEntity>] [doit] [<EventNotificationAttribute>]`. Without `doit`
# GAM only reports what it would delete and does NOT delete — an app that forgot `doit` would show
# "Event deleted." for an event that still exists — so a doit-less delete fails here.
if { [ "${1:-}" = "calendars" ] || [ "${1:-}" = "calendar" ]; } && [ "${3:-}" = "delete" ] && [ "${4:-}" = "events" ]; then
  [ -n "${2:-}" ] || missing_arg "CalendarEntity"
  cal="$2"; shift 4
  eid=""; doit=""
  while [ $# -gt 0 ]; do
    case "$1" in
      id|eventid|event|events) need_value $# "EventId"; eid="$2"; shift 2 ;;
      doit) doit=1; shift ;;
      notifyattendees) shift ;;
      sendnotifications) need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      sendupdates)
        case "${2:-}" in all|externalonly|none) ;; *) invalid_choice "${2:-}" "all|externalonly|none" ;; esac
        shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  if [ -z "$eid" ]; then
    echo "ERROR: mock: delete events with no event id would select every event - never emitted" 1>&2; exit 2
  fi
  if [ -z "$doit" ]; then
    printf 'Calendar: %s, Event: %s, Delete not performed: Use doit argument to perform action\n' "$cal" "$eid" 1>&2
    exit 51
  fi
  check_exists "Event" "$eid"
  echo "Calendar: $cal, Event: $eid, Deleted"
  exit 0
fi

# `gam user <assignee> create tasklist <TasklistAttribute>* [returnidonly] [formatjson]` -> with
# returnidonly, GAM prints just the new tasklist id.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "create" ] && [ "${4:-}" = "tasklist" ]; then
  user="$2"; shift 4
  idonly=""
  while [ $# -gt 0 ]; do
    case "$1" in
      title) need_value $# "String"; shift 2 ;;
      returnidonly) idonly=1; shift ;;
      formatjson) shift ;;
      *) invalid_arg "$1" ;;
    esac
  done
  check_exists "User" "$user"
  if [ -n "$idonly" ]; then echo "MockTasklist_abc123"; else echo "User: $user, Tasklist: MockTasklist_abc123, Added"; fi
  exit 0
fi

# `gam user <a> create task <TasklistEntity> <TaskAttribute>* [parent <id>] [previous <id>]` -> a task on
# the list. Real GAM 404s if the tasklist id doesn't exist, so only the id our create-tasklist handler
# returned succeeds — before this, ANY id passed and "Created N of N" proved nothing (the mock lied).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "create" ] && [ "${4:-}" = "task" ]; then
  [ -n "${5:-}" ] || missing_arg "TasklistEntity"
  user="$2"; list="$5"; shift 5
  while [ $# -gt 0 ]; do
    case "$1" in
      title|notes|due|parent|previous) need_value $# "String"; shift 2 ;;
      status)
        case "${2:-}" in needsaction|completed) ;; *) invalid_choice "${2:-}" "needsaction|completed" ;; esac
        shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  case "$list" in
    MockTasklist_abc123) echo "User: $user, Task: MockTask created" ;;
    *) echo "ERROR: 404: Tasklist not found - notFound" 1>&2; exit 1 ;;
  esac
  exit 0
fi

# `gam sendemail [recipient|to] <RecipientEntity> [subject <S>] [message <S>] [html [<B>]] ...` -> send.
# Real GAM 400s on a bad recipient, so a *SENDFAIL* address fails like an invalid `to` header.
if [ "${1:-}" = "sendemail" ]; then
  shift
  case "${1:-}" in recipient|to) shift ;; esac
  [ -n "${1:-}" ] || missing_arg "RecipientEntity"
  to="$1"; shift
  while [ $# -gt 0 ]; do
    case "$1" in
      from|mailbox|replyto|cc|bcc|subject|message|textmessage|htmlmessage) need_value $# "String"; shift 2 ;;
      html) shift; if is_bool "${1:-}"; then shift; fi ;;
      singlemessage) shift ;;
      *) invalid_arg "$1" ;;
    esac
  done
  case "$to" in
    *SENDFAIL*) echo "ERROR: 400: Bad Request - invalidArgument: Invalid to header" 1>&2; exit 1 ;;
  esac
  echo "Email sent to $to"
  exit 0
fi

# `gam create|add datatransfer|transfer <old> <DataTransferServiceList> <new> [private|shared|all]
#  [release_resources]` -> succeeds. An <old> containing CONFLICT409 (a second transfer for the same
# user still in flight) returns the exact "already in progress" error Google emits.
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && { [ "${2:-}" = "datatransfer" ] || [ "${2:-}" = "transfer" ]; }; then
  [ -n "${3:-}" ] || missing_arg "OldOwnerID"
  [ -n "${4:-}" ] || missing_arg "DataTransferServiceList"
  services="calendar|datastudio|lookerstudio|data studio|googledrive|gdrive|drive|drive and docs"
  old_ifs=$IFS; IFS=,
  for svc in $4; do
    case "$svc" in
      calendar|datastudio|lookerstudio|"data studio"|googledrive|gdrive|drive|"drive and docs") ;;
      *) IFS=$old_ifs; invalid_choice "$svc" "$services" ;;
    esac
  done
  IFS=$old_ifs
  [ -n "${5:-}" ] || missing_arg "NewOwnerID"
  old="$3"; svcs="$4"; new="$5"; shift 5
  while [ $# -gt 0 ]; do
    case "$1" in private|shared|all|release_resources) shift ;; *) invalid_arg "$1" ;; esac
  done
  case "$old" in
    *CONFLICT409*) echo "ERROR: 409: conflict - Data transfer already in progress for the user." 1>&2; exit 9 ;;
  esac
  check_exists "User" "$old"
  check_exists "User" "$new"
  echo "Requested transfer of $svcs from $old to $new"
  exit 0
fi

# Anything else is unhandled. Fail loudly: a catch-all that succeeded is how the mock used to lie — a
# `delete events` without doit, an invalid calendar role and 23 writes with no handler all "passed".
echo "ERROR: mock: unhandled argv: $*" 1>&2
exit 2
