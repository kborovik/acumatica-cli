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

## §V.1 + §V.10 + §V.18 + §V.49 — drift-grep hook (mechanized §T.19 / §T.203)

- cmd: `.spec/scripts/check-extras.sh` — emits `id|verdict|evidence` rows
  per the /sdd:check extras-hook contract; exit 1 on any VIOLATE row
- V1 VIOLATE → bail: `plane-split import per §V.1 — SSH stays in tenant.py, REST stays in client.py`
- V10 VIOLATE → bail: `class subclasses BaseModel outside models.py per §V.10 — inherit models.Model`
- V18 VIOLATE → bail: `exit $LASTEXITCODE outside the _ssh choke point per §V.18 — strip call-site suffix / restore _ssh`
- V49 VIOLATE → bail: `prose paragraph ≥3 sentences in human-facing Markdown per §V.49 — split the paragraph; lists/tables/fences exempt`

## §V.49 — md-prose-density (mechanized §T.203)

- cmd: `.spec/scripts/check-md-prose` (also via extras-hook above)
- scope: `README.md`, `docs/*.md`, `src/acumatica_cli/templates/**/README.md`
- prose paragraph = blank-line block; list / table / blockquote / fence / heading exempt
- surviving match (`V49|VIOLATE|file:line: N sentences`, exit 1) → bail: `prose paragraph ≥3 sentences in human-facing Markdown per §V.49 — split the paragraph`

## §V.9 — output-discipline recipe (extracted from SPEC.md §V.9)

