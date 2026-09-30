"""Properties of turning GAM's stderr into what the operator sees (core/gam/errors.py).

A failed GAM run reaches the operator as a GAMError: a kind (which drives the remediation text and
whether a bulk loop stops), a message, and a scrubbed stderr. These properties guard that:

- any stderr and any exit code (None = timeout) yields a well-formed error, never an exception;
- the most severe line wins, so appending a line can never make a stderr read as *less* severe —
  a real failure is never reported as the benign per-entity notice printed beside it;
- GAM's per-entity counter " (403/1200)" is never read as an HTTP 403/404/429, and its progress
  chatter ("Getting all Users…", "Got 150 Users…") and blank lines are never read as an error;
- the message quotes the last line of the reported kind, never the benign tail beside it, and
  never GAM's stdout (directory data: the error is logged);
- every kind is reachable, and the documented pattern overlaps resolve the documented way;
- a missing-scope remediation names exactly the scope URLs GAM named, whole, and only for that kind;
- a password GAM echoes back on a usage error never survives into .stderr, .message, str() or repr
  (the GAMError is logged and shown; invariant 4's secrets must not ride out on it);
- onboarding.not_ready_yet parks a step exactly for Google's "not ready yet" words.
"""

from __future__ import annotations

from types import SimpleNamespace

from hypothesis import assume, given, strategies as st

from gamgui.core.audit import _SENSITIVE_KEYS
from gamgui.core.gam.errors import (
    _REMEDIATION,
    _SEVERITY,
    GAMError,
    GAMErrorKind,
    _classify_line,
    _error_lines,
    _scrub_stderr,
    classify_stderr,
)
from gamgui.core.onboarding import not_ready_yet

K = GAMErrorKind

# Lines GAM prints, with the kind each must classify as. The pairs marked "overlap" say two patterns'
# words at once; the order of errors._PATTERNS decides them, and each decision is load-bearing.
_KNOWN = (
    ("ERROR: invalid_grant: Token has been expired or revoked", K.AUTH_EXPIRED),
    ("ERROR: invalid_grant: Bad Request", K.AUTH_EXPIRED),
    # overlap: invalid_grant for one *target* address is a missing user, not an expired admin sign-in
    ("User: nobody@example.com, User:, Show Failed: invalid_grant: Invalid email or User ID", K.NOT_FOUND),
    ("User: nobody@example.com, User:, Print Failed: invalid_grant: Not a valid email", K.NOT_FOUND),
    ("User: gone@example.com, User:, Show Failed: invalid_grant: The account has been deleted", K.NOT_FOUND),
    # overlap: a 403 that names a missing scope is the scope
    ("ERROR: 403: Request had insufficient authentication scopes", K.SCOPE_MISSING),
    ("ERROR: 429: userRateLimitExceeded - rate limit", K.RATE_LIMITED),
    ("ERROR: 404: Entity User does not exist - notFound", K.NOT_FOUND),
    ("User: bob@example.com, Service not applicable/Does not exist", K.NOT_FOUND),
    # overlap: a missing credentials file says "not found"/"Does not exist" but is setup
    ("ERROR: oauth2.txt file not found", K.NOT_AUTHENTICATED),
    ("Client OAuth2 File: /tmp/gamcfg/oauth2.txt, Does not exist", K.NOT_AUTHENTICATED),
    ("Service Account OAuth2 File: /tmp/gamcfg/oauth2service.json, Does not exist", K.NOT_AUTHENTICATED),
    ("Calendar: carol@example.com, Calendar ACL: (Scope: user:carol@example.com), "
     "Delete Failed: Cannot change your own access level.", K.OWN_ACL),
    # overlap: a refusal that also says "not found" is a refusal
    ("Calendar: alice@example.com, Calendar ACL: (Scope: user:carol@example.com), "
     "Delete Failed: 403: Forbidden - Calendar not found for this caller", K.PERMISSION_DENIED),
    ("ERROR: 403: forbidden - insufficientPermissions", K.PERMISSION_DENIED),
    ("User: dave@example.com, Calendar Service/App not enabled", K.SERVICE_NOT_ENABLED),
    # overlap: the account-wide "<API> not enabled" is a real failure, not a per-user service switch
    ('ERROR: Calendar not enabled. Please run "gam update project" and '
     '"gam user user@domain.com update serviceaccount"', K.UNKNOWN),
    # overlap: "Domain user limit" says neither "rate limit" nor "quota"
    ("User: new.hire@example.com, Create Failed: Domain user limit reached. Contact Support.", K.LICENSE_LIMIT),
    ("Calendar: alice@example.com, Calendar ACL: (Scope: user:carol@example.com), "
     "Delete Failed: Internal error encountered.", K.UNKNOWN),
)
_KNOWN_LINE = st.sampled_from([line for line, _ in _KNOWN])
_KIND_OF = dict(_KNOWN)

