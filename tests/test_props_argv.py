"""Properties of argv construction (invariant 1) and of the audit log's secret redaction.

Invariant 1: every GAM invocation is an explicit argv list and an operator-supplied value is always
exactly one list element, never interpolated into a string. Checked structurally: each builder runs
twice, once with fuzzed values and once with a unique sentinel per parameter; the two argvs must be
identical apart from the sentinel slots. So a value lands verbatim in its own element, and no other
element depends on it — nothing glued, stripped, split, re-cased or dropped.

Redaction: ``redact_secrets`` masks a known secret by value (the layer a hire surnamed "Password"
can't shift) and ``redact_argv`` masks positionally after ``_SENSITIVE_KEYS`` (the second layer, and
the only one a GAMError's argv gets). A secret reaching the audit log or the UI is what they guard.
``lifecycle.command_line`` shows an argv to the operator with the same positional mask: read back by
a POSIX shell it must give each element back whole, with nothing a shell would expand.

Builders and redactors are looked up per call (``commands.GAMCommands``, ``audit.*``) so a scratch
test can swap in a broken stand-in and watch these fail.
"""

from __future__ import annotations

import inspect
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from hypothesis import given
from hypothesis import strategies as st

from gamgui.core import audit, lifecycle
from gamgui.core.gam import commands

G = commands.GAMCommands
MASK = audit._MASK
KEYS = audit._SENSITIVE_KEYS

# argv can't carry a NUL (execve would truncate it), so no builder is ever handed one.
_CHARS = st.characters(exclude_characters="\x00")
# Shapes that break a shell, a tokenizer, a strip() or a positional mask — alongside arbitrary text.
_NASTY = ("", " ", "\t", "a b", " padded ", "--", "-h", "$(id)", "`id`", "; rm -rf / #", "a'b", 'a"b',
          "a\\b", "\n", "x\ny", "user", "password", "Password", "signature", "html", "primary", "é",
          "e\u0301", "\u202e", "\ufeff", "*", MASK)
TEXT = st.one_of(st.text(_CHARS, max_size=30), st.sampled_from(_NASTY))
WORD = TEXT.filter(bool)  # a value behind an `if x:` clause — present, so the slot is emitted
BOOL = st.booleans()


def _spell(word: str, upper: int) -> str:
    return "".join(c.upper() if upper >> i & 1 else c for i, c in enumerate(word))


# A sensitive key in any letter case — GAM reads its keywords case-insensitively, and so must the mask.
KEYWORD = st.builds(_spell, st.sampled_from(sorted(KEYS)), st.integers(0, 2**14 - 1))


# --- invariant 1: one value, one element ----------------------------------------------------------

class Spec(NamedTuple):
    name: str                                   # GAMCommands attribute, looked up at call time
    text: Tuple[str, ...]                       # str params, always emitted
    optional: Tuple[str, ...] = ()              # str params emitted only when non-empty (`if x:`)
    other: Optional[Dict[str, Any]] = None      # validated/flag params: strategies of valid values
    repeats: Optional[Dict[str, int]] = None    # a value emitted in more than one slot, by design


ROLE = st.sampled_from(commands.GROUP_ROLES)
CAL_ROLE = st.sampled_from(commands.CALENDAR_ACL_ROLES)

# Builders taking operator free text: names, titles, bodies, subjects, signatures, summaries, queries.
FREE_TEXT = (
    # notifypassword carries the temp password a second time — the one deliberate repeat.
    Spec("create_user", ("email", "first_name", "last_name", "password"), ("org_unit", "notify"),
         {"change_password": BOOL}, {"password": 2}),
    Spec("create_user", ("email", "first_name", "last_name", "password"), ("org_unit",),
         {"change_password": BOOL, "notify": st.none()}),
    Spec("update_organization", ("email", "title", "department")),
    Spec("add_calendar_event", ("calendar", "summary", "start", "end"), ("description", "attendee")),
    Spec("create_tasklist", ("assignee", "title")),
    Spec("create_task", ("assignee", "tasklist_id", "title"), ("notes",)),
    Spec("send_email", ("to", "subject", "body"), (), {"html": BOOL}),
    Spec("set_signature", ("email", "signature"), (), {"html": BOOL}),
    Spec("set_vacation", ("email", "subject", "message"), ("start", "end"),
         {"html": BOOL, "contacts_only": BOOL, "domain_only": BOOL}),
    Spec("create_group", ("email",), ("name", "description")),
    Spec("todrive_args", (), ("user", "title")),
    Spec("print_events", ("calendar_id",), ("query", "after", "before")),
    Spec("search_messages", ("email",), ("query",), {"detail": st.sampled_from(G.MESSAGE_DETAIL)}),
    Spec("print_users", (), ("query",)),
    Spec("print_cros", (), ("query",)),
    Spec("print_filelist", ("email",), ("query",)),
    Spec("print_resources", (), ("query",)),
)

