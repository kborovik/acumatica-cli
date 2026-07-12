# check-extras — repo-local audit recipes

Consumed by /sdd:check (cascade scan) and sweep tasks. Each recipe:
run cmd, apply exemption filter, any surviving match = violation
→ bail w/ recipe msg; no commit until match-free.

Build pre-commit entry: `.spec/scripts/check-all` runs every recipe
below (legs `ascii` `extras` `parity`; one leg arg = that recipe only),
exit 1 on any surviving match — per-recipe cmds + bail msgs below stay
for granular /sdd:check runs.

## §V.9 — ASCII-only output audit

- cmd: `.spec/scripts/check-ascii src/`
- exemptions mechanized in-script (§T.18): `.py` COMMENT tokens + docstrings, `.cs` `//` lines, `.xml` `<!-- -->`
- surviving match (`file:line: U+XXXX` on stdout, exit 1) → bail: `non-ASCII in output-reaching string per §V.9 — swap ASCII glyph or relocate to docstring/comment`

## §V.1 + §V.10 + §V.18 — drift-grep hook (mechanized §T.19)

- cmd: `.spec/scripts/check-extras.sh` — emits `id|verdict|evidence` rows
  per the /sdd:check extras-hook contract; exit 1 on any VIOLATE row
- V1 VIOLATE → bail: `plane-split import per §V.1 — SSH stays in tenant.py, REST stays in client.py`
- V10 VIOLATE → bail: `class subclasses BaseModel outside models.py per §V.10 — inherit models.Model`
- V18 VIOLATE → bail: `exit $LASTEXITCODE outside the _ssh choke point per §V.18 — strip call-site suffix / restore _ssh`

## §V.9 — output-discipline recipe (extracted from SPEC.md §V.9)

- no bare `print()` — ruff T20 enforces
- stdout = data (one record per line, greppable); stderr = process (steps, warnings, errors)
- ASCII-only incl. TTY — prefixes `+` ok, `!` warn, `x` error, deterministic, survive piping
- rich auto-degrades non-TTY; no manual TTY branching outside `output.py`
- `NO_COLOR`/`FORCE_COLOR` respected; markup/emoji/highlighting off; table box ASCII; spinner ASCII
- expected failure = one `x` line, no traceback (`ACU_DEBUG=1` re-raises); validation error → `SystemExit("msg")`

## §V.16 — option-convention recipe (extracted from SPEC.md §V.16)

- globals (`--tenant`, `--url`, `--ssh`, `--api-version`, `--username`, `--password`, `--version`) valid only before subcommand
- file/dir inputs positional variadic; dirs expand `*.yaml`
- `--dry-run` lines `would <VERB> …`, summary suffixed `(dry run)`
- long flags kebab-case; no short flags (`-t` retired, last short form; `-o` retired @ T9)

## §V.21 — endpoint-version parity (T33 sweep mechanized)

- cmd: `.spec/scripts/check-all parity` — sweep logic (version sed, `tr` escape-fold, `grep -vF` literal filter) lives in the script, single spelling (§T.51)
- scope: `src/` + `tests/` + data symlinks (`baseline/`, `bootstrap/`) — XML version attribute = reference, not scanned; SPEC.md closed-§T rows + `journal/` quote retired versions, exempt by scope
- empty output = parity; surviving `file:line` → bail: `stale Bootstrap/<ver> ref vs bootstrap_project.xml Endpoint version per §V.21 — version bump sweeps every ref, stale ref = silent-downgrade class surfacing at PUT time`

## §V.4 — idempotence recipe (extracted from SPEC.md §V.4)

- resume/skip gate verifies desired state, never a marker — marker outlives state loss
- published-package skip = content parity (embedded content digest), never existence alone — stale content silently starves config
- diff read-back survives delegate-view entities — list-GET optimization-500 → key-URL single-record GET fallback (closes §B.9)
- multi-view entity composite key legal, first key ! primary-view field filterable alone — cross-view `$filter` AND answers 200 `[]` while each predicate alone matches, key-URL GET 500s non-B9 so B9 fallback never fires; diff read-back filters on first key only, matches remaining key fields client-side; key-tuple uniqueness → §V.25 (closes §B.14)
- action file (`setup/*.yaml`) realizes state via contract action, not upsert — `done_when` live-state probe = verify gate both directions: apply skip (probe non-empty → `skip <action> (already done)`), diff drift (probe empty → `action <name>: not applied`, exit 2); probe coarse present/absent — action leaves no keyed record to field-diff

## §V.5 — tenant-map symptom recipe (extracted from SPEC.md §V.5)

- stale map symptom: tenant missing from sign-in + REST silently routes to default tenant; named tenants rerouted too
- post-login landed-tenant verify refuses session on mismatch (probe discovery → §T.21)

## §V.15 — cmd-grammar verb map (extracted from SPEC.md §V.15)

- `tenant` = control plane resource; `create` alone chains a data-plane bootstrap publish after the SSH create — §V.1 module split intact
- `config` = configuration ops: `init` local write, `show` local read, `check` live read-only preflight

## §V.17 — spec-state dependency recipe (extracted from SPEC.md §V.17)

- verify-gate leg: criterion never depends on capability another § records dead/pending unless citing the unblocking §T row
- retirement leg: dropping a cmd/surface re-routes every § recording it as recovery/fallback route — orphaned recorded role = silent capability loss surfacing live later
- premise leg: § text asserting repo/live state ("untracked", "exists", "published") probed @ authoring (`git ls-files` class) — stale premise plans the wrong edit, literal verify gate greens while the recorded concern stands

## §V.22 — reference-closure recipe (extracted from SPEC.md §V.22)

- extract-derived files strip fields referencing entities outside the baseline set (AccountGroup class)
- extract-derived files strip server-derived fields — PUT-tolerated, server keeps own derivation, sourced value = permanent drift (ChartOfAccountsOrder/CashAccount class, Translation* sibling)
- shipped init template set self-closing: templates' `features.yaml` enables every feature the shipped baseline templates require (closes §B.15)
- template set ships every recorded dependency-chain link its own verify chain needs — GL-posting chain = ledger + org-ledger link + GL prefs + calendar + open periods (closes §B.16)