# One stderr line of arbitrary text (no character str.splitlines() would break on).
_LINE_BREAKS = "\n\r\x0b\x0c\x1c\x1d\x1e\x85  "
_ONE_LINE = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters=_LINE_BREAKS),
                    max_size=60)
_ANY_LINE = st.one_of(_KNOWN_LINE, _ONE_LINE)
# GAM's counter; the status codes are the numbers it used to be mistaken for.
_COUNT = st.one_of(st.sampled_from([403, 404, 429]), st.integers(0, 10**6))
# GAM's progress chatter on stderr (gam.cfg show_gettings, on by default), and blank lines.
_CHATTER = st.one_of(
    st.sampled_from(["Getting all Users, may take some time on a large Google Workspace Account...",
                     "Getting all Groups, may take some time on a large Google Workspace Account...",
                     "", "   "]),
    st.builds("Got {} {}: a@example.com - z@example.com".format, st.integers(0, 10**6),
              st.sampled_from(["Users", "Groups", "Calendars"])),
)
# Marks stdout, to prove it never reaches the message or repr.
_STDOUT_MARK = "stdout-only-7f3a"


def _severity(kind: GAMErrorKind) -> int:
    return _SEVERITY.index(kind)       # lower = more severe (errors._worst takes the min)


@st.composite
def _stderr_with_an_error_line(draw) -> str:
    """Lines around at least one known GAM error line, so the stderr reports something."""
    lines = draw(st.lists(_ANY_LINE, max_size=6))
    lines.insert(draw(st.integers(0, len(lines))), draw(_KNOWN_LINE))
    return "\n".join(lines)


_STDERR = st.one_of(st.text(), _stderr_with_an_error_line())


@given(stderr=_STDERR, exit_code=st.one_of(st.none(), st.integers(-(2**31), 2**31)),
       argv=st.one_of(st.none(), st.lists(st.text(max_size=12), max_size=8)),
       stdout=st.text(max_size=40).map(lambda t: _STDOUT_MARK + t))
def test_any_stderr_and_exit_code_yield_a_well_formed_error(stderr, exit_code, argv, stdout):
    assume(_STDOUT_MARK not in stderr and _STDOUT_MARK not in "".join(argv or []))
    assert isinstance(classify_stderr(stderr), GAMErrorKind)
    err = GAMError.from_run(exit_code, stderr, argv, stdout=stdout)
    assert isinstance(err.kind, GAMErrorKind)
    assert err.kinds and all(isinstance(k, GAMErrorKind) for k in err.kinds)
    if exit_code is None:           # the process never returned: a timeout, whatever it printed
        assert err.kind is K.TIMEOUT and err.kinds == {K.TIMEOUT}
    else:                           # the reported kind is the most severe line's, as classify says
        assert err.kind is classify_stderr(stderr)
        assert err.kind is min(err.kinds, key=_severity)
    assert err.remediation.startswith(_REMEDIATION[err.kind]) and err.remediation.strip()
    assert isinstance(err.message, str) and err.message.strip()
    assert str(err) == err.message
    assert err.message.startswith(f"GAM failed ({err.kind.value}, exit={exit_code})")
    # stdout (check serviceaccount's table, directory data) is kept for setup's verify to read, but
    # never shown or logged. (Its scrub is the password property's: it passes stderr as stdout.)
    assert err.stdout == _scrub_stderr(stdout)
    assert _STDOUT_MARK not in err.message and _STDOUT_MARK not in repr(err)


@given(base=_stderr_with_an_error_line(), appended=st.one_of(_ANY_LINE, st.text()))
def test_appending_a_line_never_lowers_severity(base, appended):
    # errors._worst: the min over _SEVERITY of every line's kind. A stderr with no error line at all
    # falls back to UNKNOWN ("nothing to go on"), so the law is stated from a base that has one.
    before, after = classify_stderr(base), classify_stderr(base + "\n" + appended)
    assert _severity(after) <= _severity(before)
    # ...and exactly: the combined stderr reads as the worse of its two halves.
    if _error_lines(appended):
        assert after is min(before, classify_stderr(appended), key=_severity)
    else:
        assert after is before


