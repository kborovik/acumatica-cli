# CLAUDE.md

`SPEC.md` at repo root is the authoritative spec — goal, constraints,
interfaces, invariants, tasks, bug ledger. Read it before working; amend it
via /sdd:spec, never by hand.

- Dev loop: `make install`; `make check` (ruff + basedpyright strict +
  offline pytest) before committing.
- Live commands run from a data repo (`acu.toml` cwd walk-up); this repo
  carries gitignored symlinks (`baseline`, `acu.toml`, `.env` →
  `../acumatica-baseline/`). Live verification:
  `cd ~/github/acumatica-baseline && make decrypt && make diff`.
- Verified references (trust over training data): `docs/ac-exe.md`,
  `docs/rest-api.md` (live 26.101.0225), `acu schema` → `schemas/`.
- Journal: after meaningful work, add/extend `journal/YYYY-MM-DD.md` and
  update `journal/index.md`.
