#!/bin/sh
# Mock `gam` binary for offline tests. Behavior is driven by the argument vector and env vars set by
# the test:
#   GAM_MOCK_FIXTURES  - directory holding the *.json fixtures to echo back
#   GAM_MOCK_REFRESH   - if set, simulate an OAuth token refresh by rewriting oauth2.txt in GAMCFGDIR
#   GAM_MOCK_ARGV_LOG  - if set, append every invocation's argv to this file (tests/helpers.py reads it)
#   GAM_MOCK_STATE     - if set, a directory where the mock keeps settings between calls the way GAM
#                        merges into them (`vacation`; the `gam_state` fixture). A number in its
#                        `new_account_lag` file makes each account `create user` makes "still being set
#                        up by Google" for that many per-user calls (signature, calendar subscribe, task
#                        list), each failing as it did live — see still_provisioning. (A file, not an env
#                        var: the runner passes gam only an allowlisted environment.)
#
# The mock must fail the way real GAM fails: one more permissive than GAM turns a live break into a
# green test (CLAUDE.md, "the mock lies"). So:
#   - every call except `version` must find the credentials materialized in $GAMCFGDIR;
#   - each write the app issues has a STRICT handler accepting only the argv shape the vendored grammar
#     (gamgui/resources/gam7/GamCommands.txt) allows for it, and failing a malformed one the way GAM
#     does a usage error (echo the command, "ERROR: ...", exit 2);
#   - anything unhandled FAILS. Add a handler for a new command; never make the catch-all succeed.
# Failure triggers, by argument substring: *missing*/*nonexistent* -> "Does not exist" for the user,
# group, calendar, event or delegate; *exists* -> "Create Failed: Duplicate" on create, and "already exists" on a
# delegate add;
# plus USERLIMIT (no license left for a new account), SENDFAIL, SUBFAIL, CONFLICT409,
# FAILME, OWNACL, SWEEPFAIL, SWEEPBENIGN, SWEEPMIXED, SWEEPSLOW, SIGNOUTFAIL, FWDFAIL (see each handler). The stderr wording
# and exit codes are GAM7's shape (2 usage error, 50 action failed, 51 action not performed, 56 does
# not exist) written from its source conventions, not captured from a tenant — only a live capture
# (plan Phase 8) proves them.
#
# The per-user reads (info user, show vacation/signature, print delegates, print groups member, user
# print calendaracls) answer for THAT user: each fixture user (tests/fixtures/print_users.json) has
# different data, and an address that isn't a user fails as GAM fails. Answering Alice's data for
# anyone let five wrong-target reads pass the whole suite (failure-log 2026-09-23).

set -eu

if [ -n "${GAM_MOCK_ARGV_LOG:-}" ]; then
  # NUL-separated (argv can't contain NUL): the arg count, then each arg.
  printf '%s\0' "$#" "$@" >> "$GAM_MOCK_ARGV_LOG"
fi

ARGS="$*"

# GAM finds no gam.cfg in the private GAMCFGDIR the runner hands it per call, so it initializes one and says so on
# stdout, before any command (_writeGamCfgFile, Act.INITIALIZE). Every read comes through the runner's strip of this.
if [ -n "${GAMCFGDIR:-}" ]; then
  printf 'Created: %s/gamcache\nConfig File: %s/gam.cfg, Initialized\n' "$GAMCFGDIR" "$GAMCFGDIR"
fi

usage_error() {  # GAM's usage error (usageErrorExit): the command line, the reason, where help is; USAGE_ERROR_RC
  printf 'Command: gam %s\n\nERROR: %s\n' "$ARGS" "$1" 1>&2
  printf 'Help: Syntax in file %s/GamCommands.txt\nHelp: Documentation is at https://github.com/GAM-team/GAM/wiki\n' \
    "$(dirname "$0")" 1>&2
  exit 2
}
missing_arg() { usage_error "Missing argument: Expected <$1>"; }
empty_arg() { usage_error "Empty argument: Expected <Non-empty $1>"; }
invalid_arg() { usage_error "Invalid argument: $1"; }
invalid_choice() { usage_error "Invalid choice ($1): Expected <$2>"; }
does_not_exist() {  # <entity> <name>; ENTITY_DOES_NOT_EXIST_RC (entityDoesNotExistWarning, a per-entity loop)
  printf '%s: %s, Does not exist\n' "$1" "$2" 1>&2
  exit 56
}
does_not_exist_exit() {  # <entity> <name>: a lookup that ends the command (entityDoesNotExistExit): the command
  # line first, as a usage error has it (the bad argument's >>>marking<<< isn't modelled), then the error
  printf 'Command: gam %s\n\nERROR: %s: %s, Does not exist\n' "$ARGS" "$1" "$2" 1>&2
  exit 56
}
check_exists() {  # <entity> <name>: the *missing*/*nonexistent* trigger
  case "$2" in *missing*|*nonexistent*) does_not_exist "$1" "$2" ;; esac
}

in_directory() { grep -qF "\"primaryEmail\": \"$1\"" "$GAM_MOCK_FIXTURES/print_users.json"; }

# GAM writes every CSV in its setDialect: quotes doubled AND a backslash escape character (unless
# csv_output_no_escape_char, off by default), so a backslash is written doubled and a quote inside a JSON
# string comes out as \\"". The mock writes the same bytes, or a reader without the escape character passes
# here and drops rows live (failure log 2026-10-30). tests/test_gam_csv_dialect.py holds gam_cell to
# Python's csv writer in GAM's dialect. A value here is one line; one with CR or LF isn't modelled.
CR=$(printf '\r')
gam_cell() {  # <value>: one CSV field as GAM writes it
  case "$1" in *"$CR"*|*'
'*) echo "ERROR: mock: a CSV value with a line break isn't modelled" 1>&2; exit 2 ;; esac
  escaped=$(printf '%s' "$1" | LC_ALL=C sed -e 's/\\/\\\\/g' -e 's/"/""/g')
  case "$1" in *[,\"]*) printf '"%s"' "$escaped" ;; *) printf '%s' "$escaped" ;; esac
}
gam_row() {  # <value>...: one CSV row as GAM writes it
  separator=""
  for value in "$@"; do printf '%s' "$separator"; gam_cell "$value"; separator=","; done
  printf '\n'
}
json_text() {  # <key> <json line>: a top-level string value holding no quote or backslash (an address, an ID)
  found=$(printf '%s' "$2" | sed -n "s/.*\"$1\": \"\([^\"\\\\]*\)\".*/\1/p")
  [ -n "$found" ] || { echo "ERROR: mock: no $1 in a data line" 1>&2; exit 2; }
  printf '%s' "$found"
}
# `print … formatjson` (GAM's SetJSONTitles): a header of the key columns and `JSON`, then per record its key
# values and the record. A data file holds one JSON record per line.
print_formatjson() {  # <key columns, space-separated> <data file>
  pf_keys=$1; pf_file=$2
  printf '%s,JSON\n' "$(printf '%s' "$pf_keys" | tr ' ' ',')"
  while IFS= read -r record || [ -n "$record" ]; do
    [ -n "$record" ] || continue
    set --
    for key in $pf_keys; do set -- "$@" "$(json_text "$key" "$record")"; done
    gam_row "$@" "$record"
  done < "$GAM_MOCK_FIXTURES/$pf_file"
}
rows_with() {  # <data file> <value>...: each record in the file with those key values before it
  rw_file=$1; shift
  while IFS= read -r record || [ -n "$record" ]; do
    [ -n "$record" ] || continue
    gam_row "$@" "$record"
  done < "$GAM_MOCK_FIXTURES/$rw_file"
}
# A read shape no handler models is refused, never answered in a guessed one (NDJSON once passed every test here,
# and GAM never prints it).
not_modelled() { echo "ERROR: mock: $1 isn't modelled" 1>&2; exit 2; }
lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
# The users listed, as writeGotMessage writes them: each page's running count after a CR on one line, the last the total. A real
# page is 500 users; here the first is one, so every read of this line meets more than one page.
got_users() { [ "$1" -le 1 ] || printf '\rGot 1 User...' 1>&2; printf '\rGot %s Users...\r\n' "$1" 1>&2; }
json_quote() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
# Under GAM_MOCK_STATE, an account `create user` made is a user from then on. So is anyone a group fixture lists as
# a member: the directory fixture holds three users, but those members are accounts GAM can act as too.
was_created() { [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/created/$1" ]; }
group_member() { cat "$GAM_MOCK_FIXTURES/group_members.json" "$GAM_MOCK_FIXTURES/group_members_allhands.json" | grep -qF "\"email\": \"$1\""; }
calendar_owner() {  # <calendar id>
  key=$(lower "$1")
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$GAM_MOCK_STATE/calendar_owner/$key" ]; then cat "$GAM_MOCK_STATE/calendar_owner/$key"; return 0; fi
  file=$(grep -ilF "\"id\": \"$key\"" "$GAM_MOCK_FIXTURES"/user_owned_calendars/*.json 2>/dev/null | head -n 1)
  [ -n "$file" ] || return 0
  file=${file##*/}; file=${file%.json}; printf '%s' "${file%.hidden}"
}
known_user() { in_directory "$1" || was_created "$1" || group_member "$1"; }
is_group() { grep -qF "\"email\": \"$1\"" "$GAM_MOCK_FIXTURES/groups.json"; }   # <address, lowercased>
# A per-user Gmail/Calendar command acts AS the user, so GAM first needs a token for that address. For
# one that isn't a user the token request fails, and GAM reports the token endpoint's words against it
# (handleOAuthTokenError -> entityActionFailedWarning, ACTION_FAILED_RC; read from the vendored build).
not_a_user() {  # <address> <Show|Print>
  printf 'User: %s, User:, %s Failed: invalid_grant: Invalid email or User ID\n' "$1" "$2" 1>&2
  exit 50
}
need_user() {  # <address> <Show|Print>: a fixture-directory user, or one the mock keeps state for
  known_user "$(lower "$1")" && return 0
  if [ -n "${GAM_MOCK_STATE:-}" ] && { [ -e "$GAM_MOCK_STATE/vacation/$1" ] || [ -e "$GAM_MOCK_STATE/delegates/$1" ]; }; then
    return 0
  fi
  not_a_user "$1" "$2"
}
# A just-created account Google hasn't finished setting up. Live, 2026-09-30, a signature set 1 s after
# `create user` failed "User: x, User Set Failed: access_denied: Requested client not authorized"
# (exit 50) and a calendar subscribe 7 s after it "User: x, Calendar Service/App not enabled" (exit
# 73); both succeed once Google catches up. `create user` arms $GAM_MOCK_STATE/new_account_lag of them.
still_provisioning() {  # <user>: true, one fewer left, while the account is still being set up
  f="${GAM_MOCK_STATE:-}/provisioning/$1"
  { [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$f" ]; } || return 1
  n=$(cat "$f")
  if [ "$n" -le 1 ]; then rm -f "$f"; else echo $((n - 1)) > "$f"; fi
  return 0
}
not_ready_token() {  # <user> <Entity Action>: Google won't issue a token to act as the new account yet
  printf 'User: %s, %s Failed: access_denied: Requested client not authorized\n' "$1" "$2" 1>&2
  exit 50
}
canned_delegates() {  # <user>: each fixture user's own mail delegates, one per line
  case "$1" in
    alice@example.com) printf 'assistant@example.com\nbackup@example.com\n' ;;
    carol@example.com) printf 'helpdesk@example.com\n' ;;
  esac
}
delegates_of() {  # <user>: the current list — with GAM_MOCK_STATE, the stored one once a write touched it
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$GAM_MOCK_STATE/delegates/$1" ]; then
    cat "$GAM_MOCK_STATE/delegates/$1"
  else
    canned_delegates "$1"
  fi
}
has_delegate() { delegates_of "$1" | grep -qixF -- "$2"; }   # Gmail compares addresses case-insensitively
# `todrive <ToDriveAttribute>*` (grammar 655) after a print/report read: only the shape the Builder emits
# (GAMCommands.todrive_args) — `todrive [tduser <EmailAddress>] [tdtitle <String>]`, each once, in that order,
# last on the line. GAM (CSVPrintFile.GetTodriveParameters, read from the vendored build) reads tduser with
# getString, which refuses an empty value, and tdtitle with minLen=0, which doesn't.
todrive_tail() {
  if [ "${1:-}" = "tduser" ]; then
    need_value $# "EmailAddress"
    [ -n "$2" ] || empty_arg "EmailAddress"
    shift 2
  fi
  if [ "${1:-}" = "tdtitle" ]; then need_value $# "String"; shift 2; fi
  [ $# -eq 0 ] || invalid_arg "$1"
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
    echo "GAM 7.50.00 - mock"
    exit 0
    ;;
esac

# Every other call is authenticated: GamGUI must have materialized the Keychain credentials into a
# private per-call GAMCFGDIR (never ~/.gam), and GAM can't act without its credential files. The exit
# codes are the build's (OAUTH2SERVICE_JSON_REQUIRED_RC and OAUTH2_TXT_REQUIRED_RC, both 16; they were
# once a guessed 12); the wording is approximate.
if [ -z "${GAMCFGDIR:-}" ]; then
  echo "ERROR: mock: GAMCFGDIR is not set - an authenticated call escaped the ephemeral config" 1>&2
  exit 2
fi
if [ ! -s "$GAMCFGDIR/oauth2service.json" ]; then
  printf 'ERROR: Service Account OAuth2 File: %s, Does not exist\n' "$GAMCFGDIR/oauth2service.json" 1>&2
  exit 16
fi
if [ ! -s "$GAMCFGDIR/oauth2.txt" ]; then
  printf 'ERROR: Client OAuth2 File: %s, Does not exist\nPlease run: gam oauth create\n' "$GAMCFGDIR/oauth2.txt" 1>&2
  exit 16
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

# A read may end in `todrive …` (the Builder's export to a Sheet). Only a print/report read takes it — the
# grammar's info/show/check lines have none, and the `info user`/`check serviceaccount` handlers ignore
# trailing words, so it is refused here. Check its attributes, then handle the read as without them (the
# canned CSV stays the output; GAM would print the Sheet's URL). A write is left to its strict handler.
case "${1:-}:${3:-}" in
  print:*|report:*|*:print) td_read=print ;;
  info:*|*:show|*:check) td_read=other ;;
  *) td_read="" ;;
esac
if [ -n "$td_read" ]; then
  td=0; i=0
  for a in "$@"; do i=$((i + 1)); if [ "$a" = "todrive" ]; then td=$i; break; fi; done
  if [ "$td" -gt 0 ]; then
    [ "$td_read" = print ] || invalid_arg todrive
    ( shift "$td"; todrive_tail "$@" ) || exit $?
    i=0
    for a in "$@"; do   # keep the words before `todrive` (the list was expanded before the loop)
      i=$((i + 1))
      [ "$i" -gt 1 ] || set --
      [ "$i" -ge "$td" ] || set -- "$@" "$a"
    done
  fi
fi

# Read commands -> echo the matching fixture.
# `gam print users … formatjson` -> GAM's CSV, a `primaryEmail` and a `JSON` column per user, escaped as GAM
# escapes it (carol's title and department need it). Every formatjson read below prints GAM's own key columns,
# read from its source at 7.50.00 (failure log 2026-11-04, the mock's reads and writes).
if [ "${1:-}" = "print" ] && [ "${2:-}" = "users" ]; then
  print_formatjson primaryEmail print_users.json
  exit 0
fi
# `gam info user <addr>` -> THAT user's record, keyed on the address like GAM. An address that isn't in
# the directory fails the way GAM does — returning Alice for anyone was the same lie as the
# group-members bug in the failure log.
# Like the Directory API, an address matches case-insensitively and an ALIAS resolves to the account
# that owns it (Alice's a.anders@) — the record's primaryEmail then differs from what was asked.
if [ "${1:-}" = "info" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  addr=$(printf '%s' "$3" | tr '[:upper:]' '[:lower:]')
  # Phones changed since the directory was read: with GAM_MOCK_STATE, $GAM_MOCK_STATE/phones/<user> (a JSON list) is the
  # record's phones, printed as GAM prints only the fields named. No fixture record has any.
  # $GAM_MOCK_STATE/stray_stdout/<user>: a line that isn't the record first, as GAM prints a config banner there.
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/stray_stdout/$addr" ]; then echo "A notice GAM printed"; fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$GAM_MOCK_STATE/phones/$addr" ]; then
    printf '{"primaryEmail": "%s", "phones": %s}\n' "$addr" "$(tr -d '\n' < "$GAM_MOCK_STATE/phones/$addr")"
    exit 0
  fi
  case "$addr" in
    alice@example.com|a.anders@example.com|alice@alias.example.net) cat "$GAM_MOCK_FIXTURES/info_user.json" ;;
    bob@example.com)   cat "$GAM_MOCK_FIXTURES/info_user_suspended.json" ;;
    admin@example.com) printf '{"id": "100000000000000000009", "primaryEmail": "admin@example.com", "suspended": false}\n' ;;
    # A failure trigger, an account `create user` made, or a group's member is an account too, with an ID of its own.
    *signoutfail*|*sweep*|*fwdfail*|*conflict409*|*pending*)
      printf '{"id": "1000%s", "primaryEmail": "%s", "suspended": false}\n' "$(printf '%s' "$addr" | cksum | cut -d' ' -f1)" "$addr" ;;
    *) grep -F "\"primaryEmail\": \"$addr\"" "$GAM_MOCK_FIXTURES/print_users.json" || {
         { was_created "$addr" || group_member "$addr"; } || does_not_exist "User" "$3"
         printf '{"id": "1000%s", "primaryEmail": "%s", "suspended": false}\n' "$(printf '%s' "$addr" | cksum | cut -d' ' -f1)" "$addr"
       } ;;
  esac
  exit 0