@given(line=_ONE_LINE, n=_COUNT, m=_COUNT)
def test_the_entity_counter_never_changes_a_lines_kind(line, n, m):
    line = line.strip()             # _error_lines strips each line before classifying it
    assert _classify_line(f"{line} ({n}/{m})") is _classify_line(line)


@given(lines=st.lists(st.tuples(_KNOWN_LINE, st.one_of(st.none(), st.tuples(_COUNT, _COUNT))),
                      min_size=1, max_size=6))
def test_a_counted_stderr_reads_as_the_uncounted_one(lines):
    plain = "\n".join(line for line, _ in lines)
    counted = "\n".join(line + (f" ({c[0]}/{c[1]})" if c else "") for line, c in lines)
    assert classify_stderr(counted) is classify_stderr(plain)
    assert GAMError.from_run(1, counted).kinds == GAMError.from_run(1, plain).kinds


@given(lines=st.lists(_KNOWN_LINE, max_size=5), exit_code=st.integers(1, 255),
       chatter=st.lists(st.tuples(st.integers(0, 5), _CHATTER), min_size=1, max_size=4))
def test_progress_chatter_changes_nothing(lines, exit_code, chatter):
    # Read as an UNKNOWN line, "Got 150 Users" would outrank every per-entity NOT_FOUND beside it, and
    # be the line the message quotes; chatter alone would read as a failure with something to show.
    noisy = list(lines)
    for at, line in chatter:
        noisy.insert(min(at, len(noisy)), line)
    plain = GAMError.from_run(exit_code, "\n".join(lines))
    loud = GAMError.from_run(exit_code, "\n".join(noisy))
    assert (loud.kind, loud.kinds, loud.message) == (plain.kind, plain.kinds, plain.message)


@given(lines=st.lists(_KNOWN_LINE, min_size=1, max_size=6), exit_code=st.integers(1, 255))
def test_the_message_quotes_the_last_line_of_the_reported_kind(lines, exit_code):
    # Against the table, not the classifier: the reported kind is the most severe documented kind, and
    # the message quotes the LAST line of it — the tail of a mixed stderr is often a benign notice.
    err = GAMError.from_run(exit_code, "\n".join(lines))
    documented = {_KIND_OF[line] for line in lines}
    assert err.kinds == documented and err.kind is min(documented, key=_severity)
    quoted = [line for line in lines if _KIND_OF[line] is err.kind][-1]
    assert err.message == f"GAM failed ({err.kind.value}, exit={exit_code}): {quoted}"


@given(row=st.sampled_from(_KNOWN), indent=st.sampled_from(["", "    ", "\t"]),
       case=st.sampled_from([str, str.upper, str.lower, str.swapcase]),
       count=st.one_of(st.none(), st.tuples(_COUNT, _COUNT)))
def test_known_lines_classify_as_documented(row, indent, case, count):
    line, kind = row
    shown = indent + case(line) + (f" ({count[0]}/{count[1]})" if count else "")
    assert classify_stderr(shown) is kind          # every pattern is case-insensitive
    err = GAMError.from_run(1, shown)
    assert err.kind is kind and err.remediation.startswith(_REMEDIATION[kind])


@given(kind=st.sampled_from(list(GAMErrorKind)))
def test_every_kind_is_reachable(kind):
    assert _REMEDIATION[kind].strip()
    if kind is K.TIMEOUT:           # only a run that never returned
        assert GAMError.from_run(None, "").kind is K.TIMEOUT
        return
    lines = [line for line, k in _KNOWN if k is kind]
    assert lines, f"no known GAM line reaches {kind}"
    assert all(classify_stderr(line) is kind for line in lines)


# --- the scopes GAM names in a missing-scope error ---
_SCOPE_SEG = st.from_regex(r"[a-z](?:[a-z0-9_-]{0,10}[a-z0-9])?", fullmatch=True)
_SCOPE = st.one_of(
    st.just("https://mail.google.com/"),
    st.builds(lambda first, rest: "https://www.googleapis.com/auth/" + first + "".join(s + g for s, g in rest),
              _SCOPE_SEG, st.lists(st.tuples(st.sampled_from("./"), _SCOPE_SEG), max_size=3)),
)