# Every other builder with a str parameter: addresses, calendar/event ids, scopes, aliases, dates.
IDENTIFIERS = (
    Spec("check_svcacct", ("admin",), (), {"scopes": st.just(("https://mail.google.com/",))}),
    Spec("report_users", ("date",), (), {"params": st.just(("accounts:used_quota_in_mb",))}),
    Spec("info_user", ("email",)),
    Spec("set_suspended", ("email",), (), {"suspended": BOOL}),
    Spec("print_calendar_acls", ("email", "calendar")),
    Spec("add_calendar_acl", ("email", "target", "calendar"), (), {"role": CAL_ROLE}),
    Spec("delete_calendar_acl", ("email", "scope", "calendar")),
    Spec("print_user_calendars", ("email",)),
    Spec("print_calendar_acls_cal", ("calendar_id",)),
    Spec("add_calendar_acl_cal", ("calendar_id", "scope"), (), {"role": CAL_ROLE, "send_notifications": BOOL}),
    Spec("delete_calendar_acl_cal", ("calendar_id", "scope")),
    Spec("subscribe_calendar", ("email", "calendar_id"), (), {"selected": BOOL}),
    Spec("remove_calendar", ("owner", "calendar_id")),
    Spec("get_event", ("calendar_id", "event_id")),
    Spec("delete_event", ("calendar_id", "event_id"), (), {"doit": BOOL}),
    Spec("reset_password", ("email",)),
    Spec("signout_user", ("email",)),
    Spec("deprovision_user", ("email",)),
    Spec("create_datatransfer", ("old_owner", "service", "new_owner"), (),
         {"privacy": st.sampled_from(("",) + G.TRANSFER_PRIVACY)}),
    Spec("print_datatransfers", (), ("old_owner",)),
    Spec("remove_all_calendar_acls", ("email",)),
    Spec("delete_user", ("email",)),
    Spec("undelete_user", ("email",)),
    Spec("show_signature", ("email",)),
    Spec("add_delegate", ("email", "delegate")),
    Spec("remove_delegate", ("email", "delegate")),
    Spec("print_delegates", ("email",)),
    Spec("vacation_off", ("email",)),
    Spec("add_forwarding_address", ("email", "address")),
    Spec("print_forwarding_addresses", ("email",)),
    Spec("set_forward", ("email", "address"), (), {"action": st.sampled_from(G.FORWARD_ACTIONS)}),
    Spec("forward_off", ("email",)),
    Spec("create_user_alias", ("alias", "email")),
    Spec("delete_alias", ("alias",)),
    Spec("show_vacation", ("email",)),
    Spec("print_group_members", ("group",)),
    Spec("print_groups_member", ("email",)),
    Spec("add_group_member", ("group", "member"), (), {"role": ROLE}),
    Spec("remove_group_member", ("group", "member")),
)


@st.composite
def _calls(draw, specs):
    """One call per spec, every str param fed from a small shared pool so each example drives every
    builder with the same adversarial strings."""
    texts = draw(st.lists(TEXT, min_size=1, max_size=6))
    words = draw(st.lists(WORD, min_size=1, max_size=6))
    calls, k, j = [], 0, 0
    for spec in specs:
        values = {}
        for p in spec.text:
            values[p] = texts[k % len(texts)]
            k += 1
        for p in spec.optional:
            values[p] = words[j % len(words)]
            j += 1
        other = draw(st.fixed_dictionaries(spec.other)) if spec.other else {}
        calls.append((spec, values, other))
    return calls


def _sentinel(param: str) -> str:
    return f"{param}"


