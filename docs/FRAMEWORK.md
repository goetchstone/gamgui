# How this repo teaches itself

GamGUI is a solo-operator project: one person, no PR review, no second pair of
eyes — and the credentials it touches (`oauth2service.json` impersonates any
user; `oauth2.txt` is an admin password) make a silent mistake expensive. The
model has to be the second pair of eyes, and the structure has to make that role
enforceable. This document is how.

One sentence if you read nothing else: **rules earn their place by surviving
incidents; each rule lives in the layer that can actually enforce it; and the
mock is guilty until proven faithful.**

---

## 1. Three layers of written context

Everything written for the model to read is one of three things. The split is
what keeps `CLAUDE.md` short enough that the model actually holds it — a 2,000-
line constitution gets silently ignored from the bottom up.

| Layer | Where | Purpose | Loaded |
|---|---|---|---|
| **Constitution** | [`CLAUDE.md`](../CLAUDE.md) | Numbered invariants (#1–#9), each with the incident that birthed it. | Every session |
| **Domain runbooks** | [`docs/domains/*.md`](domains) | Per-area knowledge: files, flow, the failure history, the mock-lies traps, how to do common tasks. | On demand, by area |
| **Skills** | [`.claude/skills/*/SKILL.md`](../.claude/skills) | Procedures for a moment: pre-commit, post-failure, start/end-session, improve-rules. | When the moment arrives |

When CLAUDE.md wants to grow, push the detail into a runbook and keep the
invariant one line. The runbook is the law library; the constitution is short.

## 2. Three enforcement homes

Every invariant needs a home that matches how hard it must bite. GamGUI already
had the third layer before it had the other two — the drift guards and the
"mock lies" tests are tripwires.

| Home | What it is | Strength | Example in this repo |
|---|---|---|---|
| **Skill** | A checklist the model reads when activated | Persuasive | [`pre-commit`](../.claude/skills/pre-commit/SKILL.md): "route the mutation through `_run_write`" |
| **Hook** | Code on a tool event; can hard-block | Hard gate | [`.claude/hooks/pre-commit-check.sh`](../.claude/hooks): blocks a `fix:` commit that skipped the failure log |
| **Tripwire test** | A test that fails CI if a guard is missing or the mock lies | Backstop | `test_required_command_tokens_present`, `test_catalog_matches_grammar`, `test_pinned_version_consistent`; the mock-must-fail-like-GAM tests |

The question every incident asks: **where does this rule live so it can't be
forgotten?** Don't file a hard gate as a skill — the model reads it once and
forgets. The invariant table in CLAUDE.md should, over time, name each
invariant's enforcement home.

**A hook only counts if its output reaches the model.** Claude Code routes hook
output by channel: a SessionStart hook's **stdout** becomes context; a
non-blocking PreToolUse hook must print JSON
`{"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "…"}}`
on stdout; stderr is fed back only on a blocking **exit 2**. Anything else on
stderr at exit 0 reaches nobody — these hooks ran that way for weeks unnoticed
(see [failure-log.md](failure-log.md), 2026-09-23). Test a hook by finding its
text in the model's context, not by running it in a terminal. Keep what hooks
inject short and specific: a few lines relevant to *this* diff are read; a
40-line checklist on every commit is noise.

**Local vs shared.** Per `.gitignore`, `.claude/*` is local **except**
`skills/` and `agents/`. So the *knowledge* — skills, domain runbooks, this
file, the ledgers — is committed and benefits any contributor; the *enforcement
wiring* — `.claude/hooks/*` and `.claude/settings.json` — stays local to this
operator. (To share the gates too, un-ignore those two.)

## 3. The learning loop

```
Something breaks (a test late, a live tenant, a regression)
        ↓  post-failure skill
docs/failure-log.md entry: symptom / cause / why-not-caught / fix / prevention
        ↓
Recurring shape? ── yes ─→ a tripwire test that asserts it can't recur silently
        │                   (and/or a note in docs/RULE-FEEDBACK.md)
        no
        ↓
docs/RULE-FEEDBACK.md — one low-friction entry: which invariant strained, and how
        ↓  (evidence accumulates; sessions do NOT edit CLAUDE.md)
improve-rules skill — later, with distance — reads the pile and proposes
ONE focused CLAUDE.md edit as a PR. A human merges it. Next session inherits it.
```

Two moving parts keep it honest:

- **Sessions append, the observer decides.** The agent that just got burned is
  the worst judge of whether its bruise deserves a constitutional invariant —
  that instinct is how a constitution accretes gaps and contradictions. Sessions
  write to the ledger (cheap, commits to nothing); `improve-rules` judges the
  pile later, with distance, as a PR against `main`.
- **The fix commit is the review signal.** With no human PR review to mine, the
  substitute is the `fix:`/`revert:` commit — literally "what the agent proposed
  vs what reality required." `.claude/hooks/rules-improver-check.sh` counts them
  and nudges once enough pile up. Retirement runs the same bar in reverse: an
  invariant whose code path is gone, or whose guard never fires, is a candidate
  for demotion to its runbook (number preserved so citations resolve).

## 4. The defining failure class: the mock lies

The bug this repo keeps hitting is **"the mock passed, the live tenant broke."**
It has happened more than once: a command that rejects `formatjson` while the
mock accepted it (see [failure-log.md](failure-log.md)); a mock that returned
group members for *any* address, so a user looked like a group. So the standing
discipline: when you touch `tests/fixtures/mock_gam.sh`, make it **fail the way
real GAM fails**, checking syntax against the vendored grammar
`gamgui/resources/gam7/GamCommands.txt` — a mock more permissive than GAM is
worse than none, because it converts a live break into a green test. Corollary:
passing the offline suite does not mean a GAM write works; a real mutation is
unproven until it has run against a throwaway account.

Other invariants carry their own scars, each an entry the loop would produce
today: the plaintext credential stranded by a "quit mid-call" → the `atexit` +
owner-PID wipe (#4); resolved-path comparison rejecting an NFD home and the
firmlink spelling → the inode-based import bound (#5); `| tojson` not escaping
`"` inside an attribute → directory data via `data-*` + `dataset` (#8); a
`git pull` that moves the version pin but not the gitignored GAM binary → the
`make gam` re-vendor step (see
[build-packaging](domains/build-packaging.md)).

## 5. Adopting more of it

The framework is incremental — it grows with the incident history, it does not
need to be built all at once. Its provenance is the fuller writeup in the sibling
`holt` project's `docs/FRAMEWORK.md`; this is the GamGUI-calibrated cut. If you
extend it: add an invariant only when an incident proves it (cite the incident),
give it an enforcement home, and prefer sharpening an existing invariant over
adding a number.

---

## File tree

```
CLAUDE.md                          # Constitution — numbered invariants #1–#9
ROADMAP.md                         # Ranked backlog + deliberate trade-offs
docs/
├── FRAMEWORK.md                   # This file
├── failure-log.md                 # Five-field incident entries (shared)
├── RULE-FEEDBACK.md               # Low-friction ledger the observer reads (shared)
└── domains/                       # Load-on-demand runbooks, one per area
    ├── README.md                  #   the index + "by task" map
    └── *.md
.claude/
├── settings.json                  # Hook wiring (LOCAL — gitignored)
├── .rules-last-run                # improve-rules window stamp (LOCAL)
├── hooks/                         # (LOCAL — gitignored)
│   ├── pre-commit-check.sh        #   injects the diff-relevant checklist; blocks fix: w/o failure log
│   ├── session-start-check.sh     #   orient digest: git, GAM-pin drift, runbook pointer
│   └── rules-improver-check.sh    #   nudge to run improve-rules (unresolved evidence only)
├── skills/                        # (SHARED — committed)
│   ├── pre-commit/ · post-failure/
│   ├── start-session/ · end-of-session/
│   ├── improve-rules/
│   └── add-builder-command/       #   pre-existing
└── agents/                        # gam-command-author, gam-command-reviewer (SHARED)
```

The structure is a result, not a starting point. Build it as the incidents teach
you.