@given(named=st.lists(st.tuples(st.sampled_from([" ", ", ", ": "]), _SCOPE,
                                st.sampled_from(["", ".", ",", ";", ")", "]"])), max_size=4),
       worse=st.booleans())
def test_the_scope_remediation_names_exactly_the_scopes_gam_named(named, worse):
    # All on the scope line, so it classifies as SCOPE_MISSING whatever the URLs spell. Each URL is
    # followed by the punctuation GAM's sentence puts after it, which must not become part of it.
    stderr = "ERROR: 403: Request had insufficient authentication scopes" + \
        "".join(sep + url + punct for sep, url, punct in named)
    if worse:                       # a more severe line: its remediation, with no scope list tacked on
        stderr += "\nERROR: invalid_grant: Token has been expired or revoked"
    err = GAMError.from_run(1, stderr)
    assert err.kind is (K.AUTH_EXPIRED if worse else K.SCOPE_MISSING)
    urls = sorted({url for _, url, _ in named})
    if worse or not urls:
        assert err.remediation == _REMEDIATION[err.kind]
    else:
        assert err.remediation == _REMEDIATION[err.kind] + " GAM named: " + ", ".join(urls) + "."


# --- a password GAM echoes back on a usage error ---
# Cmd.QuotedArgumentList and Cmd.CommandLineWithBadArgumentMarked, as disassembled from the vendored
# 7.48.14 build: an item is quoted when empty or holding a space, comma or apostrophe; the bad argument
# is marked >>>like this<<<, or everything from it on when extraneous, or ">>><<<" when one is missing.
def _quoted(items):
    return " ".join(i if i and " " not in i and "," not in i and "'" not in i else '"' + i + '"'
                    for i in items)


def _gam_usage_error(argv, at, form):
    if form == "missing":
        echo = f"Command: {_quoted(argv)} >>><<<"
    elif form == "extraneous":
        echo = f"Command: {_quoted(argv[:at])} >>>{_quoted(argv[at:])}<<<"
    else:
        echo = f"Command: {_quoted(argv[:at])} >>>{_quoted([argv[at]])}<<< {_quoted(argv[at + 1:])}"
    return (echo + "\nERROR: Invalid argument: expected <String>\n"
            "Help: Syntax in file /Applications/GamGUI.app/gam7/GamCommands.txt\n")


_FILLER = st.one_of(
    st.sampled_from(["create", "user", "update", "new.hire@example.com", "firstname", "Ada", "lastname",
                     "Lovelace", "O'Brien", "Smith, Jr", "changepassword", "on", "org", "/Staff/New Hires",
                     "notify", "boss@example.com", ""]),
    # Not a token that ends in the keyword, and not another sensitive key: either one shifts a
    # positional mask onto the keyword itself (a hire surnamed "Password"). That case is owned by the
    # connector's redact_secrets(), which masks the known secret wherever it lands.
    st.text(max_size=10).filter(lambda t: "password" not in t.casefold() and t.lower() not in _SENSITIVE_KEYS),
)
_KEYWORD = st.sampled_from(["password", "notifypassword"]).flatmap(
    lambda kw: st.lists(st.booleans(), min_size=len(kw), max_size=len(kw)).map(
        lambda up: "".join(c.upper() if u else c for c, u in zip(kw, up, strict=True))))
# GAM echoes a whitespace-free value as one token (quoted if it has a comma or apostrophe) — every
# password GamGUI sends (onboarding.generate_temp_password) is one. Out of scope, and not reachable today:
# a value with a space (echoed `password "a b"`, the scrub masks only `"a`) and a rejected keyword
# (`>>>password<<< value`, no whitespace after the keyword) both pass _SECRET_IN_STDERR.
_SECRET_CHAR = st.characters(blacklist_categories=("Cs",)).filter(lambda c: not c.isspace())