def _assert_one_element_each(spec: Spec, values: Dict[str, str], other: Dict[str, Any]) -> None:
    build = getattr(G, spec.name)
    real = build(**values, **other)
    shape = build(**{p: _sentinel(p) for p in values}, **other)
    where = f"GAMCommands.{spec.name}"
    assert isinstance(real, list) and all(type(el) is str for el in real), where
    for p in values:
        s = _sentinel(p)
        assert shape.count(s) == (spec.repeats or {}).get(p, 1), f"{where}: {p} is not exactly one element"
        assert not any(s in el and el != s for el in shape), f"{where}: {p} is glued into another element"
    back = {_sentinel(p): v for p, v in values.items()}
    assert real == [back.get(el, el) for el in shape], f"{where}: a value was altered, or moved an element"


@given(calls=_calls(FREE_TEXT))
def test_free_text_is_one_verbatim_argv_element(calls):
    for spec, values, other in calls:
        _assert_one_element_each(spec, values, other)


@given(calls=_calls(IDENTIFIERS))
def test_every_identifier_is_one_verbatim_argv_element(calls):
    for spec, values, other in calls:
        _assert_one_element_each(spec, values, other)


# Parameter types that can't carry operator text into its own slot; anything else (str, or a new or
# unannotated parameter) must be in a table.
_NOT_TEXT = ("bool", "Sequence[str]", "Optional[Sequence[str]]")


def test_every_builder_str_parameter_is_in_a_table():
    """Not a property: the tripwire that a new builder, or a new str parameter, joins the tables."""
    covered: Dict[str, set] = {}
    for spec in FREE_TEXT + IDENTIFIERS:
        assert isinstance(vars(G).get(spec.name), staticmethod), spec.name
        covered.setdefault(spec.name, set()).update(spec.text, spec.optional, spec.other or {})
    missing = []
    for name, attr in vars(G).items():
        if isinstance(attr, staticmethod):
            params = inspect.signature(attr.__func__).parameters.values()
            wanted = {p.name for p in params if p.annotation not in _NOT_TEXT}
            missing += [f"{name}({p})" for p in sorted(wanted - covered.get(name, set()))]
    assert not missing, f"builders taking operator text that no property covers: {missing}"


# Allowed words in any case, padded with whitespace, or arbitrary text: a validated slot must only
# ever emit an allowed value, and turn anything else into ValueError (never pass it through).
_ALLOWED = commands.GROUP_ROLES + commands.CALENDAR_ACL_ROLES + G.FORWARD_ACTIONS + G.TRANSFER_PRIVACY
_PAD = st.text(" \t\n", max_size=2)
SPELLED = st.builds(lambda left, w, right: left + w + right, _PAD, st.builds(_spell, st.sampled_from(_ALLOWED),
                                                               st.integers(0, 2**26 - 1)), _PAD)


def _assert_slot(build, index: int, expected: str, allowed: Tuple[str, ...]) -> None:
    try:
        argv = build()
    except ValueError:
        assert expected not in allowed, f"rejected a valid value {expected!r}"
        return
    assert expected in allowed, f"passed through an invalid value as {argv[index]!r}"
    assert argv[index] == expected


@given(word=st.one_of(SPELLED, TEXT))
def test_validated_slots_only_emit_an_allowed_value(word):
    # Roles are documented case/space-insensitive (a blank group role is "member"); the forward action
    # and transfer privacy are matched exactly.
    group_role = (word or "member").strip().lower()
    _assert_slot(lambda: G.add_group_member("g@example.com", "m@example.com", word), 4, group_role,
                 commands.GROUP_ROLES)
    cal_role = word.strip().lower()
    _assert_slot(lambda: G.add_calendar_acl("u@example.com", "t@example.com", role=word), 5, cal_role,
                 commands.CALENDAR_ACL_ROLES)
    _assert_slot(lambda: G.add_calendar_acl_cal("c@example.com", "t@example.com", role=word), 4, cal_role,
                 commands.CALENDAR_ACL_ROLES)
    _assert_slot(lambda: G.set_forward("u@example.com", "f@example.com", action=word), 4, word,
                 G.FORWARD_ACTIONS)
    if word:
        _assert_slot(lambda: G.create_datatransfer("a@example.com", "drive", "b@example.com", word), 5,
                     word, G.TRANSFER_PRIVACY)
    else:
        assert G.create_datatransfer("a@example.com", "drive", "b@example.com", word)[5:] == []
    # The message detail picks a fixed flag set; an unknown one falls back, it is never emitted.
    canonical = [G.search_messages("u@example.com", "q", d) for d in G.MESSAGE_DETAIL]
    assert G.search_messages("u@example.com", "q", word) in canonical


# --- secret redaction -----------------------------------------------------------------------------

