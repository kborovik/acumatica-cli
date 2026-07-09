# check-extras — repo-local audit recipes

Consumed by /sdd:check (cascade scan) and sweep tasks. Each recipe:
run cmd, apply exemption filter, any surviving match = violation
→ bail w/ recipe msg; no commit until match-free.

## §V.9 — ASCII-only output audit

- cmd: `grep -rnP '[^\x00-\x7F]' src/`
- exempt: docstring bodies + `#` comment lines + XML comments (non-output-reaching)
- surviving match → bail: `non-ASCII in output-reaching string per §V.9 — swap ASCII glyph or relocate to docstring/comment`

## §V.9 — output-discipline recipe (extracted from SPEC.md §V.9)

- no bare `print()` — ruff T20 enforces
- stdout = data (one record per line, greppable); stderr = process (steps, warnings, errors)
- ASCII-only incl. TTY — prefixes `+` ok, `!` warn, `x` error, deterministic, survive piping
- rich auto-degrades non-TTY; no manual TTY branching outside `output.py`
- `NO_COLOR`/`FORCE_COLOR` respected; markup/emoji/highlighting off; table box ASCII; spinner ASCII
- expected failure = one `x` line, no traceback (`ACU_DEBUG=1` re-raises); validation error → `SystemExit("msg")`

## §V.16 — option-convention recipe (extracted from SPEC.md §V.16)

- globals (`-t/--tenant`, `--version`) valid only before subcommand
- file/dir inputs positional variadic; dirs expand `*.yaml`
- `--dry-run` lines `would <VERB> …`, summary suffixed `(dry run)`
- long flags kebab-case; short flags reserved for globals
