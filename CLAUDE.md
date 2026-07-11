# CLAUDE.md

`SPEC.md` at repo root is the authoritative spec — goal, constraints,
interfaces, invariants, tasks, bug ledger. Read it before working; amend it
via /sdd:spec, never by hand.

- Dev loop: `make install`; `make check` (ruff + basedpyright strict +
  offline pytest) before committing.
- Live commands run from a data repo (`.env` cwd walk-up); this repo
  carries gitignored symlinks (`baseline`, `bootstrap`, `setup`, `.env`)
  pointing into `../acumatica-baseline/`. Live verification:
  `cd ~/github/acumatica-baseline && make decrypt && make diff`.
- Verified references (trust over training data): `docs/ac-exe.md`,
  `docs/rest-api.md` (live 26.101.0225), and `acu schema` dumps into
  `schemas/`.
- Journal: after meaningful work, add/extend `journal/YYYY-MM-DD.md` and
  update `journal/index.md`.

<!-- sdd:direct-instruction:begin -->
Write chat replies and human-facing files (README, this file) in plain
English: spell out arrows and comparison symbols in prose, avoid jargon
idioms, and explain rather than compress. The telegraph register stays
in SPEC.md and spec-adjacent files only.
<!-- sdd:direct-instruction:end -->