fi
# `gam print group-members group <addr> formatjson` -> GAM's CSV, a `group` and a `JSON` column per member
# (doPrintGroupMembers): only that shape, the one the app sends. The address matches a group's whole address,
# case-insensitively, as the Directory API's groupKey does: a substring once answered for `xsales@`. Anything
# else, a user's address included, isn't a group: code that has to tell a person from a group (calendar sharing
# fans a group grant out to its members) would otherwise pass here and break live. GAM's groupNotFound ->
# entityUnknownWarning, ENTITY_DOES_NOT_EXIST_RC (wording approximate).
if [ "${1:-}" = "print" ] && [ "${2:-}" = "group-members" ]; then
  [ $# -ge 3 ] || not_modelled "print group-members without a group"
  [ "$3" = "group" ] || invalid_arg "$3"
  [ $# -ge 4 ] || missing_arg "GroupItem"
  [ -n "$4" ] || empty_arg "GroupItem"
  [ $# -ge 5 ] || not_modelled "print group-members without formatjson"
  [ "$5" = "formatjson" ] || invalid_arg "$5"
  [ $# -eq 5 ] || invalid_arg "$6"
  addr=$(lower "$4")
  is_group "$addr" || does_not_exist "Group" "$4"
  echo "group,JSON"
  case "$addr" in
    allhands@example.com) rows_with group_members_allhands.json "$addr" ;;   # 12 members: past the bulk threshold
    empty-group@example.com) ;;                                               # a real, but memberless, group
    *) rows_with group_members.json "$addr" ;;
  esac
  exit 0
fi

# `gam user <email> show vacation` (no formatjson) -> parseable text, like real GAM. With GAM_MOCK_STATE
# and a stored setting for the user, the stored one in GAM's _showVacation shape (a date, else
# Started/NotSpecified while it is on); otherwise canned.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && [ "${4:-}" = "vacation" ]; then
  [ $# -eq 4 ] || invalid_arg "$5"
  need_user "$2" Show
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
  case "$2" in   # each fixture user's own setting
    alice@example.com) on=True; subject="Out of office"; message="I am away until next week." ;;
    carol@example.com) on=False; subject="Conference week"; message="Carol is at a conference." ;;
    *) on=False; subject=None; message=None ;;
  esac
  printf 'User: %s, Vacation:\n  Enabled: %s\n  Contacts Only: False\n  Domain Only: False\n' "$2" "$on"
  printf '  Subject: %s\n  Message:\n    %s\n' "$subject" "$message"
  exit 0
fi

# `gam report users ...` -> usage CSV on stdout. GAM's progress goes to stderr (doReport; show_gettings is on
# by default): "Getting Reports for all users", then the page count, written after a CR and ended with CR LF.
# A report Google refuses ($GAM_MOCK_STATE/noreports): accessErrorExit's line after the progress, and
# INVALID_DOMAIN_RC. Wording and codes as SwiftGamGui's mock read them from GAM's source; the "Access
# Forbidden" message and the exit code are checked against the 7.49.02 build (glmsgs, the *_RC table).
if [ "${1:-}" = "report" ] && [ "${2:-}" = "users" ]; then
  printf 'Getting Reports for all users\n' 1>&2
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/noreports" ]; then
    printf '\nERROR: Customer ID: my_customer, Domain: example.com, User: admin@example.com, Access Forbidden\n' 1>&2
    exit 61
  fi
  printf '\rGot 3 Reports for all users on 2026-06-16...\r\n' 1>&2
  cat <<'EOF'
email,date,accounts:used_quota_in_mb,drive:num_items_created,gmail:num_emails_received,gmail:num_emails_sent
alice@example.com,2026-06-16,608873,1,125,10
bob@example.com,2026-06-16,1048576,0,5,0
carol@example.com,2026-06-16,2048,2,40,3
EOF
  exit 0
fi

# `gam user <email> show signature` (no formatjson) -> THAT user's signature, as text.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "show" ] && [ "${4:-}" = "signature" ]; then
  [ $# -eq 4 ] || invalid_arg "$5"
  in_directory "$2" || not_a_user "$2" Show
  case "$2" in
    alice@example.com) sig='Best,<br>Alice' ;;
    carol@example.com) sig='Carol Clark<br>Operations' ;;
    *) sig='None' ;;
  esac
  printf 'SendAs Address: <%s>\n  IsPrimary: True\n  Default: True\n  Signature:\n    %s\n' "$2" "$sig"
  exit 0
fi

# `gam print groups member <email>` -> CSV of THAT user's group emails. For an address that isn't a user
# the Directory API says "Invalid Input: memberKey" and GAM stops (invalidMember -> entityActionFailedExit,
# ACTION_FAILED_RC; read from the vendored build — wording approximate).
if [ "${1:-}" = "print" ] && [ "${2:-}" = "groups" ] && [ "${3:-}" = "member" ]; then
  [ -n "${4:-}" ] || missing_arg "EmailItem"
  [ $# -eq 4 ] || invalid_arg "$5"
  case "$4" in
    alice@example.com) printf 'email\nsales@example.com\nstaff@example.com\n' ;;
    bob@example.com)   printf 'email\nstaff@example.com\n' ;;
    carol@example.com) printf 'email\nit@example.com\n' ;;
    *) echo "ERROR: Group:, Print Failed: Invalid Input: memberKey" 1>&2; exit 50 ;;
  esac
  exit 0
fi

# `gam print domains formatjson` -> CSV, a `domainName` and a `JSON` column per domain: the Directory API's
# Domains resource, its domain aliases nested in it. The tenant: example.com (primary, aliased by
# alias.example.net) and a secondary example.net. GAM also takes todrive and showitemcountonly, and without
# formatjson flattens the record into columns this mock doesn't model; the app sends neither, so each is
# refused rather than answered in a guessed shape.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "domains" ]; then
  shift 2; fj=""
  while [ $# -gt 0 ]; do
    case "$1" in
      formatjson) fj=1; shift ;;
      quotechar) [ -n "$fj" ] || invalid_arg "$1"; [ -n "${2:-}" ] || missing_arg "Character"; shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  [ -n "$fj" ] || { echo "ERROR: mock: print domains without formatjson isn't modelled" 1>&2; exit 2; }
  cat <<'EOF'
domainName,JSON
example.com,"{""creationTime"": ""2020-01-06T17:00:00.000Z"", ""domainAliases"": [{""creationTime"": ""2021-03-01T09:00:00.000Z"", ""domainAliasName"": ""alias.example.net"", ""parentDomainName"": ""example.com"", ""verified"": true}], ""domainName"": ""example.com"", ""isPrimary"": true, ""verified"": true}"
example.net,"{""creationTime"": ""2022-05-10T12:00:00.000Z"", ""domainName"": ""example.net"", ""isPrimary"": false, ""verified"": true}"
EOF
  exit 0
fi

# `gam print groups fields <GroupFieldNameList> formatjson` -> GAM's CSV, an `email` and a `JSON` column per group
# (doPrintGroups). Each field must be one the app asks for.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "groups" ]; then
  shift 2; fj=""; had_fields=""
  while [ $# -gt 0 ]; do
    case "$1" in
      fields)
        had_fields=1
        need_value $# "GroupFieldNameList"
        [ -n "$2" ] || empty_arg "GroupFieldNameList"
        set -f
        for field in $(printf '%s' "$2" | tr ',' ' '); do
          case "$(lower "$field" | tr -d '_')" in
            email|name|description|directmemberscount) ;;
            *) not_modelled "print groups field $field" ;;
          esac
        done
        set +f
        shift 2 ;;
      formatjson) fj=1; shift ;;
      *) invalid_arg "$1" ;;
    esac
  done
  if [ -z "$fj" ]; then   # bare, GAM's default column is the address; asked-for fields it flattens, which isn't modelled
    [ "$had_fields" = "" ] || not_modelled "print groups fields without formatjson"
    echo "email"
    while IFS= read -r record || [ -n "$record" ]; do
      [ -n "$record" ] || continue
      json_text email "$record"; echo
    done < "$GAM_MOCK_FIXTURES/groups.json"
    exit 0
  fi
  print_formatjson email groups.json
  exit 0
fi

# `<user> print messages query rfc822msgid:<id> includespamtrash max_to_print <n> headers all showlabels showdate`: the
# Phishing screen's recipients read (GamCommands.searchMessages, detail .headers), strict, as GAM 7.48.30's
# printShowMessagesThreads runs it, from the mailboxes in mock_gam/messages.csv and each message's headers in
# mock_gam/message_headers/<id> (its `Gmail-` lines are Gmail's own fields; the rest its headers, in order; made up).
# With GAM_MOCK_STATE, $GAM_MOCK_STATE/trashed/<id> is in Trash (still found: includespamtrash), deleted/<id> purged
# (gone), and nogmail/<user> Gmail off. As GAM does:
#   - a token for the user (token_refused, "User Print Failed"); with showlabels the labels are read first, so Gmail off is
#     userGmailServiceNotEnabledWarning (73) before any list;
#   - the list ("Getting all Messages…" on stderr), at most max_to_print (0 is no limit); none sets NO_ENTITIES_FOUND_RC (60);
#   - the CSV (_printMessage, SortTitles): User,threadId,id, GAM's default headers, LabelsCount,Labels, then every other
#     header name sorted; each header under its own name, a repeat as "<name> 1"; Date is the message's own time
#     (showdate overwrites the header); the header line is printed even with no row, after any failure.
# The same read answers Users' "Search a mailbox" (MailboxSearch, docs/domains/compromised-account.md slice 3): a query of
# space-separated Gmail terms, each modelled from the fixtures: `rfc822msgid:<id>`; `in:<label>` (the message's labels,
# case aside: the compromised-account check's `in:scheduled` is the SCHEDULED label, carol@'s one scheduled phish, a name
# for the live check to confirm); `from:<text>` (its From header) and `to:<text>` (To, Cc or Bcc), case aside, as part of the header;
# `newer_than:<N>d|h`, a known lie: every fixture message is newer (their dates are fixed). Any other term, or a message
# whose headers the fixtures don't hold, is refused as not modelled, never answered more kindly than Gmail.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "messages" ] && [ "${5:-}" = "query" ] \
    && case "${6:-}" in rfc822msgid:*) true ;; *) false ;; esac; then
  user=$2; query=$6
  shift 6
  spamtrash=""; all=""; labels=""; dates=""; most=0
  while [ $# -gt 0 ]; do
    word=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '_')
    case "$word" in
      includespamtrash) spamtrash=1; shift ;;
      maxtoprint)
        [ $# -ge 2 ] || missing_arg "integer x>=0"
        case "$2" in ''|*[!0-9]*) usage_error "Invalid argument: Expected <integer x>=0>" ;; esac
        most=$2; shift 2 ;;
      headers)
        [ $# -ge 2 ] || missing_arg "String"
        [ "$2" = all ] || not_modelled "print messages with headers other than all"
        all=1; shift 2 ;;
      showlabels) labels=1; shift ;;
      showdate) dates=1; shift ;;
      *) not_modelled "an rfc822msgid: search with $1" ;;
    esac
  done
  { [ -n "$spamtrash" ] && [ -n "$all" ] && [ -n "$labels" ] && [ -n "$dates" ]; } \
    || not_modelled "an rfc822msgid: search other than the recipients read"
  [ -n "$query" ] || empty_arg "Query"
  set -f
  for term in $query; do
    case "$term" in
      *[*?[]*) not_modelled "a Gmail term like $term" ;;
      rfc822msgid:?*|in:?*|from:?*|to:?*) ;;
      newer_than:[0-9]*[dh]) case "${term#newer_than:}" in *[!0-9dh]*|*[dh]?*) not_modelled "a Gmail term like $term" ;; esac ;;
      *) not_modelled "a Gmail term like $term" ;;
    esac
  done
  set +f
  # Each term of the query, against one message: its Message-ID, its labels, and its header file (`Gmail-` lines aside).
  matches() {  # <message-id> <labels> <id>; the terms hold no glob character (refused above)
    for term in $query; do
      case "$term" in
        rfc822msgid:*) [ "$1" = "${term#rfc822msgid:}" ] || return 1 ;;
        in:*) want=$(printf '%s' "${term#in:}" | tr '[:lower:]' '[:upper:]')
              case " $2 " in *" $want "*) ;; *) return 1 ;; esac ;;
        newer_than:*) ;;
        from:*|to:*)
          [ -f "$GAM_MOCK_FIXTURES/message_headers/$3" ] || not_modelled "message $3's headers"
          case "$term" in from:*) names='^From:' ;; *) names='^(To|Cc|CC|Bcc|BCC):' ;; esac
          grep -E "$names" "$GAM_MOCK_FIXTURES/message_headers/$3" | grep -qiF -- "${term#*:}" || return 1 ;;
      esac
    done
    return 0
  }
  base="User