- no bare `print()` — ruff T20 enforces
- stdout = data (one record per line, greppable); stderr = process (steps, warnings, errors)
- ASCII-only incl. TTY — prefixes `+` ok, `!` warn, `x` error, deterministic, survive piping
- rich auto-degrades non-TTY; no manual TTY branching outside `output.py`
- `NO_COLOR`/`FORCE_COLOR` respected; markup/emoji/highlighting off; table box ASCII; spinner ASCII
- expected failure = one `x` line, no traceback (`ACU_DEBUG=1` re-raises); validation error → `SystemExit("msg")`
- transport/network class (`httpx.TransportError` + subclasses: connect/timeout/TLS) @ `main` choke → rewrite to one friendly `x` line (class + `base_url` or host + action hint: check `ACU_BASE_URL` / network / instance); never `str(exc)` dump for that class; HTTP status + Acumatica `exceptionMessage` path unchanged; `config check` rest fail lines may reuse same rewrite; `ACU_DEBUG=1` re-raises full chain
- once-skip line `skip <path> (once: already present)` on stdout data path (gh #19)
- no `--json` — plain text = machine interface
- `tenant list` table title `Tenants on <hostname>` — host from `base_url` (urlparse), never full URL (closes §B.27)
- progress: long single-op (SSH ac.exe, pool recycle, swagger dump, artifact parse, reconcile load/compare) → `output.step` (TTY spinner / piped stderr process line)
- multi-unit (apply/diff/extract/run/state file|row|view) → stdout per-unit banner before unit work; never silence through multi-second work

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
- scenario `once` skip-if-present via authored `present` inquire-absolute probe (not marker file; cold expects skipped on skip) (gh #19)
- `$filter` key literal types by YAML scalar — bool/number bare, string quoted (closes §B.23)

## §V.5 — tenant-map symptom recipe (extracted from SPEC.md §V.5)

- stale map symptom: tenant missing from sign-in + REST silently routes to default tenant; named tenants rerouted too
- post-login landed-tenant verify refuses session on mismatch (probe discovery → §T.21)

## §V.15 — cmd-grammar verb map (extracted from SPEC.md §V.15)

- `tenant` = control plane resource; verbs `list|create|delete|recycle`; `create` alone chains a data-plane bootstrap publish after the SSH create — §V.1 module split intact; `recycle` = site-wide app-pool restart only (no REST, no `--id`)
- `bootstrap` = data plane verb: publish AcuBootstrap (`/CustomizationApi`); optional post-publish recycle when `ACU_SSH` set; `--export` local-only zip write (no REST, no SSH)
- `config` = configuration ops: `init` local write, `show` local read, `check` live read-only preflight
- `inventory` = offline dual-reader verb: SnapshotArtifact → `inventory/` summary+tables; no REST/SSH/password (V35/V37); never seed/apply/`config/` write
- `reconcile` = offline dual-reader verb: inventory/ + optional config/ → `findings/` only; no REST/SSH/password (V35/V36); never writes `config/`
- `state` = data plane verb: capture derived state (balances/qty) → `state/`; views in `config/views/`; never seed/apply path (V32); hard-cut no `snapshot` alias

## §V.17 — spec-state dependency recipe (extracted from SPEC.md §V.17)

- verify-gate leg: criterion never depends on capability another § records dead/pending unless citing the unblocking §T row
- retirement leg: dropping a cmd/surface re-routes every § recording it as recovery/fallback route — orphaned recorded role = silent capability loss surfacing live later
- premise leg: § text asserting repo/live state ("untracked", "exists", "published") probed @ authoring (`git ls-files` class) — stale premise plans the wrong edit, literal verify gate greens while the recorded concern stands

## §V.22 — reference-closure recipe (extracted from SPEC.md §V.22)

- extract-derived files strip fields referencing entities outside the baseline set (AccountGroup class)
- extract-derived files strip server-derived fields — PUT-tolerated, server keeps own derivation, sourced value = permanent drift (ChartOfAccountsOrder/CashAccount class, Translation* sibling)
- shipped init template set self-closing: templates' `features.yaml` enables every feature the shipped baseline templates require (closes §B.15)
- template set ships every recorded dependency-chain link its own verify chain needs — GL-posting chain = ledger + org-ledger link + GL prefs + calendar + open periods (closes §B.16)
- dir expansion alphabetical = sole ordering mechanism within a seed dir; umbrella expands SEED_DIRS order first; filename prefixes encode order
- feature file resolve: `config/bootstrap/features.yaml` or `bootstrap/features.yaml`

## §V.2 — bootstrap source closure (extracted from SPEC.md §V.2)

- package-embedded config = what — never company surface hardcoded in plugin source
- bootstrap feature set sources from data-repo dual path `config/bootstrap/features.yaml` then `bootstrap/features.yaml` (absent → built-in six)
- bootstrap endpoint contract = packaged `bootstrap_project.xml` sole SoT — never data-repo `project.xml` override; present data-repo file → hard error on bootstrap/publish/load_contract

## §V.3 — discovery resolution matrix (extracted from SPEC.md §V.3)

- required keys post-merge: `ACU_BASE_URL`, `ACU_PASSWORD` — unresolved → hard error naming key(s)
- `ACU_SSH`: key absent + base_url host → default `Administrator@<host>`; key present blank → empty (hosted/data-plane only); flag/env explicit wins; empty post-default fine for data-plane cmds
- `api_version`: never from env (`ACU_API_VERSION` ignored if present); `--api-version` flag ? → else `target.yaml` `default_api` when present → else code default `25.200.001` (V27)
- tenant CRUD hard-errors when `ACU_SSH` empty post-default, names key

## §V.20 — seed endpoint resolution (extracted from SPEC.md §V.20)

- literal forms: `Bootstrap/<ver>` | `Default/<ver>`; symbolic: `bootstrap` | `default`
- symbolic `bootstrap` → active package version @ load from packaged `bootstrap_project.xml` only
- symbolic `default` → `Default/<Instance.api_version>` @ HTTP via `client._url` (never load-rewritten)
- §B.8 class — Bootstrap `Currency` vs Default CM201000 list; symptom returns behind clean apply

## §V.24 — extract exit/msg matrix (extracted from SPEC.md §V.24)

- row failure (fetch or synth) → `x <name>: <reason>`, run continues to next manifest row
- `PXSetupNotEnteredException` 500 = empty-state class → `skip <path> (screen setup not entered)`, not failure
- run ends w/ summary; exit 0 all rows wrote or skipped clean, 1 any row failed; never 2

## §V.26 — org-scoped view audit (extracted from SPEC.md §V.26)

- contract entity over org-scoped screen (GL201100 class) answers 200 [] on multi-org tenant w/o org parameter
- multi-org verify gate applies only when multi-org surface in scope (single-org demo strategy, multi-org = paid engagement)

## §V.27 — dataset-target gate (extracted from SPEC.md §V.27)

- allowlisted data-plane cmds: `apply`/`diff`/`run`/`extract`/`schema`/`bootstrap`/`state` + `config check`
- present target → `load_instance` sets `Instance.api_version` = `default_api` when `--api-version` flag absent (source-merge; dual-source match gate retired); invalid → hard fail any loader
- missing → warn on `config check` unless `--strict`; api_version stays flag or code default `25.200.001`
- never `ACU_API_VERSION` env pin; unknown `ACU_*` ignored
- gate ! inside bare `_resolve_instance`/`pass_instance` for hard-fail-on-mismatch (retired); target still loadable for erp claim + config check/show surface
- `config check` target line: present → `ok target (api_version from default_api=…; erp=… claimed)`; no mismatch fail
- `erp` live when `GET /entity` wrapper has `version.acumaticaBuildVersion` → major.minor match `target.erp` else fail; bare array / no build id → skip (claimed still on target line); never SSH/sqlcmd (V1)

## §V.19 — release-pipeline recipe (extracted from SPEC.md §V.19)

- sole path: `make release <part>` — `make check` first, then bump + CHANGELOG promote + commit + tag + push
- never local `gh release create`
- Keep-a-Changelog root `CHANGELOG.md`: user-facing work appends under `## Unreleased` (`### Added` / `### Changed` / `### Fixed`) during development
- `make release` promote: move Unreleased body → `## [vX.Y.Z] - YYYY-MM-DD` (version = post-bump `uv version --short`); leave empty `## Unreleased` heading block; empty/no bullet Unreleased → hard fail (nothing to ship)
- release commit includes `CHANGELOG.md` + `pyproject.toml` (+ lock if bumped) together
- tag `v*` → `release.yml` re-runs CI check (`workflow_call` → `ci.yml`) then `uv build` + `pypa/gh-action-pypi-publish` OIDC (`id-token: write`) + GH release create with `dist/*` artifacts + notes from promoted CHANGELOG version section (not sole `--generate-notes`)
- trusted publisher on pypi.org: owner `kborovik`, repo `acumatica-cli`, workflow `release.yml`; no PyPI API token
- tag `v<version>` == pyproject `version`
- CI also runs on every push/PR to `main`

## §V.21 — contract identity (extracted from SPEC.md §V.21)

- entity or field shape change in packaged `bootstrap_project.xml` ! version bump
- sole active path: packaged `bootstrap_project.xml` (never data-repo `project.xml`)
- version held → older build's digest gate republishes prior contract under same identity (silent downgrade, no version signal in seed failures)
- single contract line (identity = Endpoint name+version in package XML; not dual minimal/full versions)
- version-ref parity scan → existing §V.21 parity recipe above

## §V.28 — init-template recipe (extracted from SPEC.md §V.28)

- `config init` single full seed — no `--flavor`
- layout `config/{bootstrap,baseline,setup,master}/` + observer `config/views/10-trial-balance.yaml` (numeric EndingBalance-class via `inquire:` or `gi:`; `config/views/` ! SEED_DIRS)
- package templates ! derive from sibling `acumatica-gitops` seed trees; prune non-seed extras (`demo/`, Makefile, live `.env`, committed `state/`)
- package `templates/**/*.yaml` ! data only — no `#` comments (full-line `^\s*#` or trailing `\s#\s`); unit gate offline
- narrative/docs comments ! sibling `acumatica-gitops` separate files, never inlined package YAML
- never scaffolds `project.xml` — Bootstrap contract stays packaged SoT (V2/V21); not a second Bootstrap identity in data repo
- `config/views/` TB only w/ EndingBalance-class numeric money capture (V33)
- inventory-summary ! golden this pass
- golden `scenario/` lifecycle: `10-seed-capital` (once+present) + `20-buy` + `30-build` + `40-sell` + README (gitops names)
- monoscenario `buy-sell` forbidden; skip-if-exists unchanged
- root SEED_DIRS ! default scaffold; V30 dual-layout still honors legacy root data repos

## §V.32 — derived-state-observation recipe (extracted from SPEC.md §V.32)

- views `config/views/*.yaml`, captures `state/<name>.yaml`
- bare defaults hard-cut those paths (no `config/snapshot/` fallback)
- never SEED_DIRS/seed shape; never `endpoint:` symbols; `apply`/`diff` never load them
- `capture:` allowlist only; money = Decimal → fixed-point string @ `decimals` (default 2)
- sort by `key` always; key-tuple collision → exit 1; no body timestamps
- `erp:` header from live build; view `params` pinned in YAML not runtime-resolved
- one-row-per-line flow YAML
- exit 0 write or compare (change fine bare); exit 1 op fail; exit 2 only `--assert-unchanged` when moved
- `--diff` write nothing; `--dry-run` local only
- CLI verb `state` only — hard-cut drop `snapshot` alias (T112)
- ! SM203520 tenant snapshot; ! inventory/ ! findings/ (those = dual-reader products V35, not balances)

## §V.33 — observation-source recipe (extracted from SPEC.md §V.33)

- each view exactly one of `gi:`|`entity:`|`inquire:`
- `entity:` → contract REST list GET `Default/<Instance.api_version>` (no per-view `endpoint:` pin v1)
- `gi:` → OData (`/t/<tenant>/api/odata/gi/<Name>` 24R2+, legacy form ok); Expose via OData required; `params` fail-closed vs `$metadata` @ load
- GI definition = seed under `master` when GenericInquiry surface V12-verified (`apply` creates, `state` reads)
- `inquire:` → contract inquiry PUT `$expand=Results` (same idiom as `run` expect/present; `params` pinned in view YAML; optional row `match` filter)
- stem `trial-balance` ! capture ≥1 numeric money field (fixed-point @ `decimals`) — roster-only `entity: Account` forbidden for that stem
- distribution golden: TB → EndingBalance-class (`inquire: AccountSummaryInquiry` or `gi:` LAB5-TrialBalance)
- inventory-summary QtyOnHand golden ! shipped this pass (`InventorySummaryInquiry` warehouse-only → empty Results on live; optional later)
- params pinned literals — no runtime calendar or `${current_period}` resolve (scenario run tokens = V43, not views)

## §V.34 — seed-catalog completeness (extracted from SPEC.md §V.34)

- every packaged `templates/config/**` seed yaml (except `project.xml`, features synthesis, `config/views/`) ! exactly one `seed_catalog.yaml` row
- multi-file same entity ! filter-split (one row per numbered file + optional filter/match partition)
- template file set = catalog file set = extract emit paths
- no hand_only markers
- golden seed ! claim fields GET never returns (B11 class; packaging UOMs drop until verified read-back)

## §V.35 — dual-reader single-writer + vocab (extracted from SPEC.md §V.35)

- REST = live data-plane transport only
- entity = contract/REST identity
- seed = `config/` SEED_DIRS YAML (CaC artifact, ! synonym for REST)
- tenant-snapshot artifact = SM203520 XML ZIP or `ac.exe export xml` folder (ingress only; ! side name like XMLDUMP; bare "snapshot" ! `state`)
- table/DAC = artifact identity
- inventory/ = offline IR (`tables/<Table>.yaml`)
- reconcile map = table↔entity (`snapshot_map` or identity match) never seed-name↔inventory-name
- readers = REST `extract` (seed under `config/`) + offline `inventory` (artifact → `inventory/`)
- sole tenant mutator = `apply` PUT (V4)
- never SM203520 restore / `ac.exe import` / binary import
- `inventory/` + `findings/` never SEED_DIRS never `apply`/`diff` load
- v1 artifact path writes `inventory/` + `findings/` only — never `config/` from artifact
- `extract` REST-only

## §V.38 — reconcile-normalize (extracted from SPEC.md §V.38)

- entity-vs-table compare ! pad-trim both sides on string keys+fields
- seed→inv key/field aliases declarative via snapshot_map (or package defaults); v1 `{table,entity}` rows still load
- FK fields resolve inv int IDs → CD via inventory lookup indexes (prefer compare in seed/CD space)
- first targets Account+Sub for *AcctID/*SubID on ReasonCode/VendorClass-class + PostingClass/CashAccount/OrderType freight
- global enums: per-row field→enum fold REST labels → DAC codes (Usage, Account Type/PostOption, bool_bit Active, CreditTerms Due/Disc/Visible, …)
- decimal-looking strings collapse trailing zeros (0 vs 0.000000) without mangling bare CDs like 000000
- never silent promote config/ (V36)

## §V.42 — inventory-map-coverage (extracted from SPEC.md §V.42)

- dual-reader masters (catalog entity or demo seed claims) ! snapshot_map table→entity (+ aliases/resolves/enums as needed) so `acu reconcile` findings = real gaps not join-alias noise
- intentional unmapped (txn/history/Notes/LoginTrace/ScreenPreferences/FeaturesSet-via-plugin) ! short docs table
- demo-not-seeded Default master → explicit non-goal note ok
- ! Bootstrap bump for map/catalog-only polish (gh #27)

## §V.43 — period-token (extracted from SPEC.md §V.43)

- `acu run` interpolates `${current_period}` → `MMyyyy` from host-local date @ process start
- expand every run `${var}` site (steps + expect + once present params)
- pure `period_mmYYYY(date)` sole format
- built-in available pre-steps (present ok); capture `${var}` still post-capture only
- `config/views` / `acu state` ! expand token (params pinned literals)
- unknown `${…}` hard fail
- no ERP business-date probe; no `--as-of`/`--period` CLI v1 (gh #28)
