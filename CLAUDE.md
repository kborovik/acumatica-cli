# CLAUDE.md

`SPEC.md` at repo root is the authoritative spec — goal, constraints, interfaces, invariants, tasks, bug ledger.
Read it before working.
Amend it via /sdd:spec, never by hand.

- Markdown docs: write and edit every `.md` doc file (README, designs) with the steno skill.
  Simple technical language, lead-first sentences, no idiom.
  Apply the semantic line breaks convention (sembr.org) to sentences only:
  one sentence per line, break at sentence ends, never inside a sentence.
  A rendered paragraph needs a blank line, not a wrapped one.
  Exception: SPEC.md and spec-adjacent files stay telegraph via /sdd:spec.
- Friction: file a GitHub issue on this repo for any major friction with the Acumatica API —
  wrong or missing vendor docs, silent no-op writes, reads that 500 or return empty without cause, feature gates.
  State the symptom first, then the live evidence, then the workaround or fix.