threadId
id
Date
Subject
From
Reply-To
To
Delivered-To
Content-Type
Message-ID
LabelsCount
Labels"
  header_only() { gam_row $(printf '%s' "$base" | tr '\n' ' '); }
  if ! known_user "$user"; then header_only; token_refused "$user" Print; fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
    printf 'User: %s, Gmail Service/App not enabled\n' "$user" 1>&2
    header_only; exit 73
  fi
  printf 'Getting all Messages that match query ((%s)) for %s\n' "$query" "$user" 1>&2
  ids=""; labels_of=""
  while IFS=, read -r owner id message tags; do
    [ "$owner" = "$user" ] || continue
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/trashed/$id" ]; then now="$tags TRASH"; else now=$tags; fi
    matches "$message" "$now" "$id" || continue
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/deleted/$id" ]; then continue; fi
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/trashed/$id" ]; then tags="$tags TRASH"; fi
    ids="$ids $id"
    labels_of="$labels_of
$id $tags"
    # At most max_to_print (GAM's maxToProcess; 0 is no limit).
    set -- $ids
    if [ "$most" -gt 0 ] && [ "$#" -ge "$most" ]; then break; fi
  done < "$GAM_MOCK_FIXTURES/messages.csv"
  if [ -z "$ids" ]; then
    printf '\rGot 0 Messages that matched query ((%s)) for %s...\r\n' "$query" "$user" 1>&2
    header_only; exit 60
  fi
  rows=$(mktemp -d)
  trap 'rm -rf "$rows"' EXIT
  n=0
  for id in $ids; do
    n=$((n + 1)); data="$GAM_MOCK_FIXTURES/message_headers/$id"
    [ -f "$data" ] || not_modelled "message $id's headers"
    tags=$(printf '%s\n' "$labels_of" | sed -n "s/^$id //p")
    {
      printf 'User\t%s\nthreadId\t%s\nid\t%s\n' "$user" "$(sed -n 's/^Gmail-ThreadId: //p' "$data")" "$id"
      # Each header under its own name, a repeat numbered from 1 (headerCounts); Date is replaced below.
      awk '/^Gmail-/ { next } { name = substr($0, 1, index($0, ": ") - 1); value = substr($0, index($0, ": ") + 2)
             c = seen[name]++; if (c > 0) name = name " " c; if (name != "Date") print name "\t" value }' "$data"
      printf 'Date\t%s\n' "$(sed -n 's/^Gmail-InternalDate: //p' "$data")"
      set -- $tags
      printf 'LabelsCount\t%s\nLabels\t%s\n' "$#" "$tags"
    } > "$rows/$n"
  done
  extras=$(cut -f1 "$rows"/* | LC_ALL=C sort -u | while IFS= read -r name; do
    printf '%s\n' "$base" | grep -qxF -- "$name" || printf '%s\n' "$name"; done)
  columns="$base
$extras"
  printf '\rGot %s Messages that matched query ((%s)) for %s...\r\n' "$n" "$query" "$user" 1>&2
  # The header, then each message's cells in the same order.
  old_ifs=$IFS; IFS='
'
  set --
  for column in $columns; do [ -n "$column" ] && set -- "$@" "$column"; done
  IFS=$old_ifs
  gam_row "$@"
  i=0
  while [ "$i" -lt "$n" ]; do
    i=$((i + 1))
    old_ifs=$IFS; IFS='
'
    set --
    for column in $columns; do
      [ -n "$column" ] || continue
      set -- "$@" "$(awk -F '\t' -v k="$column" '$1 == k { print substr($0, length($1) + 2); exit }' "$rows/$i")"
    done
    IFS=$old_ifs
    gam_row "$@"
  done
  exit 0
fi

if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "messages" ]; then
  case "$*" in
    *FAILME*) echo "ERROR: 400: Bad Request - precondptionFailed: a representative GAM failure detail" 1>&2; exit 1 ;;
  esac
  # `print messages` has no formatjson mode — real GAM returns CSV. One row carries a bulk-mailer
  # style Return-Path (a bounce address unlike the From) so the mailbox-search flow has an
  # envelope-sender to surface. Made up: never paste a real message's headers in here.
  cat <<'EOF'
User,id,Subject,From,Date,Return-Path
someone@example.com,msg_1001,Your receipt,billing@vendor.example,"Mon, 23 Jun 2026 14:29:10 +0000",<fixture-bounce-0001@bounces.vendor.example>
someone@example.com,msg_1002,Weekly digest,news@vendor.example,"Tue, 24 Jun 2026 09:00:00 +0000",<bounce@vendor.example>
EOF
  exit 0
fi

# `gam print cros [query <QueryCrOS>] [fields <CrOSFieldNameList>] formatjson` -> GAM's CSV, a `deviceId` and a `JSON`
# column per device (doPrintCrOSDevices; Builder: Find Chromebooks). Every device, whatever the query or fields.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "cros" ]; then
  shift 2; fj=""
  while [ $# -gt 0 ]; do
    case "$1" in
      query) need_value $# "QueryCrOS"; shift 2 ;;
      fields) need_value $# "CrOSFieldNameList"; [ -n "$2" ] || empty_arg "CrOSFieldNameList"; shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print cros with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print cros without formatjson"
  print_formatjson deviceId cros.json
  exit 0
fi

# `gam user <email> print filelist [query <String>] fields <DriveFieldNameList> formatjson` -> GAM's CSV, `Owner`, `id`,
# `name`, the flattened owners, and a `JSON` column per file (printFileList; Builder: Find Drive files). `owners` in the
# JSON is Drive's list of objects. printFileList exits 60 when it printed no row; a refused token is its warning first.
# Every file for any user, whatever the query (the owners columns' exact set is approximate).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "filelist" ]; then
  user=$2; shift 4; fj=""
  while [ $# -gt 0 ]; do
    case "$1" in
      query) need_value $# "QueryDriveFile"; shift 2 ;;
      fields) need_value $# "DriveFieldNameList"; [ -n "$2" ] || empty_arg "DriveFieldNameList"; shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print filelist with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print filelist without formatjson"
  if ! known_user "$(lower "$user")"; then
    printf 'User: %s, User Print Failed: invalid_grant: Invalid email or User ID\n' "$user" 1>&2
    echo "Owner,id,name,owners.emailAddress,JSON"
    exit 60
  fi
  echo "Owner,id,name,owners,owners.0.displayName,owners.0.emailAddress,JSON"
  while IFS= read -r record || [ -n "$record" ]; do
    [ -n "$record" ] || continue
    gam_row "$user" "$(json_text id "$record")" "$(json_text name "$record")" 1 \
      "$(json_text displayName "$record")" "$(json_text emailAddress "$record")" "$record"
  done < "$GAM_MOCK_FIXTURES/drive_files.json"
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

# `gam user <u> print forwardingaddresses` (printShowForwardingAddresses): GAM acts as the person, so an address that
# isn't a user fails at the token, and Gmail off ($GAM_MOCK_STATE/nogmail/<u>) is exit 73 after its "Getting" line, with
# the titles alone. One row per address Gmail has, none for none: only alice@ has one.
gmail_off() { [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$1" ]; }
gmail_read() {  # <user> <items> <titles>: the token, the "Getting" line, and Gmail off
  need_user "$1" Print
  printf 'Getting %s for %s\n' "$2" "$1" 1>&2
  if gmail_off "$1"; then printf 'User: %s, Gmail Service/App not enabled\n' "$1" 1>&2; printf '%s\n' "$3"; exit 73; fi
}
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "forwardingaddresses" ]; then
  user=$(lower "$2")
  [ $# -eq 4 ] || not_modelled "print forwardingaddresses with $5"
  titles='User,forwardingEmail,verificationStatus'
  gmail_read "$user" "Forwarding Addresses" "$titles"
  printf '%s\n' "$titles"
  case "$user" in
    alice@example.com) printf '%s,fwd@example.com,accepted\n' "$user" ;;
  esac
  exit 0
fi

# `gam user <email> print delegates` (no formatjson) -> THAT user's delegates as plain CSV, like real GAM.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "delegates" ]; then
  [ $# -eq 4 ] || invalid_arg "$5"
  need_user "$2" Print
  printf 'User,delegateAddress,delegationStatus\n'
  delegates_of "$2" | while IFS= read -r d; do
    if [ -n "$d" ]; then printf '%s,%s,accepted\n' "$2" "$d"; fi
  done
  exit 0
fi

# `gam user <u> print calendaracls primary formatjson` (printShowCalendarACLs) -> primaryEmail,calendarId,JSON: THAT
# user's rules, each rule's id, role and scope only (the JSON holds neither key column). Without formatjson GAM
# flattens each rule into columns this mock doesn't model.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendaracls" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  [ $# -ge 6 ] || not_modelled "print calendaracls without formatjson"
  [ "$6" = "formatjson" ] || invalid_arg "$6"
  [ $# -eq 6 ] || invalid_arg "$7"
  in_directory "$2" || not_a_user "$2" Print
  [ "$5" = "primary" ] || [ "$5" = "$2" ] || does_not_exist "Calendar" "$5"
  user="$2"
  acl() {  # <scope value> <role>: one user-scoped rule on this user's primary calendar
    gam_row "$user" "$user" "{\"id\": \"user:$1\", \"role\": \"$2\", \"scope\": {\"type\": \"user\", \"value\": \"$1\"}}"
  }
  echo "primaryEmail,calendarId,JSON"
  case "$user" in
    alice@example.com) rows_with calendar_acls.json "$user" "$user" ;;
    carol@example.com) acl "$user" owner; acl helpdesk@example.com reader ;;
    *) acl "$user" owner ;;
  esac
  exit 0
fi

# `gam print resources fields id,name,email,resourcetype,buildingid formatjson` (doPrintShowResourceCalendars) ->
# resourceId,resourceName,JSON; GAM writes buildingId as `id:<id>` beside a looked-up buildingName.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "resources" ]; then
  shift 2; fj=""
  while [ $# -gt 0 ]; do
    case "$1" in
      fields) need_value $# "ResourceFieldNameList"; [ -n "$2" ] || empty_arg "ResourceFieldNameList"; shift 2 ;;
      query) need_value $# "String"; shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print resources with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print resources without formatjson"
  print_formatjson "resourceId resourceName" resources.json
  exit 0
fi

# `gam all users print calendars ...` -> real GAM shape: CSV with a `primaryEmail` sibling column
# next to the per-row `JSON` blob (the owning user is NOT inside the JSON). Used for name search.
if [ "${1:-}" = "all" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendars" ]; then
  cat "$GAM_MOCK_FIXTURES/all_calendars.csv"
  exit 0
fi

# `gam user <u> print calendars fields <CalendarListFieldList> formatjson` (printShowCalendars) ->
# primaryEmail,calendarId,JSON. An address that isn't a user fails at the token. alice@ has her two; anyone else their
# primary calendar alone.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendars" ]; then
  user=$2; shift 4; fj=""; owned=""; hidden=""
  while [ $# -gt 0 ]; do
    case "$1" in
      fields) need_value $# "CalendarListFieldList"; [ -n "$2" ] || empty_arg "CalendarListFieldList"; shift 2 ;;
      formatjson) fj=1; shift ;;
      ownedsecondary) owned=1; shift ;;
      showhidden) hidden=1; shift ;;
      *) not_modelled "print calendars with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print calendars without formatjson"
  known_user "$(lower "$user")" || not_a_user "$user" Print
  # `ownedsecondary`: the calendars on that list the person is dataOwner of. calendarList.list leaves hidden entries out
  # unless showHidden: `user_owned_calendars/<u>.json` are the visible ones, `<u>.hidden.json` the hidden ones. Calendar
  # off (GAM_MOCK_STATE/nocalendar/<u>) fails after the "Getting" line, exit 73.
  if [ -n "$owned" ]; then
    printf 'Getting Calendars for %s\n' "$user" 1>&2
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nocalendar/$(lower "$user")" ]; then
      printf 'User: %s, Calendar Service/App not enabled\n' "$user" 1>&2; exit 73
    fi
    echo "primaryEmail,calendarId,JSON"
    for file in "$GAM_MOCK_FIXTURES/user_owned_calendars/$(lower "$user").json" ${hidden:+"$GAM_MOCK_FIXTURES/user_owned_calendars/$(lower "$user").hidden.json"}; do
      [ -f "$file" ] || continue
      while IFS= read -r record; do
        [ -n "$record" ] || continue
        # GAM keeps those whose dataOwner is the person: one handed over has left this read.
        [ "$(calendar_owner "$(json_text id "$record")")" = "$(lower "$user")" ] || continue
        gam_row "$user" "$(json_text id "$record")" "$record"
      done < "$file"
    done
    exit 0
  fi
  echo "primaryEmail,calendarId,JSON"
  if [ "$user" = "alice@example.com" ]; then
    while IFS= read -r record || [ -n "$record" ]; do
      [ -n "$record" ] || continue
      gam_row "$user" "$(json_text id "$record")" "$record"
    done < "$GAM_MOCK_FIXTURES/user_calendars.json"
  else
    gam_row "$user" "$user" "{\"accessRole\": \"owner\", \"id\": \"$user\", \"primary\": true, \"summary\": \"$user\"}"
  fi
  exit 0
fi

# `gam calendars <id> print calendaracls formatjson` (doCalendarsPrintShowACLs) -> calendarId,JSON.
if [ "${1:-}" = "calendars" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "calendaracls" ]; then
  [ $# -ge 5 ] || not_modelled "print calendaracls without formatjson"
  [ "$5" = "formatjson" ] || invalid_arg "$5"
  [ $# -eq 5 ] || invalid_arg "$6"
  echo "calendarId,JSON"
  case "$2" in
    *orphan*) rows_with calendar_acls_orphan.json "$2" ;;  # sole owner is a suspended user
    *)        rows_with calendar_acls.json "$2" ;;
  esac
  exit 0
fi
# `gam calendars <id> print events [query <S>] [after <T>] [before <T>] [eventid <id>] fields <…> formatjson`
# (_validateCalendarGetEvents, _printCalendarEvent) -> calendarId,id,JSON. `eventid` answers that one event, not every
# event; one that doesn't exist is entityActionFailedWarning, ACTION_FAILED_RC, with the titles alone (Google's words
# approximate). A list that finds nothing is NO_ENTITIES_FOUND_RC (a NOMATCH query here). The query and dates otherwise
# select nothing.
if [ "${1:-}" = "calendars" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "events" ]; then
  cal=$2; shift 4; fj=""; only=""
  while [ $# -gt 0 ]; do
    case "$1" in
      query|after|before) need_value $# "String"; shift 2 ;;
      eventid) need_value $# "EventId"; only=$2; shift 2 ;;
      fields) need_value $# "EventFieldNameList"; [ -n "$2" ] || empty_arg "EventFieldNameList"; shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print events with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print events without formatjson"
  echo "calendarId,id,JSON"
  if [ -n "$only" ] && ! grep -qF "\"id\": \"$only\"" "$GAM_MOCK_FIXTURES/events.json"; then
    printf 'Calendar: %s, Event: %s, Print Failed: Not Found\n' "$cal" "$only" 1>&2
    exit 50
  fi
  case "$ARGS" in *NOMATCH*) exit 60 ;; esac
  while IFS= read -r record || [ -n "$record" ]; do
    [ -n "$record" ] || continue
    id=$(json_text id "$record")
    if [ -z "$only" ] || [ "$only" = "$id" ]; then gam_row "$cal" "$id" "$record"; fi
  done < "$GAM_MOCK_FIXTURES/events.json"
  exit 0
fi

# `gam user <admin> check serviceaccount (scope|scopes <APIScopeURLList>)*`, as the vendored 7.48.11
# build's checkServiceAccount prints it: `{label:73} PASS`, then each scope checked, sorted, as
# `  {scope:73} PASS (j/n)`. A scope asked for must be a SERVICE-ACCOUNT scope (lowercased, commas or
# spaces between) or GAM exits with an invalid choice — admin.directory.* are client-access scopes (the
# admin token, `gam oauth create`) and are refused here as GAM refuses them. The simulated tenant
# authorized exactly the scopes GamGUI pre-fills (core/setup.py DWD_SCOPES). A bare check asks about
# GAM's own larger default set, so its extra scopes FAIL and GAM exits SCOPES_NOT_AUTHORIZED_RC (10).
# A `*partialdwd*` admin's tenant authorized all of those but gmail.settings.sharing and tasks. On a FAIL,
# checkServiceAccount appends userinfo.email to the scopes checked and prints SCOPE_AUTHORIZATION_FAILED
# with the short link and the admin.google.com one (clientScopeToAdd = those scopes, sorted; authuser =
# the admin) — all on STDOUT (printLine), nothing on stderr — then exits SCOPES_NOT_AUTHORIZED_RC (10).
# The exit codes are the build's *_RC values; tests/test_gam_exit_codes.py holds them to it.
SA_AUTHORIZED="https://mail.google.com/ https://www.googleapis.com/auth/calendar https://www.googleapis.com/auth/drive https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.settings.basic https://www.googleapis.com/auth/gmail.settings.sharing https://www.googleapis.com/auth/tasks"
# A sample of GAM's other default-on service-account scopes (gamlib glapi _SVCACCT_SCOPES).
SA_OTHER="https://www.googleapis.com/auth/documents https://www.googleapis.com/auth/drive.activity https://www.googleapis.com/auth/forms.body https://www.googleapis.com/auth/keep https://www.googleapis.com/auth/spreadsheets"
if [ "${1:-}" = "user" ] && [ "${3:-}" = "check" ] && [ "${4:-}" = "serviceaccount" ]; then
  sa_user="$2"; shift 4
  asked=""
  while [ $# -gt 0 ]; do
    case "$1" in
      scope|scopes)
        need_value $# "APIScopeURLList"
        [ -n "$2" ] || empty_arg "APIScopeURLList"
        for s in $(printf '%s' "$2" | tr 'A-Z,' 'a-z '); do
          case " $SA_AUTHORIZED $SA_OTHER " in
            *" $s "*) asked="$asked $s" ;;
            *) invalid_choice "$s" "$(printf '%s' "$SA_AUTHORIZED $SA_OTHER" | tr ' ' '|')" ;;
          esac
        done
        shift 2 ;;
      *) invalid_arg "$1" ;;
    esac
  done
  [ -n "$asked" ] || asked="$SA_AUTHORIZED $SA_OTHER"
  checked=$(printf '%s\n' $asked | sort -u)
  n=$(printf '%s\n' "$checked" | wc -l | tr -d ' ')
  # A rejected service-account key (deleted or rotated in the Cloud console): the key row FAILs and GAM
  # stops before any scope is checked — no delegation table, no link. invalidOauth2serviceJsonExit then
  # writes its error and instructions to stderr and exits OAUTH2SERVICE_JSON_REQUIRED_RC (16).
  case "$sa_user" in
    *badkey*)
      printf '%-73s %s\n' "System time status" "PASS" \
        "Service Account Private Key Authentication" "FAIL"
      printf '\nERROR: Service Account OAuth2 File: %s, Does not exist or has invalid format, Authentication\n' \
        "$GAMCFGDIR/oauth2service.json" 1>&2
      printf 'Please run\n\ngam create|use project\ngam user <user> update serviceaccount\n\nto create and authorize a Service account.\n' 1>&2
      exit 16 ;;
  esac
  printf '%-73s %s\n' "System time status" "PASS" \
    "Service Account Private Key Authentication" "PASS" \
    "Service Account Private Key age; Google recommends rotating keys on a routine basis" "PASS"
  printf 'Domain-wide Delegation authentication:, User: %s, Scopes: %s\n' "$sa_user" "$n"
  granted="$SA_AUTHORIZED"
  case "$sa_user" in
    *partialdwd*) granted=$(printf '%s\n' $SA_AUTHORIZED | grep -v -e '/gmail\.settings\.sharing$' -e '/tasks$' | tr '\n' ' ') ;;
  esac
  j=0; failed=0
  for s in $checked; do
    j=$((j + 1))
    case " $granted " in *" $s "*) st=PASS ;; *) st=FAIL; failed=1 ;; esac
    printf '  %-73s %s (%d/%d)\n' "$s" "$st" "$j" "$n"
  done
  if [ "$failed" = 1 ]; then
    to_add=$(printf '%s\n' $checked https://www.googleapis.com/auth/userinfo.email | sort -u | paste -sd, -)
    printf '\nSome scopes FAILED or should be DISABLED!\nTo update authorization, please go to the following link in your browser:\n%s\n    %s\n\n' \
      "https://gam-shortn.appspot.com/mockdwd" \
      "https://admin.google.com/ac/owl/domainwidedelegation?clientScopeToAdd=$to_add&clientIdToAdd=1234567890&overwriteClientId=true&authuser=$sa_user"
    printf '%s\n' "You will be directed to the Google Workspace admin console Security > API Controls > Domain-wide Delegation page" \
      'The "Add a new Client ID" box will open' 'Make sure that "Overwrite existing client ID" is checked' 'Click AUTHORIZE' \
      "When the box closes you're done" \
      'After authorizing it may take some time for this test to pass so wait a few moments and then try this command again.' '' ''
    exit 10
  fi
  printf '\nAll scopes PASSED!\n\nService Account Client name: 1234567890 is fully authorized.\n\n'
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
  phone_primary="" phones_sent="" phones_json=""
  while [ $# -gt 0 ]; do
    case "$1" in
      phone)
        # Grammar `phone clear`, or `phone [type <type>|custom <String>] [value <String>] notprimary|primary`, as
        # getUserAttributes reads it: a type GAM doesn't know is kept as a custom type, so any word passes; `custom` needs a
        # name that isn't blank; a second primary is a usage error; an empty value is dropped, none at all stops GAM.
        shift
        phones_sent=1
        if [ "${1:-}" = "clear" ]; then shift; phones_json=""; continue; fi
        phone_value="" phone_kept="" phone_type="" phone_custom="" phone_text=""
        while :; do
          case "${1:-}" in
            type)
              need_value $# "phone type"
              if [ "$(lower "$2" | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')" = custom ]; then
                case "${3:-}" in *[![:space:]]*) phone_type=custom; phone_custom=$3; shift 3 ;; *) missing_arg "custom attribute type" ;; esac
              else
                phone_type=$2; shift 2
              fi ;;
            value) need_value $# "String"; phone_value=1; phone_text=$2; if [ -n "$2" ]; then phone_kept=1; fi; shift 2 ;;
            primary|notprimary)
              if [ -z "$phone_value" ]; then
                printf 'Traceback (most recent call last):\n  File "gam/__init__.py", in appendItemToBodyList\nKeyError: %s\n' "'value'" 1>&2
                exit 1
              fi
              if [ "$1" = primary ] && [ -n "$phone_kept" ]; then
                [ -z "$phone_primary" ] || usage_error "Multiple phones are marked primary, only one can be primary"
                phone_primary=1
              fi
              if [ -n "$phone_kept" ]; then
                phones_json="${phones_json:+$phones_json, }{\"type\": \"$(json_quote "$phone_type")\", \"customType\": \"$(json_quote "$phone_custom")\", \"value\": \"$(json_quote "$phone_text")\", \"primary\": $([ "$1" = primary ] && echo true || echo false)}"
              fi
              shift; break ;;
            "") missing_arg "notprimary|primary" ;;
            *) invalid_arg "$1" ;;
          esac
        done ;;
      suspended|suspend|changepassword|changepasswordatnextlogin|archived|archive)
        need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      password)
        need_value $# "Password"
        case "$2" in
          random|uniquerandom) shift 2; case "${1:-}" in [0-9]*) shift ;; esac ;;
          *) shift 2 ;;
        esac ;;
      firstname|givenname|lastname|familyname)
        # getUserAttributes reads a name with getString(maxLen=60): longer is a usage error, which echoes the
        # whole command line, password and all.
        need_value $# "String"
        [ "$(printf '%s' "$2" | LC_ALL=C tr -d '\200-\277' | wc -c)" -le 60 ] || invalid_arg "Expected <string length 0<=x<=60 for String>"
        shift 2 ;;
      gal|includeinglobaladdresslist)
        need_value $# "Boolean"; need_bool "$2"; shift 2 ;;
      displayname|recoveryemail|recoveryphone|ou|org|orgunitpath)
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

# `gam create user <email> <UserAttribute>* [notify ...]` -> new account. An address already taken fails as
# doCreateUser's `except GAPI.duplicate` does: duplicateAliasGroupUserWarning names what holds it (a user, a user's
# alias, a group), ENTITY_DUPLICATE_RC. Taken: a directory user, one of their aliases, a group, an account made earlier
# in this state, or an *exists* address. An org unit that doesn't exist fails the command (entityActionFailedExit,
# ACTION_FAILED_RC). Read from GAM's source, not captured from a tenant.
duplicate_of() {  # <address, lowercased>: what GAM names as holding it, or nothing
  if in_directory "$1" || was_created "$1" || case "$1" in *exists*) true ;; *) false ;; esac; then
    printf 'User: %s' "$1"; return
  fi
  while IFS= read -r record || [ -n "$record" ]; do
    case "$(printf '%s' "$record" | sed -n 's/.*"aliases": \[\([^]]*\)\].*/\1/p')" in
      *"\"$1\""*) printf 'User Alias: %s, User: %s' "$1" "$(json_text primaryEmail "$record")"; return ;;
    esac
  done < "$GAM_MOCK_FIXTURES/print_users.json"
  if is_group "$1"; then printf 'Group: %s' "$1"; fi
}
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "EmailAddress"
  user="$3"; shift 3
  check_user_attrs "$@"
  org=""
  while [ $# -gt 0 ]; do case "$1" in ou|org|orgunitpath) org="$2"; shift 2 ;; *) shift ;; esac; done
  held=$(duplicate_of "$(lower "$user")")
  if [ -n "$held" ]; then
    printf 'User: %s, Create Failed: Duplicate, %s\n' "$user" "$held" 1>&2
    exit 57
  fi
  case "$(lower "$org")" in
    *missing*|*nonexistent*) printf '\nERROR: User: %s, Create Failed: Invalid Organizational Unit\n' "$user" 1>&2; exit 50 ;;
  esac
  case "$user" in
    # Seen live 2026-09-30, when the subscription had no free license.
    *USERLIMIT*) echo "ERROR: User: $user, Create Failed: Domain user limit reached. Contact Support." 1>&2; exit 50 ;;
  esac
  if [ -n "${GAM_MOCK_STATE:-}" ]; then
    mkdir -p "$GAM_MOCK_STATE/created"
    : > "$GAM_MOCK_STATE/created/$(lower "$user")"
  fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$GAM_MOCK_STATE/new_account_lag" ]; then
    mkdir -p "$GAM_MOCK_STATE/provisioning"
    cp "$GAM_MOCK_STATE/new_account_lag" "$GAM_MOCK_STATE/provisioning/$user"
  fi
  echo "User: $user, Added"
  exit 0