# A secret sharing a character with the mask can "reappear" inside the mask itself ("red" in
# "***redacted***") — a coincidence, not a leak — so secrets avoid those characters; the text they
# are hidden in does not.
SECRET = st.text(st.characters(exclude_characters="\x00" + "".join(sorted(set(MASK)))), min_size=1, max_size=12)
_KEY = st.sampled_from(("argv", "target", "extra", "error", "actor", "action"))


@st.composite
def _redaction_case(draw):
    secrets = draw(st.lists(st.one_of(SECRET, st.just("")), max_size=4))
    needles = [s for s in secrets if s]
    # A secret's case-swapped twin is not the secret: masking it too would be matching case-blind.
    frag = st.one_of(TEXT, st.sampled_from(needles), st.sampled_from(needles).map(str.swapcase)) if needles else TEXT
    leaf = st.one_of(st.lists(frag, max_size=6).map("".join), st.integers(), st.none(), st.booleans())
    tree = st.recursive(leaf, lambda kids: st.one_of(
        st.lists(kids, max_size=4), st.tuples(kids, kids), st.dictionaries(_KEY, kids, max_size=4)),
        max_leaves=12)
    return draw(tree), secrets


def _leaf_pairs(before, after):
    """Walk both trees in step, asserting the shape survived, and yield the leaf pairs."""
    assert type(after) is type(before)
    if isinstance(before, (list, tuple)):
        assert len(after) == len(before)
        for b, a in zip(before, after, strict=True):
            yield from _leaf_pairs(b, a)
    elif isinstance(before, dict):
        assert list(after) == list(before)  # keys are the log's own field names, never redacted
        for key in before:
            yield from _leaf_pairs(before[key], after[key])
    else:
        yield before, after


@given(case=_redaction_case())
def test_redact_secrets_leaves_no_secret(case):
    value, secrets = case
    needles = [s for s in secrets if s]
    out = audit.redact_secrets(value, secrets)
    for before, after in _leaf_pairs(value, out):
        if not isinstance(before, str):
            assert after == before
            continue
        for s in needles:
            assert s not in after, f"secret {s!r} survived in {after!r}"
        if not any(s in before for s in needles):
            assert after == before  # an empty secret masks nothing; secret-free text is untouched


@given(case=_redaction_case())
def test_redact_secrets_is_idempotent(case):
    value, secrets = case
    once = audit.redact_secrets(value, secrets)
    assert audit.redact_secrets(once, secrets) == once


# Secrets from odd code points, the text around them from even ones: no secret can occur in, or
# straddle into, the text, so the exact redacted string is known. The small pools add regex
# metacharacters to the secrets and mask/shell characters to the text; prefixes of a secret are also
# secrets, so the longest one must win where two start at the same place.
_ODD = st.one_of(st.sampled_from("ace)+?[{"), st.characters(min_codepoint=2).map(lambda c: chr(ord(c) | 1)))
_EVEN = st.one_of(st.sampled_from(" \n*drt$\"\\.|^("), st.characters(min_codepoint=2).map(lambda c: chr(ord(c) & ~1)))


@st.composite
def _spliced(draw):
    base = draw(st.lists(st.text(_ODD, min_size=1, max_size=6), min_size=1, max_size=3, unique=True))
    prefixes = [s[:draw(st.integers(1, len(s)))] for s in base]
    secrets = draw(st.permutations(list(dict.fromkeys(base + prefixes)) + draw(st.sampled_from([[], [""]]))))
    head = draw(st.text(_EVEN, max_size=6))
    pairs = draw(st.lists(st.tuples(st.sampled_from(base), st.text(_EVEN, min_size=1, max_size=6)), max_size=4))
    tail = draw(st.one_of(st.just(""), st.sampled_from(base)))
    text = head + "".join(s + sep for s, sep in pairs) + tail
    expected = head + "".join(MASK + sep for _, sep in pairs) + (MASK if tail else "")
    return text, secrets, expected


@given(case=_spliced())
def test_redact_secrets_masks_each_occurrence_whole_and_nothing_else(case):
    # Every occurrence becomes one mask — the longest secret, not a prefix that leaves its tail
    # showing — and the text around it is kept exactly (the error line stays readable in the log).
    text, secrets, expected = case
    assert audit.redact_secrets(text, secrets) == expected


