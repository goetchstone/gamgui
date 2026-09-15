---
name: end-of-session
description: The wrap-up routine at the end of a GamGUI session — capture what was learned in the right place so the next session inherits it. Deliberately does NOT edit CLAUDE.md; evidence goes to the ledger and improve-rules decides later.
---

# End-of-session — capture

The work is done; now make sure the next session (and the one after a context
compaction) inherits what this one learned. Update docs, not the constitution.

## The checklist

1. **Any incident this session?** A regression, a broken test, a live break, a
   "the mock lied" surprise → a five-field entry in
   [docs/failure-log.md](../../../docs/failure-log.md) via
   [post-failure](../post-failure/SKILL.md), if you didn't already.
2. **Did an invariant strain?** An invariant almost let something through, sat
   in the wrong enforcement layer, read too loosely, or a failure hit a shape no
   invariant covers → one entry in
   [RULE-FEEDBACK.md](../../../docs/RULE-FEEDBACK.md). Low friction on purpose.
   **Do not edit CLAUDE.md here** — [improve-rules](../improve-rules/SKILL.md)
   reads the pile later, with distance, and proposes the change as a PR. A
   session editing the constitution while the incident is warm is exactly how it
   bloats.
3. **Did a domain's behavior turn out different from its runbook?** Update the
   relevant [docs/domains](../../../docs/domains) runbook so the correction
   isn't relearned. New area with no runbook → add one in the house template.
4. **Deferred work?** It has exactly two valid homes: a spawned task chip, or an
   entry in [ROADMAP.md](../../../ROADMAP.md). "We'll get to it" in chat is not
   tracking.
5. **A durable, non-obvious fact** (a preference, a project constraint, an
   external pointer) that isn't already in the repo or git history → a memory.
   Don't memorize what the code already records.

## Committing the docs

Commit documentation updates as their **own atomic commit**, separate from the
code, so the diff stays reviewable. The pre-commit hook and
[pre-commit](../pre-commit/SKILL.md) checklist still apply.

## Close the chapter

Mark the session's phases as chapters (orient → build → verify → wrap) so the
transcript stays navigable and the model knows prior context is closed.