fi

# `gam update user <email> <UserAttribute>*` -> organization / suspended / password random.
if [ "${1:-}" = "update" ] && [ "${2:-}" = "user" ]; then
  [ -n "${3:-}" ] || missing_arg "UserItem"
  user="$3"; shift 3
  check_user_attrs "$@"
  check_exists "User" "$user"
  if [ -n "$phones_sent" ] && [ -n "${GAM_MOCK_STATE:-}" ]; then   # the list sent is the list Google keeps
    mkdir -p "$GAM_MOCK_STATE/phones"; printf '[%s]\n' "$phones_json" > "$GAM_MOCK_STATE/phones/$(lower "$user")"
  fi
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
# The stdout wording is GAM's shape from its source conventions, not captured from a tenant. Before each
# part GAM says on stderr what it is getting (printGettingEntityItemForWhom; one user, so no counter).
# A suspended user's backup codes aren't touched (checkUserSuspended): when they have codes, "Not Deprovisioned" with
# GAM's reason (IS_SUSPENDED_NO_BACKUPCODES) and ACTION_NOT_PERFORMED_RC once the other parts have run; without codes
# nothing is said and it exits 0. Everyone has codes here, except an account in $GAM_MOCK_STATE/nobackupcodes/<user>.
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
  printf 'Getting Application Specific Password IDs for %s\n' "$user" 1>&2
  echo "User: $user, Application Specific Passwords: 0"
  printf 'Getting Backup Verification Codes for %s\n' "$user" 1>&2
  rc=0
  if grep -F "\"primaryEmail\": \"$(lower "$user")\"" "$GAM_MOCK_FIXTURES/print_users.json" | grep -qF '"suspended": true' \
     && ! { [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nobackupcodes/$(lower "$user")" ]; }; then
    printf 'User: %s, Backup Verification Codes Not Deprovisioned: User is suspended. You must unsuspend to process backupcodes\n' "$user" 1>&2
    rc=51
  else
    echo "User: $user, Backup Verification Codes, Invalidated"
  fi
  printf 'Getting Access Tokens for %s\n' "$user" 1>&2
  echo "User: $user, Access Tokens: 0"
  if [ -n "$signout" ]; then
    case "$user" in *SIGNOUTFAIL*) signout_refused "$user" ;; esac
    echo "User: $user, Signed Out"
  fi
  echo "User: $user, Deprovisioned"
  exit $rc
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
  if still_provisioning "$user"; then not_ready_token "$user" "User Set"; fi
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
  # A delegate the user doesn't have: Gmail's delete is notFound, reported against the pair (processDelegates,
  # entityActionFailedWarning, ACTION_FAILED_RC; the API's words approximate).
  if [ "$verb" = "Delete" ] && ! has_delegate "$2" "$delegate"; then
    printf 'User: %s, Delegate: %s, Delete Failed: Not Found\n' "$2" "$delegate" 1>&2; exit 50
  fi
  # A delegate the user already has: GAM's processDelegates catches Gmail's alreadyExists and reports it
  # against the pair (entityActionFailedWarning, ACTION_FAILED_RC; read from the vendored build; Google's
  # wording approximate). *exists* is the trigger; a delegate the user has (delegates_of) fails the same way.
  if [ "$verb" = "Add" ]; then
    case "$delegate" in *exists*) already=1 ;; *) already="" ;; esac
    if has_delegate "$2" "$delegate"; then already=1; fi
    if [ -n "$already" ]; then
      printf 'User: %s, Delegate: %s, Add Failed: Delegate already exists.\n' "$2" "$delegate" 1>&2; exit 50
    fi
  fi
  if [ -n "${GAM_MOCK_STATE:-}" ]; then   # keep the list the way Gmail does, for the next print delegates
    ddir="$GAM_MOCK_STATE/delegates"; mkdir -p "$ddir"
    [ -f "$ddir/$2" ] || canned_delegates "$2" > "$ddir/$2"
    if [ "$verb" = "Add" ]; then
      printf '%s\n' "$delegate" >> "$ddir/$2"
    else
      grep -vixF -- "$delegate" "$ddir/$2" > "$ddir/$2.new" || true
      mv "$ddir/$2.new" "$ddir/$2"
    fi
  fi
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
    *SWEEPCLEAN*)
      # A leaver suspended before offboarding is not among `all users`: no own calendar visited, exit 0.
      printf 'User: alice@example.com, Calendar: alice@example.com, Calendar ACL: (Scope: user:%s), Deleted\n' "$scope"
      exit 0 ;;
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
  # Seen live (2026-10-07, GAM 7.48.14): `all users` includes the leaver, who is still active after the
  # reset, so every sweep ends on their own primary calendar, which GAM refuses to change — exit 50 with
  # this line, tolerated as OWN_ACL. A clean exit 0 never happens for an active leaver.
  printf 'User: alice@example.com, Calendar: alice@example.com, Calendar ACL: (Scope: user:%s), Deleted\n' "$scope"
  printf '    Calendar: %s, Calendar ACL: (Scope: user:%s), Delete Failed: Cannot change your own access level.\n' "$scope" "$scope" 1>&2
  exit 50