@given(argv=st.lists(st.one_of(KEYWORD, TEXT), max_size=12))
def test_redact_argv_masks_the_value_after_each_sensitive_key(argv):
    before = list(argv)
    out = audit.redact_argv(argv)
    assert argv == before, "the input argv was mutated"
    assert isinstance(out, list) and out is not argv and len(out) == len(argv)
    for i, (tok, got) in enumerate(zip(argv, out, strict=True)):
        if got != tok:  # only ever the value right after a sensitive key, and only ever masked
            assert got == MASK and i > 0 and argv[i - 1].lower() in KEYS, (i, argv, out)
    for i in range(len(out) - 1):
        if out[i] == argv[i] and argv[i].lower() in KEYS:  # a key left standing as a key
            assert out[i + 1] == MASK, (i, argv, out)
    assert audit.redact_argv(out) == out


@given(email=TEXT, first=st.one_of(TEXT, KEYWORD), last=st.one_of(TEXT, KEYWORD), password=SECRET,
       org=st.one_of(st.none(), WORD), notify=st.one_of(st.none(), WORD), change=BOOL)
def test_create_user_password_never_reaches_the_audit_copy(email, first, last, password, org, notify, change):
    # Mirrors GAMConnector.create_user -> _run_write -> AuditLog.record: the preview/audit copy is built
    # with "********" and masked by value; the record masks positionally, then by value again. A name
    # that is itself a sensitive key ("Password") shifts the positional mask onto the keyword — the
    # value layer is what still holds, including for the real argv GAM may echo back.
    argv = G.create_user(email, first, last, password, change, org, notify)
    shown = audit.redact_secrets(G.create_user(email, first, last, "********", change, org, notify), [password])
    logged = audit.redact_secrets(audit.redact_argv(shown), [password])
    positional = audit.redact_secrets(audit.redact_argv(argv), [password])
    for view in (shown, logged, positional):
        assert len(view) == len(argv)
        assert not any(password in el for el in view), view
    # GAM echoes the whole command line on a usage error — with `notify`, the password twice — and
    # _run_write masks that text by value before it is shown or audited.
    echoed = audit.redact_secrets("ERROR: gam " + " ".join(argv), [password])
    assert password not in echoed, echoed


# --- the command line shown to the operator -------------------------------------------------------

_BARE = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_@%+=:,./-")


def _posix_words(line: str) -> List[str]:
    """Read ``line`` as a POSIX shell would, for the quoting ``command_line`` emits: bare safe
    characters, '...', and "..." with only \\ " $ ` escaped. Anything a shell would act on instead of
    read (an expansion, a metacharacter, a line continuation) fails the assertion."""
    words: List[str] = []
    i, n = 0, len(line)
    while i < n:
        if line[i] == " ":
            i += 1
            continue
        word = []
        while i < n and line[i] != " ":
            c = line[i]
            if c == "'":
                end = line.index("'", i + 1)
                word.append(line[i + 1:end])
                i = end + 1
            elif c == '"':
                i += 1
                while line[i] != '"':
                    if line[i] == "\\" and line[i + 1] in '\\"$`':
                        word.append(line[i + 1])
                        i += 2
                    else:
                        assert line[i] not in "\\$`", f"unescaped {line[i]!r} in double quotes: {line!r}"
                        word.append(line[i])
                        i += 1
                i += 1
            else:
                assert c in _BARE, f"{c!r} left bare: {line!r}"
                word.append(c)
                i += 1
        words.append("".join(word))
    return words


# Apostrophes switch _quote to double quotes, where $ ` \ " must be escaped: give it plenty of both.
QUOTEY = st.text(st.sampled_from("'\"$`\\ \n!ab(){}|;&"), max_size=8)
GENERATOR = st.sampled_from(sorted(lifecycle._PASSWORD_KEYWORDS))


@given(argv=st.lists(st.one_of(KEYWORD, GENERATOR, QUOTEY, TEXT), max_size=10))
def test_command_line_reads_back_as_the_masked_argv(argv):
    words = _posix_words(lifecycle.command_line(argv))
    assert words[0] == "gam" and len(words) == len(argv) + 1, (argv, words)
    masked = audit.redact_argv(argv)
    for i, (raw, shown) in enumerate(zip(argv, words[1:], strict=True)):
        generator = i > 0 and argv[i - 1].lower() == "password" and raw in lifecycle._PASSWORD_KEYWORDS
        # Masked exactly where the audit log masks it — except GAM's own `password random`-style
        # keyword, which is not a secret and is shown so the operator can read the command.
        assert shown == (raw if generator else masked[i]), (i, argv, words)