@st.composite
def _usage_error(draw, n):
    """A GAM usage error on a command carrying ``n`` password values, rendered twice: with one set of
    random values and with another. Returns (argv, stderr, argv2, stderr2, the first set of values)."""
    values = st.text(_SECRET_CHAR, min_size=6, max_size=24)
    argv, keyword_at = ["gam"], []
    for _ in range(n):
        argv += draw(st.lists(_FILLER, max_size=4))
        keyword_at.append(len(argv))
        argv += [draw(_KEYWORD), draw(values)]
    argv += draw(st.lists(_FILLER, max_size=4))
    argv2 = list(argv)
    for k in keyword_at:
        argv2[k + 1] = draw(st.text(_SECRET_CHAR, min_size=1, max_size=24))
    form = draw(st.sampled_from(["bad", "extraneous", "missing"]))
    at = draw(st.integers(1, len(argv) - 1))
    if form == "bad" and at in keyword_at:
        at += 1                     # GAM marks a keyword only when it rejects it; ours take a password
    return (argv, _gam_usage_error(argv, at, form), argv2, _gam_usage_error(argv2, at, form),
            [argv[k + 1] for k in keyword_at])


@given(case=_usage_error(1) | _usage_error(2), exit_code=st.sampled_from([2, 1, 50]))
def test_an_echoed_password_never_reaches_the_operator(case, exit_code):
    argv, stderr, argv2, stderr2, secrets = case
    # The same command with different password values: once scrubbed, the two must be identical —
    # nothing of the value, not even a fragment, survives.
    assert _scrub_stderr(stderr) == _scrub_stderr(stderr2)
    err = GAMError.from_run(exit_code, stderr, argv, stdout=stderr)
    err2 = GAMError.from_run(exit_code, stderr2, argv2, stdout=stderr2)
    assert (err.stderr, err.stdout, err.argv) == (err2.stderr, err2.stdout, err2.argv)
    # And no value is in anything shown or logged (unless the surrounding text happens to say it).
    blank = GAMError.from_run(exit_code, _scrub_stderr(stderr), [])
    assume(not any(s in blank.message or s in repr(blank) for s in secrets))
    for secret in secrets:
        for shown in (_scrub_stderr(stderr), err.stderr, err.message, str(err), repr(err)):
            assert secret not in shown


# --- onboarding: is this failure Google not having finished the new account yet? ---
_NOT_READY = "Requested client not authorized"
_NEAR_MISSES = ["Requested client is not authorized", "Client not authorized",
                "Requested client not  authorized", "Not Authorized to access this resource/api",
                "unauthorized_client", "Requested client authorized"]


@st.composite
def _any_case(draw, text):
    up = draw(st.lists(st.booleans(), min_size=len(text), max_size=len(text)))
    return "".join(c.upper() if u else c.lower() for c, u in zip(text, up, strict=True))


# No "q" in the text around it: every way to say the phrase starts "Requested", so the random halves
# can never spell it out around a near miss, and the expected answer needs no second matcher.
_NO_Q = st.text(st.characters(blacklist_categories=("Cs",), blacklist_characters="qQ"), max_size=30)


@given(kind=st.sampled_from([*GAMErrorKind, None, "service_not_enabled"]), says=st.booleans(),
       before=_NO_Q, after=_NO_Q, data=st.data())
def test_not_ready_yet_is_service_off_or_client_not_authorized(kind, says, before, after, data):
    phrase = data.draw(_any_case(_NOT_READY)) if says else data.draw(st.sampled_from(_NEAR_MISSES))
    detail = before + phrase + after
    expected = kind is K.SERVICE_NOT_ENABLED or says
    assert not_ready_yet(SimpleNamespace(kind=kind, detail=detail)) is expected
    # A failed ChangeResult with no detail: only the kind can say it.
    assert not_ready_yet(SimpleNamespace(kind=kind, detail="")) is (kind is K.SERVICE_NOT_ENABLED)


@given(row=st.sampled_from(_KNOWN), says=st.booleans(), data=st.data())
def test_not_ready_yet_reads_a_raised_gam_error(row, says, data):
    # A raised GAMError has no .detail: its str() (the reported line) is what's read.
    line, kind = row
    if says:                        # the first is the shape seen live (2026-09-30)
        line = data.draw(st.sampled_from(["User: new.hire@example.com, User Set Failed: access_denied: ",
                                          "User: new.hire@example.com, Signature Update Failed: "
                                          "unauthorized_client: "])) + data.draw(_any_case(_NOT_READY))
    err = GAMError.from_run(data.draw(st.sampled_from([1, 50, 73])), line)
    assert not_ready_yet(err) is (says or err.kind is K.SERVICE_NOT_ENABLED)
    assert not_ready_yet(GAMError.from_run(None, line)) is says      # a timeout is never "service off"