fi

# `gam user <owner> remove calendars <UserCalendarEntity>` -> PERMANENTLY delete a secondary calendar.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "remove" ] && [ "${4:-}" = "calendars" ]; then
  [ -n "${5:-}" ] || missing_arg "UserCalendarEntity"
  [ $# -eq 5 ] || invalid_arg "$6"
  check_exists "Calendar" "$5"
  echo "User: $2, Calendar: $5, Removed"
  exit 0
fi

# `gam user <email> add calendars <id> <CalendarAttribute>*` (addCalendars, _processCalendarList) -> subscribe, GAM
# acting as them. One who isn't a user fails at the token. Calendar off, or an account Google is still setting up, exits
# 73 from validateCalendar, before GAM steps in a level, so unindented. Each calendar's line is one level in (two
# spaces): `  User: <u>, Calendar: <c>, Added`; a calendar GAM can't get, or one already in their list,
# `  User: <u>, Calendar: <c>, Add Failed: <why>`, exit 50 (Google's words approximate). With GAM_MOCK_STATE the
# calendars added are kept. A SUBFAIL target simulates GAM refusing to act as that recipient (partial-success path).
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
  known_user "$(lower "$user")" || not_a_user "$user" Add
  if still_provisioning "$user"; then
    printf 'User: %s, Calendar Service/App not enabled\n' "$user" 1>&2
    exit 73
  fi
  case "$cal" in
    *missing*|*nonexistent*) printf '  User: %s, Calendar: %s, Add Failed: Not Found\n' "$user" "$cal" 1>&2; exit 50 ;;
  esac
  listed=""
  if [ "$cal" = "$user" ]; then listed=1; fi
  if [ "$user" = "alice@example.com" ] && grep -qF "\"id\": \"$cal\"" "$GAM_MOCK_FIXTURES/user_calendars.json"; then listed=1; fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -f "$GAM_MOCK_STATE/subscribed/$user" ] && grep -qxF "$cal" "$GAM_MOCK_STATE/subscribed/$user"; then listed=1; fi
  if [ -n "$listed" ]; then
    printf '  User: %s, Calendar: %s, Add Failed: Duplicate\n' "$user" "$cal" 1>&2; exit 50
  fi
  if [ -n "${GAM_MOCK_STATE:-}" ]; then
    mkdir -p "$GAM_MOCK_STATE/subscribed"
    printf '%s\n' "$cal" >> "$GAM_MOCK_STATE/subscribed/$user"
  fi
  printf '  User: %s, Calendar: %s, Added\n' "$user" "$cal"
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
  # The token failure is per account, not per API, so the signature's live wording is assumed here.
  if still_provisioning "$user"; then not_ready_token "$user" "Tasklist Create"; fi
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

# `gam create|add datatransfer|transfer <old> <DataTransferServiceList> <new> [private|shared|all] [release_resources]`
# (doCreateDataTransfer). GAM resolves each owner to a user ID as it reads it (convertEmailAddressToUID): an address that
# isn't a user ends the command there, before any transfer (entityDoesNotExistExit). The same account twice, an alias of
# the old owner included, is a usage error. On success GAM prints "Transfer Request Performed", then the transfer's ID,
# services, From and To (the Data Transfer API's app names). With GAM_MOCK_STATE a transfer stays in progress for the old
# owner, and a second one is Google's 409, which GAM's callGAPI doesn't expect: systemErrorExit(HTTP_ERROR_RC, 9). An <old>
# containing CONFLICT409 does the same without state.
transfer_owner() {  # <address>: the primary it resolves to, or the command ends as GAM's lookup does
  owner=$(lower "$1")
  # A failure trigger is not an account's absence: a pseudo-user, as *pending* is for print datatransfers.
  case "$owner" in *conflict409*|*pending*|*sweep*|*signoutfail*|*fwdfail*) printf '%s' "$owner"; return ;; esac
  if known_user "$owner"; then printf '%s' "$owner"; return; fi
  held=$(duplicate_of "$owner")
  case "$held" in
    "User Alias: "*) printf '%s' "${held##*, User: }"; return ;;
  esac
  does_not_exist_exit "User" "$1"
}
if { [ "${1:-}" = "create" ] || [ "${1:-}" = "add" ]; } && { [ "${2:-}" = "datatransfer" ] || [ "${2:-}" = "transfer" ]; }; then
  [ -n "${3:-}" ] || missing_arg "OldOwnerID"
  old_owner=$(transfer_owner "$3")
  [ -n "${4:-}" ] || missing_arg "DataTransferServiceList"
  services="calendar|datastudio|lookerstudio|data studio|googledrive|gdrive|drive|drive and docs"
  apps=""
  old_ifs=$IFS; IFS=,
  for svc in $4; do
    case "$svc" in
      calendar) app="Calendar" ;;
      datastudio|lookerstudio|"data studio") app="Looker Studio" ;;
      googledrive|gdrive|drive|"drive and docs") app="Drive and Docs" ;;
      *) IFS=$old_ifs; invalid_choice "$svc" "$services" ;;
    esac
    apps="${apps:+$apps,}$app"
  done
  IFS=$old_ifs
  [ -n "${5:-}" ] || missing_arg "NewOwnerID"
  new_owner=$(transfer_owner "$5")
  [ "$old_owner" != "$new_owner" ] || usage_error "New owner must differ from old owner"
  old="$3"; svcs="$4"; new="$5"; shift 5
  while [ $# -gt 0 ]; do
    case "$1" in
      # PRIVACY_LEVEL is a Drive parameter: GAM's _assignAppParameter refuses it (usage error) when no
      # listed app takes it.
      private|shared|all)
        case ",$svcs," in
          *,drive,*|*,googledrive,*|*,gdrive,*|*",drive and docs,"*) ;;
          *) usage_error "No data transfer application for key PRIVACY_LEVEL" ;;
        esac
        shift ;;
      release_resources) shift ;;
      *) invalid_arg "$1" ;;
    esac
  done
  conflict() { echo "ERROR: 409: conflict - Data transfer already in progress for the user." 1>&2; exit 9; }
  case "$old" in *CONFLICT409*) conflict ;; esac
  if [ -n "${GAM_MOCK_STATE:-}" ]; then
    [ ! -e "$GAM_MOCK_STATE/transfers/$old_owner" ] || conflict
    mkdir -p "$GAM_MOCK_STATE/transfers"
    printf '%s\n' "$new_owner" > "$GAM_MOCK_STATE/transfers/$old_owner"
  fi
  echo "Transfer Request Performed"
  echo "  Transfer ID: MockTransfer_$(printf '%s' "$old_owner" | cksum | cut -d' ' -f1)"
  echo "  Service: $apps"
  echo "  From: $old"
  echo "  To: $new"
  exit 0
fi

# `gam print tokens` -> GAM's CSV (_printShowTokens, gam/__init__.py:74077 at 7.50.00): tokens.csv's grants for every
# account but the suspended ones (the default entity, ENTITY_ALL_USERS_NS), a row per grant, the scopes space-joined
# (csv_output_field_delimiter's default). Each account gets its "Getting Access Tokens for" line on stderr, grants or
# not (printGettingEntityItemForWhom). GAM_MOCK_STATE/denied/tokens is ClientAPIAccessDeniedExit, API_ACCESS_DENIED_RC
# 12, on the first account. GAM_MOCK_STATE/tokens_gone/<address> names an account it can't read: entityUnknownWarning,
# then the CSV as usual, and 56 at the end. No option or entity is modelled.
if [ "${1:-}" = "print" ] && { [ "${2:-}" = "tokens" ] || [ "${2:-}" = "token" ]; }; then
  [ $# -eq 2 ] || not_modelled "print tokens with $3"
  active=$(grep -vF '"suspended": true' "$GAM_MOCK_FIXTURES/print_users.json" | sed -n 's/.*"primaryEmail": "\([^"]*\)".*/\1/p' | tr '\n' ' ')
  count=$(printf '%s' "$active" | wc -w | tr -d ' ')
  i=0
  rc=0
  gone=""
  for user in $active; do
    i=$((i + 1))
    printf 'Getting Access Tokens for %s (%s/%s)\n' "$user" "$i" "$count" 1>&2
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/denied/tokens" ]; then
      printf '\nERROR: Not Authorized to access this resource/api\n\nERROR: Reauthentication is needed, please run\n\ngam oauth create\n' 1>&2
      exit 12
    fi
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/tokens_gone/$user" ]; then
      printf 'User: %s, Does not exist (%s/%s)\n' "$user" "$i" "$count" 1>&2
      gone="$gone $user"
      rc=56
    fi
  done
  head -n 1 "$GAM_MOCK_FIXTURES/tokens.csv"
  tail -n +2 "$GAM_MOCK_FIXTURES/tokens.csv" | while IFS= read -r line; do
    user=${line%%,*}
    case "$gone " in *" $user "*) continue ;; esac
    case " $active " in *" $user "*) printf '%s\n' "$line" ;; esac
  done
  exit $rc
fi

# `gam print chromeprofiles filter user_email="<address>" fields <ChromeProfileFieldNameList> formatjson` -> GAM's CSV,
# `name`, `profileId` and a `JSON` column per profile (doPrintShowChromeProfiles, gam/__init__.py:26525-26592 at 7.48.30;
# a person's page, Security tab): the profiles in mock_gam/chrome_profiles.json signed in with that address, none the
# titles alone (exit 0). Only that filter is modelled (the Chrome Management API's own field name, the address quoted);
# any other filter or option, or no formatjson, is refused; a field outside the grammar's <ChromeProfileFieldName> is
# getFieldsList's invalid choice. GAM_MOCK_STATE/noscope/chromemanagement is an authorization with no Chrome Management
# scope (buildGAPIObject: NO_SCOPES_FOR_API_RC, 15, before any request); GAM_MOCK_STATE/denied/chromeprofiles the API's
# refusal, without the Profiles scope (entityActionFailedExit, ACTION_FAILED_RC, 50; Google's words approximate).
# Not modelled: whether the API matches the address in another case, and what an unreported profile lacks.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "chromeprofiles" ]; then
  shift 2; fj=""; owner=""
  while [ $# -gt 0 ]; do
    case "$1" in
      filter|filters) need_value $# "String"
        case "$2" in user_email=\"?*\") owner=${2#user_email=\"}; owner=${owner%\"} ;; *) not_modelled "print chromeprofiles filter $2" ;; esac
        case "$owner" in *\"*|*\\*|*\**|*[[:space:]]*) not_modelled "print chromeprofiles filter $2" ;; esac
        shift 2 ;;
      fields) need_value $# "ChromeProfileFieldNameList"; [ -n "$2" ] || empty_arg "ChromeProfileFieldNameList"
        for field in $(printf '%s' "$2" | tr ',' ' '); do
          case "$field" in
            affiliationstate|annotatedlocation|annotateduser|attestationcredential|browserchannel|browserversion|deviceinfo|\
            displayname|extensioncount|firstenrollmenttime|identityprovider|lastactivitytime|lastpolicyfetchtime|\
            lastpolicysynctime|laststatusreporttime|name|osplatformtype|osplatformversion|osversion|policycount|profileid|\
            profilepermanentid|reportingdata|useremail|userid) ;;
            *) invalid_choice "$field" "affiliationstate|annotatedlocation|annotateduser|attestationcredential|browserchannel|browserversion|deviceinfo|displayname|extensioncount|firstenrollmenttime|identityprovider|lastactivitytime|lastpolicyfetchtime|lastpolicysynctime|laststatusreporttime|name|osplatformtype|osplatformversion|osversion|policycount|profileid|profilepermanentid|reportingdata|useremail|userid" ;;
          esac
        done
        shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print chromeprofiles with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print chromeprofiles without formatjson"
  [ -n "$owner" ] || not_modelled "print chromeprofiles without a user_email filter"
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/noscope/chromemanagement" ]; then
    printf '\nERROR: There are no scopes authorized for the API(s): Chrome Management API\n' 1>&2; exit 15
  fi
  printf 'Getting all Chrome Profiles that match query (user_email="%s")\n' "$owner" 1>&2
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/denied/chromeprofiles" ]; then
    printf '\nERROR: Chrome Profile: user_email="%s", Print Failed: Request had insufficient authentication scopes.\n' "$owner" 1>&2
    exit 50
  fi
  echo "name,profileId,JSON"
  while IFS= read -r record; do
    [ "$(json_text userEmail "$record")" = "$owner" ] || continue
    gam_row "$(json_text name "$record")" "$(json_text profileId "$record")" "$record"
  done < "$GAM_MOCK_FIXTURES/chrome_profiles.json"
  exit 0
fi

# `gam print mobile query email:<address> fields <MobileFieldNameList> formatjson` -> GAM's CSV, `resourceId` and a `JSON`
# column per device (doPrintMobileDevices, gam/__init__.py:34750-34891 at 7.48.30; the compromised-account check): the
# devices in mock_gam/mobile_devices.json with an owner the address starts, as the API's `email:` search matches a
# prefix (carol@example.com.au's phone answers carol@example.com), none the titles alone (exit 0). Only that
# query (_validateDeviceQuery: a field, a colon, a value, no `?`, else a usage error) and the grammar's field names;
# formatjson needs `resourceid` among them (its row is written from it). GAM_MOCK_STATE/denied/mobile is the API's
# refusal: ClientAPIAccessDeniedExit, the error then "Reauthentication is needed", API_ACCESS_DENIED_RC 12. Not modelled:
# Google's search beyond an owner's prefix.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "mobile" ]; then
  shift 2; fj=""; owner=""; fields=""; limit=1
  while [ $# -gt 0 ]; do
    case "$1" in
      listlimit) need_value $# "Number"; limit=$2; shift 2 ;;
      query) need_value $# "QueryMobile"
        case "$2" in *\?*|:*|*:) usage_error "Invalid Mobile Device query: $2" ;; email:?*) owner=${2#email:} ;;
          *:*) not_modelled "print mobile query $2" ;; *) usage_error "Invalid Mobile Device query: $2" ;; esac
        shift 2 ;;
      fields) need_value $# "MobileFieldNameList"; [ -n "$2" ] || empty_arg "MobileFieldNameList"; fields="$2"
        for field in $(printf '%s' "$2" | tr ',' ' '); do
          case "$field" in
            adbstatus|applications|basebandversion|bootloaderversion|brand|buildnumber|defaultlanguage|developeroptionsstatus|\
            devicecompromisedstatus|deviceid|devicepasswordstatus|email|encryptionstatus|firstsync|hardware|hardwareid|imei|\
            kernelversion|lastsync|managedaccountisonownerprofile|manufacturer|meid|model|name|networkoperator|os|\
            otheraccountsinfo|privilege|releaseversion|resourceid|securitypatchlevel|serialnumber|status|supportsworkprofile|\
            type|unknownsourcesstatus|useragent|wifimacaddress) ;;
            *) invalid_choice "$field" "MobileFieldName" ;;
          esac
        done
        shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print mobile with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print mobile without formatjson"
  [ -n "$owner" ] || not_modelled "print mobile without an email: query"
  case ",$fields," in *,resourceid,*) ;; *) not_modelled "print mobile formatjson without resourceid (GAM's row needs it)" ;; esac
  printf 'Getting all Mobile Devices that match query (email:%s)\n' "$owner" 1>&2
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/denied/mobile" ]; then
    printf '\nERROR: Not Authorized to access this resource/api\n\nERROR: Reauthentication is needed, please run\n\ngam oauth create\n' 1>&2
    exit 12
  fi
  echo "resourceId,JSON"
  while IFS= read -r record; do
    # GAM's listLimit defaults to 1 and cleanJSON cuts every list to it, so a shared device shows only its first owner.
    shown=$record
    [ "$limit" = 0 ] || shown=$(printf '%s' "$record" | sed -E 's/("email": \["[^"]*")(, "[^"]*")*\]/\1]/')
    case "$record" in *"\"$owner"*) gam_row "$(json_text resourceId "$record")" "$shown" ;; esac   # a prefix, as the API's
  done < "$GAM_MOCK_FIXTURES/mobile_devices.json"
  exit 0
fi

# `gam print devices query email:<address> fields <DeviceFieldNameList> userfields <DeviceUserFieldNameList> formatjson` ->
# GAM's CSV, `name` and a `JSON` column per device (doPrintCIDevices, gam/__init__.py:32609-32780 at 7.48.30; the compromised-
# account check's computers): the devices in mock_gam/ci_devices.json with a user the address starts, as Google's `email:`
# device search matches a prefix (carol@example.com.au's PC answers carol@example.com), each with its users (GAM's default
# lists them and attaches each to its device); none is the titles alone (exit 0). The service account impersonates the
# admin, and GAM builds that before it reads an argument, checking its own scope list before it asks Google:
# GAM_MOCK_STATE/denied/devices is a service account scope list without the API (getSvcAcctCredentials:
# SvcAcctAPIAccessDeniedExit, API_ACCESS_DENIED_RC 12), which wins over GAM_MOCK_STATE/noscope/devices, the scope not
# delegated (handleOAuthTokenError with softErrors: userServiceNotEnabledWarning for the admin, SERVICE_NOT_APPLICABLE_RC
# 73); GAM_MOCK_STATE/badkey/devices a dead key (an invalid_grant handleOAuthTokenError doesn't know: "Authentication Token
# Error", then APIAccessDeniedExit, 12). Then only that query (_validateDeviceQuery: a field, a colon, a value, no `?`,
# else GAM's usage error) and the grammar's field names (getFieldsList: an invalid choice, GAM's list sorted); the app's
# own fields only, as the fixture holds them (GAM fetches only the fields named). Not modelled: Google's search beyond a
# user's prefix, the views (company, personal), and the other options.
if [ "${1:-}" = "print" ] && [ "${2:-}" = "devices" ]; then
  denied='ERROR: API access Denied\nPlease make sure the Service Account Client ID: 1 is authorized for the appropriate API or scopes: Cloud Identity API - Devices\n\nRun: gam user admin@example.com update serviceaccount\n'
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/denied/devices" ]; then printf "\n$denied" 1>&2; exit 12; fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/badkey/devices" ]; then
    printf "\nERROR: Authentication Token Error - invalid_grant: Invalid JWT Signature\n\n$denied" 1>&2; exit 12
  fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/noscope/devices" ]; then
    printf 'User: admin@example.com, Cloud Identity API - Devices Service/App not enabled\n' 1>&2; exit 73
  fi
  shift 2; fj=""; owner=""; dwant=" "; uwant=" "
  while [ $# -gt 0 ]; do
    word=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '_')
    case "$word" in
      query) need_value $# "QueryDevice"
        bad="Invalid Device query \"$2\"; it must be if the form \"field:value\" and must not contain a \"?\""
        case "$2" in *\?*|:*|*:) usage_error "$bad" ;; email:?*) owner=${2#email:} ;;
          *:*) not_modelled "print devices query $2" ;; *) usage_error "$bad" ;; esac
        shift 2 ;;
      fields) need_value $# "DeviceFieldNameList"; [ -n "$2" ] || empty_arg "DeviceFieldNameList"
        for field in $(printf '%s' "$2" | tr ',' ' '); do
          case "$field" in
            androidspecificattributes|assettag|basebandversion|bootloaderversion|brand|buildnumber|compromisedstate|createtime|\
            deviceid|devicetype|enableddeveloperoptions|enabledusbdebugging|encryptionstate|endpointverificationspecificattributes|\
            hostname|imei|kernelversion|lastsynctime|managementstate|manufacturer|meid|model|name|networkoperator|osversion|\
            otheraccounts|ownertype|releaseversion|securitypatchtime|serialnumber|unifieddeviceid|wifimacaddresses) dwant="$dwant$field " ;;
            *) invalid_choice "$field" "androidspecificattributes|assettag|basebandversion|bootloaderversion|brand|buildnumber|compromisedstate|createtime|deviceid|devicetype|enableddeveloperoptions|enabledusbdebugging|encryptionstate|endpointverificationspecificattributes|hostname|imei|kernelversion|lastsynctime|managementstate|manufacturer|meid|model|name|networkoperator|osversion|otheraccounts|ownertype|releaseversion|securitypatchtime|serialnumber|unifieddeviceid|wifimacaddresses" ;;
          esac
        done
        shift 2 ;;
      userfields) need_value $# "DeviceUserFieldNameList"; [ -n "$2" ] || empty_arg "DeviceUserFieldNameList"
        for field in $(printf '%s' "$2" | tr ',' ' '); do
          case "$field" in
            compromisedstate|createtime|firstsynctime|languagecode|lastsynctime|managementstate|name|passwordstate|useragent|useremail) uwant="$uwant$field " ;;
            *) invalid_choice "$field" "compromisedstate|createtime|firstsynctime|languagecode|lastsynctime|managementstate|name|passwordstate|useragent|useremail" ;;
          esac
        done
        shift 2 ;;
      formatjson) fj=1; shift ;;
      *) not_modelled "print devices with $1" ;;
    esac
  done
  [ -n "$fj" ] || not_modelled "print devices without formatjson"
  [ -n "$owner" ] || not_modelled "print devices without an email: query"
  for f in devicetype model manufacturer osversion hostname serialnumber ownertype managementstate compromisedstate createtime lastsynctime; do
    case "$dwant" in *" $f "*) ;; *) not_modelled "print devices without field $f (the fixture holds it; GAM fetches only the fields named)" ;; esac
  done
  for f in name useremail createtime firstsynctime lastsynctime managementstate; do
    case "$uwant" in *" $f "*) ;; *) not_modelled "print devices without user field $f (the fixture holds it; GAM fetches only the fields named)" ;; esac
  done
  printf 'Getting all Devices that match query (email:%s), may take some time on a large Google Workspace Account...\n' "$owner" 1>&2
  n=$(grep -c "\"userEmail\": \"$owner" "$GAM_MOCK_FIXTURES/ci_devices.json" || true)
  printf 'Got %s Devices...\n' "$n" 1>&2
  printf 'Getting all Device Users that match query (email:%s), may take some time on a large Google Workspace Account...\n' "$owner" 1>&2
  printf 'Got %s Device Users...\n' "$n" 1>&2
  echo "name,JSON"
  while IFS= read -r record; do
    case "$record" in *"\"userEmail\": \"$owner"*) ;; *) continue ;; esac   # a prefix, as the API's
    # A device user `block deviceuser` blocked shows BLOCKED; one `delete deviceuser` deleted is gone (GAM_MOCK_STATE/deviceusers).
    for user in $(printf '%s' "$record" | grep -o '"name": "devices/[^"]*/deviceUsers/[^"]*"' | sed 's/^"name": "//; s/"$//'); do
      sfile="${GAM_MOCK_STATE:-/nonexistent}/deviceusers/$(printf '%s' "$user" | tr '/' '_')"
      [ -f "$sfile" ] || continue
      case "$(cat "$sfile")" in
        BLOCKED) record=$(printf '%s' "$record" | sed "s#\"managementState\": \"[A-Z_]*\", \"name\": \"$user\"#\"managementState\": \"BLOCKED\", \"name\": \"$user\"#") ;;
        DELETED) case "$record" in *"\"name\": \"$user\", \"userEmail\": \"$owner"*) record="" ;; esac ;;   # the person gone from it
      esac
    done
    [ -n "$record" ] && gam_row "$(json_text name "$record")" "$record"
  done < "$GAM_MOCK_FIXTURES/ci_devices.json"
  exit 0
fi

# `gam calendars <list> transfer ownership <user>` (doCalendarsTransferOwnership, :55992-56014; GamUpdate 7.46.04;
# the grammar's 1690 lacks `ownership`, which getChoice requires: anything else is an invalid choice). Each calendar
# in the comma list in turn, "(i/n)" after each line when there's more than one: validateCalendar gets it with the
# admin's own access first, so one the admin can't see (here: one the fixtures don't hold) fails "Not Found" before
# any transfer; then transferOwnership as the admin: "Calendar: <id>, Ownership Transferred to: <user>" on stdout.
# A failure leaves the rest done, ACTION_FAILED_RC (50) at the end. (The new owner's refusal wording is
# approximate: the API's reason.)
if [ "${1:-}" = "calendars" ] && [ "${3:-}" = "transfer" ]; then
  [ $# -ge 4 ] || missing_arg "ownership"
  [ "$4" = "ownership" ] || invalid_choice "$4" "ownership"
  [ $# -ge 5 ] || missing_arg "EmailAddress"
  [ $# -eq 5 ] || invalid_arg "$6"
  to=$(lower "$5"); failed=""
  set -f; ids=$(printf '%s' "$2" | tr ',' ' '); set +f
  n=$(printf '%s\n' $ids | grep -c . || true); i=0
  for id in $ids; do
    i=$((i + 1)); of=""; [ "$n" -gt 1 ] && of=" ($i/$n)"
    owner=$(calendar_owner "$id")
    # validateCalendar reads a `.calendar.google.com` ID as the admin, so one not shared with them isn't found, whoever owns it.
    grep -qixF "$id" "$GAM_MOCK_FIXTURES/calendars_shared_with_admin.txt" || owner=""
    if [ -z "$owner" ]; then
      printf 'Calendar: %s, Transfer Ownership Failed: Not Found%s\n' "$id" "$of" 1>&2; failed=1; continue
    fi
    if ! known_user "$to"; then
      printf 'Calendar: %s, Transfer Ownership to: %s, Transfer Ownership Failed: Invalid Value%s\n' "$id" "$to" "$of" 1>&2
      failed=1; continue
    fi
    if [ -n "${GAM_MOCK_STATE:-}" ]; then
      mkdir -p "$GAM_MOCK_STATE/calendar_owner"; printf '%s' "$to" > "$GAM_MOCK_STATE/calendar_owner/$(lower "$id")"
    fi
    printf 'Calendar: %s, Ownership Transferred to: %s%s\n' "$id" "$to" "$of"
  done
  [ -z "$failed" ] || exit 50
  exit 0
fi

# `gam user <u> print filters labelidsonly formatjson` (printShowFilters, :77655-77712) -> CSV, each filter's
# flattened columns (in GAM's sort order) then its JSON. `user_filters/<u>.csv` is that output as GAM would print it;
# with GAM_MOCK_STATE, a filter `delete filters` removed is left out (the header stays the fixture's, where GAM keeps
# only the columns of rows written: the app reads the JSON). None: GAM's titles alone, `User,id`. As the person:
# "Getting Filters for <u>" first; Gmail off is userGmailServiceNotEnabledWarning, exit 73, then the titles.
deleted_filter() { [ -n "${GAM_MOCK_STATE:-}" ] && grep -qxF "$2" "$GAM_MOCK_STATE/filters_deleted/$1" 2>/dev/null; }
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "filters" ]; then
  user=$(lower "$2"); shift 4
  [ "$*" = "labelidsonly formatjson" ] || not_modelled "print filters with $*"
  need_user "$user" Print
  printf 'Getting Filters for %s\n' "$user" 1>&2
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
    printf 'User: %s, Gmail Service/App not enabled\n' "$user" 1>&2; printf 'User,id\n'; exit 73
  fi
  file="$GAM_MOCK_FIXTURES/user_filters/$user.csv"; rows=""
  if [ -f "$file" ]; then
    while IFS= read -r row; do
      id=$(printf '%s' "$row" | cut -d, -f2)
      deleted_filter "$user" "$id" || rows="$rows$row
"
    done <<EOF_ROWS
$(tail -n +2 "$file")
EOF_ROWS
  fi
  if [ -n "$rows" ]; then head -n 1 "$file"; printf '%s' "$rows"; else printf 'User,id\n'; fi
  exit 0
fi

# `gam user <u> delete filters <FilterIDEntity>` (deleteFilters, :77581-77604): "User: <u>, Delete <n> Filter(s)",
# then each in turn, indented, "User: <u>, Filter: <id>, Deleted (j/n)"; one Gmail doesn't have fails "Delete Failed:
# Not Found" (the API's words, approximate) and the rest are still deleted, exit 50 at the end. An empty list is
# NO_ENTITIES_FOUND_RC (60) after the count line; Gmail off, the count line, then the indented warning, exit 73.
# Not modelled, so never answered more kindly than GAM: whitespace (Python's split takes more than sh's, and getChoice
# strips it first) or anything but ASCII letters, digits, `_`, `-` and commas; and a list selector word (file, csv,
# csvfile, csvkmd, csvsubkey, csvdata, ignoring case, `_` and `-`), which GAM reads a file from (getEntityList).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "delete" ] && [ "${4:-}" = "filters" ]; then
  user=$(lower "$2")
  [ $# -ge 5 ] || missing_arg "FilterIDEntity"
  [ $# -eq 5 ] || invalid_arg "$6"
  case "$5" in *[!A-Za-z0-9_,-]*) not_modelled "a filter ID list like $5" ;; esac
  case "$(lower "$5" | tr -d '_-')" in csvkmd|csvsubkey|csv|csvfile|file|csvdata) not_modelled "delete filters from $5" ;; esac
  need_user "$user" Delete
  set -f; ids=$(printf '%s' "$5" | tr ',' ' '); set +f
  n=$(printf '%s\n' $ids | grep -c . || true); i=0; failed=""
  if [ "$n" -eq 1 ]; then noun=Filter; else noun=Filters; fi
  printf 'User: %s, Delete %s %s\n' "$user" "$n" "$noun"
  [ "$n" -gt 0 ] || exit 60
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
    printf '  User: %s, Gmail Service/App not enabled\n' "$user" 1>&2; exit 73
  fi
  for id in $ids; do
    i=$((i + 1)); of=""; [ "$n" -gt 1 ] && of=" ($i/$n)"
    if deleted_filter "$user" "$id" || ! grep -q "^[^,]*,$id," "$GAM_MOCK_FIXTURES/user_filters/$user.csv" 2>/dev/null; then
      printf '  User: %s, Filter: %s, Delete Failed: Not Found%s\n' "$user" "$id" "$of" 1>&2; failed=1; continue
    fi
    if [ -n "${GAM_MOCK_STATE:-}" ]; then mkdir -p "$GAM_MOCK_STATE/filters_deleted"; printf '%s\n' "$id" >> "$GAM_MOCK_STATE/filters_deleted/$user"; fi
    printf '  User: %s, Filter: %s, Deleted%s\n' "$user" "$id" "$of"
  done
  [ -z "$failed" ] || exit 50
  exit 0
fi

# `gam user <u> print vaultholds|holds` (printShowUserVaultHolds, :45612-45690) -> CSV, no formatjson: every
# open matter's holds that name the person or an org unit above them, `User,matterId,matterName,holdId,name,
# orgUnitId,orgUnitPath` (its titles in their given order: `sortall` re-inserts each as given), the header alone
# for none. `vault_holds.csv` holds the rows. GAM acts as the admin throughout: it lists the open matters and their
# holds first, so a Vault refusal fails before anything prints, entityActionFailedWarning "Vault Hold Print Failed:
# <reason>", ACTION_FAILED_RC (50), modelled by `$GAM_MOCK_STATE/novault` (the reason is the API's; a token
# whose file names no Vault scope exits 15 earlier); then it reads each person's org units with the Directory, and
# one who isn't a user ends it: the command echo, "ERROR: User: <u>, Does not exist", ENTITY_DOES_NOT_EXIST_RC
# (getAllParentOrgUnitsForUser -> entityDoesNotExistExit, :1762-1768, :800-805).
if [ "${1:-}" = "user" ] && [ "${3:-}" = "print" ] && { [ "${4:-}" = "vaultholds" ] || [ "${4:-}" = "holds" ]; }; then
  user=$(lower "$2"); shift 4
  if [ $# -gt 0 ]; then
    case "$1" in todrive) not_modelled "print vaultholds todrive" ;; *) invalid_arg "$1" ;; esac
  fi
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/novault" ]; then
    echo "Vault Hold Print Failed: Request had insufficient authentication scopes." 1>&2; exit 50
  fi
  if ! known_user "$user"; then
    printf 'Command: gam %s\n\nERROR: User: %s, Does not exist\n' "$ARGS" "$user" 1>&2; exit 56
  fi
  echo "User,matterId,matterName,holdId,name,orgUnitId,orgUnitPath"
  grep -i "^$user," "$GAM_MOCK_FIXTURES/vault_holds.csv" || true
  exit 0
fi

# `gam user <email> imap|imap4 <Boolean> [noautoexpunge] [expungebehavior archive|deleteforever|trash]
#  [maxfoldersize 0|1000|2000|5000|10000]` (8385) / `pop|pop3 <Boolean> [for allmail|newmail|mailfromnowon|fromnowon]
#  [action keep|leaveininbox|archive|delete|trash|markread]` (8391). GAM's setImap and setPop (7.48.26): the options
# first, then per user a token (not_a_user, "Set Failed") and one Gmail settings call, Gmail off its
# userGmailServiceNotEnabledWarning (exit 73); printed as _showImap and _showPop print the settings Gmail returns.
if [ "${1:-}" = "user" ] && case "${3:-}" in imap|imap4|pop|pop3) true ;; *) false ;; esac; then
  user=$2; setting=$3
  [ $# -ge 4 ] || missing_arg "true|false"
  boolean=$(printf '%s' "$4" | tr '[:upper:]' '[:lower:]')
  case "$boolean" in
    true|on|yes|enabled|1) enabled=True ;;
    false|off|no|disabled|0) enabled=False ;;
    *) invalid_choice "$boolean" "true|false" ;;
  esac
  # _popDefaults: `disabled` when off; `for` overwrites it, so `pop off for allmail` turns POP on, as in GAM.
  expunge=True; behavior=archive; size=0; disposition=leaveInInbox
  if [ "$enabled" = True ]; then window=allMail; else window=disabled; fi
  case "$setting" in imap*) kind=imap ;; *) kind=pop ;; esac
  shift 4
  while [ $# -gt 0 ]; do
    # Exact patterns: a glob here would let `x:noautoexpunge` through.
    case "$kind:$1" in
      imap:noautoexpunge) expunge=False; shift ;;
      imap:expungebehavior)
        [ $# -ge 2 ] || missing_arg "archive|deleteforever|trash"
        case "$2" in archive|trash) behavior=$2 ;; deleteforever) behavior=deleteForever ;;
          *) invalid_choice "$2" "archive|deleteforever|trash" ;; esac
        shift 2 ;;
      imap:maxfoldersize)
        [ $# -ge 2 ] || missing_arg "0|1000|2000|5000|10000"
        case "$2" in 0|1000|2000|5000|10000) size=$2 ;; *) invalid_choice "$2" "0|1000|2000|5000|10000" ;; esac
        shift 2 ;;
      pop:for)
        [ $# -ge 2 ] || missing_arg "allmail|fromnowon|mailfromnowon|newmail"
        case "$2" in allmail) window=allMail ;; fromnowon|mailfromnowon|newmail) window=fromNowOn ;;
          *) invalid_choice "$2" "allmail|fromnowon|mailfromnowon|newmail" ;; esac
        shift 2 ;;
      pop:action)
        [ $# -ge 2 ] || missing_arg "archive|delete|keep|leaveininbox|markread|trash"
        case "$2" in archive) disposition=archive ;; delete|trash) disposition=trash ;; keep|leaveininbox) disposition=leaveInInbox ;;
          markread) disposition=markRead ;; *) invalid_choice "$2" "archive|delete|keep|leaveininbox|markread|trash" ;; esac
        shift 2 ;;
      pop:confirm) shift ;;
      *) invalid_arg "$1" ;;
    esac
  done
  known_user "$user" || not_a_user "$user" Set
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
    printf 'User: %s, Gmail Service/App not enabled\n' "$user" 1>&2; exit 73
  fi
  case "$kind" in
    imap) echo "User: $user, IMAP Enabled: $enabled, autoExpunge: $expunge, expungeBehavior: $behavior, maxFolderSize: $size" ;;
    *) if [ "$window" != disabled ]; then echo "User: $user, POP Enabled: True, For: $window, Action: $disposition"
       else echo "User: $user, POP Enabled: False"; fi ;;
  esac
  exit 0
fi

# `all users print messages query "rfc822msgid:<id> in:spam" includespamtrash headers message-id showlabels`: the Phishing
# screen's read of the copies Gmail filed to Spam (GamCommands.findSpamCopies, SpamCopies), strict, as GAM 7.48.30's
# printShowMessagesThreads runs it over every active user (isSuspended=False, by address), from mock_gam/messages.csv and
# each message's mock_gam/message_headers/<id>. With GAM_MOCK_STATE, nogmail/<user> is Gmail off, deleted/<id> gone and
# trashed/<id> in Trash with its SPAM label kept beside TRASH (whether Gmail's messages.trash drops SPAM isn't known here,
# so the app has to skip a copy in Trash by that label either way). Labels are the fixture's system labels, never a user
# label whose name holds a space (GAM joins names with one; the reader's own tests carry that case). As GAM does:
#   - every argument read first (getArgument: lowercased, `_` dropped), `query` needing a value; only this one form;
#   - the users listed ("Got <n> Users..."), then each in order, numbered (i/n) past one: showlabels reads the labels
#     first, so Gmail off is userGmailServiceNotEnabledWarning (73) before that user's "Getting all Messages…" line; the
#     list, Spam and Trash included (includeSpamTrash), of that Message-ID in Spam; none sets NO_ENTITIES_FOUND_RC (60);
#   - the CSV once every user is read (CSVPrintFile): User,threadId,id, the Message-ID header under its SMTP_HEADERS_MAP
#     name, LabelsCount,Labels; the header line even with no row; the last code set stands (setSysExitRC).
if [ "${1:-}" = "all" ] && [ "${2:-}" = "users" ] && [ "${3:-}" = "print" ] && [ "${4:-}" = "messages" ]; then
  shift 4
  query=""; queries=0; spamtrash=""; header=""; labels=""
  while [ $# -gt 0 ]; do
    word=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '_')
    case "$word" in
      query)
        [ $# -ge 2 ] || missing_arg "Query"
        [ -n "$2" ] || empty_arg "Query"
        queries=$((queries + 1)); query=$2; shift 2 ;;
      includespamtrash) spamtrash=1; shift ;;
      headers)
        [ $# -ge 2 ] || missing_arg "String"
        [ "$(printf '%s' "$2" | tr '[:upper:]' '[:lower:]')" = message-id ] || not_modelled "all users print messages with headers $2"
        header=1; shift 2 ;;
      showlabels) labels=1; shift ;;
      *) not_modelled "all users print messages with $1" ;;
    esac
  done
  { [ "$queries" -eq 1 ] && [ -n "$spamtrash" ] && [ -n "$header" ] && [ -n "$labels" ]; } \
    || not_modelled "all users print messages other than the read of copies in Spam"
  case "$query" in
    "rfc822msgid:"*" in:spam") msgid=${query#rfc822msgid:}; msgid=${msgid% in:spam} ;;
    *) not_modelled "a Gmail query other than rfc822msgid:<id> in:spam" ;;
  esac
  case "$msgid" in ''|*[[:space:]]*) not_modelled "a Gmail query other than rfc822msgid:<id> in:spam" ;; esac
  users=$(grep -vF '"suspended": true' "$GAM_MOCK_FIXTURES/print_users.json" \
    | sed -n 's/.*"primaryEmail": "\([^"]*\)".*/\1/p' | LC_ALL=C sort)
  count=$(printf '%s\n' "$users" | grep -c . || true)
  printf 'Getting all Users that match query (isSuspended=False isGuest=False), may take some time on a large Google Workspace Account...\n' 1>&2
  got_users "$count"
  rows=""; rc=0; i=0
  for user in $users; do
    i=$((i + 1))
    if [ "$count" -gt 1 ]; then n=" ($i/$count)"; else n=""; fi
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
      printf 'User: %s, Gmail Service/App not enabled%s\n' "$user" "$n" 1>&2
      rc=73; continue
    fi
    printf 'Getting all Messages that match query ((%s)) for %s%s\n' "$query" "$user" "$n" 1>&2
    found=0
    while IFS=, read -r owner id message tags; do
      { [ "$owner" = "$user" ] && [ "$message" = "$msgid" ]; } || continue
      case " $tags " in *" SPAM "*) ;; *) continue ;; esac
      if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/deleted/$id" ]; then continue; fi
      if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/trashed/$id" ]; then tags="$tags TRASH"; fi
      data="$GAM_MOCK_FIXTURES/message_headers/$id"
      [ -f "$data" ] || not_modelled "message $id's headers"
      set -- $tags
      rows="$rows$(gam_row "$user" "$(sed -n 's/^Gmail-ThreadId: //p' "$data")" "$id" \
        "$(sed -n 's/^Message-ID: //p' "$data" | head -n 1)" "$#" "$tags")
"
      found=$((found + 1))
    done < "$GAM_MOCK_FIXTURES/messages.csv"
    if [ "$found" -eq 1 ]; then noun=Message; else noun=Messages; fi
    printf '\rGot %s %s that matched query ((%s)) for %s...\r\n' "$found" "$noun" "$query" "$user" 1>&2
    [ "$found" -gt 0 ] || rc=60
  done
  gam_row User threadId id Message-ID LabelsCount Labels
  printf '%s' "$rows"
  exit "$rc"
fi

# `<user> trash messages ids <MessageIDEntity>` (8197-8201; _processMessagesThreads with ids: nothing is listed, so Spam is no
# bar; no doit, no count check): the Phishing screen's trash of one mailbox's copies in Spam, by the IDs SpamCopies read. As
# GAM 7.48.30 does: a token for the user; getProfile, so Gmail off is userGmailServiceNotEnabledWarning (73); "User: <u>,
# Trash N Messages"; then Gmail's messages.trash on each ID, a batch request each: "  User: <u>, Message: <id>, Trashed
# (j/N)", or for an ID the mailbox doesn't have (messages.csv, not deleted) "  User: <u>, Message: <id>, Trash Failed: Does
# not exist (j/N)" on stderr (Gmail's notFound by _GMAIL_ERROR_REASON_TO_MESSAGE_MAP; entityActionFailedWarning, exit 50),
# the rest trashed. A copy already in Trash is trashed again without complaint (messages.trash as idempotent: assumed, not
# seen live). With GAM_MOCK_STATE a trashed message is $GAM_MOCK_STATE/trashed/<id>. Only a list of ASCII letters, digits,
# `-`, `_` and commas is modelled: GAM splits on whitespace too, and reads a file for a list selector. A word after the list
# that GAM takes there (doit, a count, csv…) isn't modelled, any other is GAM's invalid argument; and a suspended user's
# mailbox isn't, as for the trash by query: what GAM says for one is unknown.
if [ "${1:-}" = "user" ] && [ "${3:-}" = "trash" ] && [ "${4:-}" = "messages" ] && [ "${5:-}" = "ids" ]; then
  user=$2
  [ $# -ge 6 ] || missing_arg "MessageIDEntity"
  if [ $# -gt 6 ]; then
    case "$(printf '%s' "$7" | tr '[:upper:]' '[:lower:]' | tr -d '_')" in
      doit|quick|notquick|maxtotrash|maxtoprocess|maxmessagesperthread|query|querytime*|matchlabel|or|and|labelids|\
      labelmatchpattern|sendermatchpattern|ids|csv) not_modelled "trash messages ids with $7" ;;
      *) invalid_arg "$7" ;;
    esac
  fi
  case "$6" in *[!A-Za-z0-9_,-]*) not_modelled "a message ID list like $6" ;; esac
  case "$(printf '%s' "$6" | tr '[:upper:]' '[:lower:]' | tr -d '_-')" in csvkmd|csvsubkey|csv|csvfile|file|csvdata) not_modelled "trash messages ids from $6" ;; esac
  known_user "$user" || token_refused "$user" Trash
  ! grep -F "\"primaryEmail\": \"$user\"" "$GAM_MOCK_FIXTURES/print_users.json" | grep -qF '"suspended": true' \
    || not_modelled "trash messages as a suspended user"
  if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
    printf 'User: %s, Gmail Service/App not enabled\n' "$user" 1>&2; exit 73
  fi
  set -f; ids=$(printf '%s' "$6" | tr ',' ' '); set +f
  n=$(printf '%s\n' $ids | grep -c . || true); failed=""
  if [ "$n" -eq 0 ]; then printf 'User: %s, Messages: 0, Not Trashed: No Messages matched\n' "$user" 1>&2; exit 60; fi
  if [ "$n" -eq 1 ]; then noun=Message; else noun=Messages; fi
  printf 'User: %s, Trash %s %s\n' "$user" "$n" "$noun"
  j=0
  for id in $ids; do
    j=$((j + 1)); of=""; [ "$n" -gt 1 ] && of=" ($j/$n)"
    if grep -q "^$user,$id," "$GAM_MOCK_FIXTURES/messages.csv" \
       && ! { [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/deleted/$id" ]; }; then
      if [ -n "${GAM_MOCK_STATE:-}" ]; then mkdir -p "$GAM_MOCK_STATE/trashed"; : > "$GAM_MOCK_STATE/trashed/$id"; fi
      printf '  User: %s, Message: %s, Trashed%s\n' "$user" "$id" "$of"
    else
      printf '  User: %s, Message: %s, Trash Failed: Does not exist%s\n' "$user" "$id" "$of" 1>&2
      failed=1
    fi
  done
  [ -z "$failed" ] || exit 50
  exit 0
fi

# ---- One email out of every mailbox (docs/domains/phishing.md) --------------------------------------------------------
# `<UserTypeEntity> trash messages (query <QueryGmail>)+ [quick|notquick] [doit] [max_to_trash <Number>]` (8197-8201), as
# GAM 7.48.30's _processMessagesThreads runs it, for `all users` (every active user, by address: isSuspended=False) or
# `user <email>`. The mailboxes are mock_gam/messages.csv (`user,id,rfc822msgid,labels`, a message a line, made up); with
# GAM_MOCK_STATE a trashed message is $GAM_MOCK_STATE/trashed/<id>, and $GAM_MOCK_STATE/nogmail/<user> a person with
# Gmail off. Only the query the app sends, `rfc822msgid:<id> -in:sent`, is modelled; any other, and the grammar's other
# options, are refused as not modelled. As GAM does:
#   - every argument is read first, each keyword lowercased with `_` dropped (getArgument): `query` needs a value
#     (getString: missing or empty, Cmd.OB_QUERY), `max_to_trash` an integer x>=0 (getInteger), 0 being no limit; any
#     other word is an invalid argument; no query at all is "Missing argument: Expected <query|matchlabel|ids|labelids>";
#   - then per user: a token (token_refused, "User Trash Failed"); Gmail off, userGmailServiceNotEnabledWarning (73); the list
#     leaves Spam and Trash out (includeSpamTrash is False for trash) and, with `quick` (the default), asks Gmail for at
#     most max_to_trash, so only `notquick` sees more than the cap;
#   - none is "Messages: 0, Not Trashed: No Messages matched" (60); over the cap, "Count N exceeds maximum to Trash M"
#     (51); without doit, 'Use the "doit" argument to perform action' (51), each on stderr with (i/n) past one user;
#   - with doit, "Trash N Messages" and each message's "Trashed" on stdout;
#   - every user sets the exit code and the last one set stands (setSysExitRC): a domain's dry run is never 0;
#   - `all users … doit`, a domain-wide trash the app never sends, is refused as not modelled.
if { { [ "${1:-}" = "all" ] && [ "${2:-}" = "users" ]; } || [ "${1:-}" = "user" ]; } && [ "${3:-}" = "trash" ] \
   && [ "${4:-}" = "messages" ]; then
  entity=$1; target=${2:-}
  shift 4
  query=""; queries=0; quick=1; doit=""; max=1
  while [ $# -gt 0 ]; do
    word=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '_')
    case "$word" in
      query)
        [ $# -ge 2 ] || missing_arg "Query"
        [ -n "$2" ] || empty_arg "Query"
        queries=$((queries + 1)); query=$2; shift 2 ;;
      quick) quick=1; shift ;;
      notquick) quick=""; shift ;;
      doit) doit=1; shift ;;
      maxtotrash|maxtoprocess)
        [ $# -ge 2 ] || missing_arg "integer x>=0"
        value=$(printf '%s' "$2" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')
        case "$value" in ''|*[!0-9]*) usage_error "Invalid argument: Expected <integer x>=0>" ;; esac
        value=$(printf '%s' "$value" | sed 's/^0*//')
        if [ -z "$value" ]; then max=0; elif [ ${#value} -gt 9 ]; then max=999999999; else max=$value; fi
        shift 2 ;;
      matchlabel|or|and|labelids|ids|labelmatchpattern|sendermatchpattern|maxmessagesperthread|csv|todrive|querytime*)
        not_modelled "trash messages with $1" ;;
      *) invalid_arg "$1" ;;
    esac
  done
  [ "$queries" -gt 0 ] || missing_arg "query|matchlabel|ids|labelids"
  [ "$queries" -eq 1 ] || not_modelled "trash messages with more than one query"
  case "$query" in
    "rfc822msgid:"*" -in:sent") msgid=${query#rfc822msgid:}; msgid=${msgid% -in:sent} ;;
    *) not_modelled "a Gmail query other than rfc822msgid:<id> -in:sent" ;;
  esac
  case "$msgid" in ''|*[[:space:]]*) not_modelled "a Gmail query other than rfc822msgid:<id> -in:sent" ;; esac
  if [ "$entity" = all ]; then
    [ -z "$doit" ] || not_modelled "all users trash messages with doit (the app trashes one mailbox at a time)"
    users=$(grep -vF '"suspended": true' "$GAM_MOCK_FIXTURES/print_users.json" \
      | sed -n 's/.*"primaryEmail": "\([^"]*\)".*/\1/p' | LC_ALL=C sort)
    count=$(printf '%s\n' "$users" | grep -c . || true)
    printf 'Getting all Users that match query (isSuspended=False isGuest=False), may take some time on a large Google Workspace Account...\n' 1>&2
    # getPageMessage(): "Got <n> Users...", the count the reader checks every user's line against.
    got_users "$count"
  else
    known_user "$target" || token_refused "$target" Trash
    ! grep -F "\"primaryEmail\": \"$target\"" "$GAM_MOCK_FIXTURES/print_users.json" | grep -qF '"suspended": true' \
      || not_modelled "trash messages as a suspended user"
    users=$target; count=1
  fi
  rc=0; i=0
  for user in $users; do
    i=$((i + 1))
    if [ "$count" -gt 1 ]; then n=" ($i/$count)"; else n=""; fi
    # The progress first: GAM prints it before the list call that finds Gmail off.
    printf 'Getting all Messages that match query ((%s)) for %s%s\n' "$query" "$user" "$n" 1>&2
    if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/nogmail/$user" ]; then
      printf 'User: %s, Gmail Service/App not enabled%s\n' "$user" "$n" 1>&2
      rc=73; continue
    fi
    # Theirs, with this Message-ID, in neither Sent (the query), Spam nor Trash, and not trashed here since.
    ids=""; found=0
    while IFS=, read -r owner id message labels; do
      { [ "$owner" = "$user" ] && [ "$message" = "$msgid" ]; } || continue
      case " $labels " in *" SENT "*|*" SPAM "*|*" TRASH "*) continue ;; esac
      if [ -n "${GAM_MOCK_STATE:-}" ] && [ -e "$GAM_MOCK_STATE/trashed/$id" ]; then continue; fi
      if [ -n "$quick" ] && [ "$max" -gt 0 ] && [ "$found" -ge "$max" ]; then break; fi
      found=$((found + 1)); ids="$ids $id"
    done < "$GAM_MOCK_FIXTURES/messages.csv"
    if [ "$found" -eq 1 ]; then noun=Message; else noun=Messages; fi
    printf '\rGot %s %s that matched query ((%s)) for %s...\r\n' "$found" "$noun" "$query" "$user" 1>&2
    if [ "$found" -eq 0 ]; then
      printf 'User: %s, Messages: 0, Not Trashed: No Messages matched%s\n' "$user" "$n" 1>&2
      rc=60; continue
    fi
    if [ "$max" -gt 0 ] && [ "$found" -gt "$max" ]; then
      printf 'User: %s, %s: %s, Not Trashed: Count %s exceeds maximum to Trash %s%s\n' "$user" "$noun" "$found" "$found" "$max" "$n" 1>&2
      rc=51; continue
    fi
    if [ -z "$doit" ]; then
      printf 'User: %s, %s: %s, Not Trashed: Use the "doit" argument to perform action%s\n' "$user" "$noun" "$found" "$n" 1>&2
      rc=51; continue
    fi
    echo "User: $user, Trash $found $noun$n"
    j=0
    for id in $ids; do
      j=$((j + 1))
      if [ "$found" -gt 1 ]; then m=" ($j/$found)"; else m=""; fi
      if [ -n "${GAM_MOCK_STATE:-}" ]; then mkdir -p "$GAM_MOCK_STATE/trashed"; : > "$GAM_MOCK_STATE/trashed/$id"; fi
      echo "  User: $user, Message: $id, Trashed$m"
    done
  done
  exit "$rc"
fi

# Anything else is unhandled. Fail loudly: a catch-all that succeeded is how the mock used to lie — a
# `delete events` without doit, an invalid calendar role and 23 writes with no handler all "passed".
echo "ERROR: mock: unhandled argv: $*" 1>&2
exit 2
