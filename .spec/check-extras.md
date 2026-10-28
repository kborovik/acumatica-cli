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

## §V.1 + §V.10 + §V.15 + §V.18 + §V.47 + §V.49 — drift-grep hook (mechanized §T.19 / §T.203 / §T.233 / §T.236)

- cmd: `.spec/scripts/check-extras.sh` — emits `id|verdict|evidence` rows
  per the /sdd:check extras-hook contract; exit 1 on any VIOLATE row
- V1 VIOLATE → bail: `plane-split import per §V.1 — SSH stays in tenant.py, REST stays in client.py`
- V10 VIOLATE → bail: `class subclasses BaseModel outside models.py per §V.10 — inherit models.Model`
- V15 VIOLATE → bail: `L1 extract/inventory/reconcile cmd per §V.15 — nest under survey`
- V18 VIOLATE → bail: `exit $LASTEXITCODE outside the _ssh choke point per §V.18 — strip call-site suffix / restore _ssh`
- V47 VIOLATE → bail: `dropped lifecycle-check helper per §V.47 — cold rebuild is compose, never wrap cmd`
- V49 VIOLATE → bail: `prose paragraph ≥3 sentences in human-facing Markdown per §V.49 — split the paragraph; lists/tables/fences exempt`

## §V.49 — md-prose-density (mechanized §T.203)

- cmd: `.spec/scripts/check-md-prose` (also via extras-hook above)
- scope: `README.md`, `src/acumatica_cli/templates/**/README.md`
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
- `--dry-run` scope: tenant record write (`apply`, `run`) + seed/inventory/findings emit (`survey extract|inventory|reconcile`); exempt: tenant lifecycle (`tenant create|delete|recycle`), `bootstrap` publish, `config init` scaffold
- enum-domain option value = click.Choice @ parse over V12-verified set, free string never forwarded to remote accepting any value (closes §B.20)

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
- `survey` = existing-tenant dual-reader noun; verbs `extract|inventory|reconcile`; never L1 those verbs; never aliases (check/snapshot-alias class)
- `survey extract` = REST seed reader: live GET → `--out` default `acu-config/` SEED_DIRS (V35); loads Instance
- `survey inventory` = offline dual-reader verb: SnapshotArtifact → `inventory/` summary+tables; no REST/SSH/password (V35/V37); never seed/apply/`acu-config/` write
- `survey reconcile` = offline dual-reader verb: inventory/ + optional `--config DIR` → `findings/` only; omit `--config` → inventory-only; no REST/SSH/password (V35/V36); never writes `acu-config/`
- `state` = data plane verb: capture derived state (balances/qty) → `state/`; views in `acu-config/views/`; views path required (V59); never seed/apply path (V32); hard-cut no `snapshot` alias
- cmd: `.spec/scripts/check-extras.sh` — V15 row; scope `src/`; pattern `@cli.command\("(extract|inventory|reconcile)"\)`
- surviving match (`V15|VIOLATE|file:line: …`, exit 1) → bail: `L1 extract/inventory/reconcile cmd per §V.15 — nest under survey`

## §V.17 — spec-state dependency recipe (extracted from SPEC.md §V.17)

- verify-gate leg: criterion never depends on capability another § records dead/pending unless citing the unblocking §T row
- retirement leg: dropping a cmd/surface re-routes every § recording it as recovery/fallback route — orphaned recorded role = silent capability loss surfacing live later
- premise leg: § text asserting repo/live state ("untracked", "exists", "published") probed @ authoring (`git ls-files` class) — stale premise plans the wrong edit, literal verify gate greens while the recorded concern stands

## §V.22 — reference-closure recipe (extracted from SPEC.md §V.22)

- extract-derived files strip fields referencing entities outside the baseline set (AccountGroup class)
- extract-derived files strip server-derived fields — PUT-tolerated, server keeps own derivation, sourced value = permanent drift (ChartOfAccountsOrder/CashAccount class, Translation* sibling)
- shipped init template set self-closing: templates' `features.yaml` enables every feature the shipped baseline templates require (closes §B.15)
- template set ships every recorded dependency-chain link its own verify chain needs — GL-posting chain = ledger + org-ledger link + GL prefs + calendar + open periods (closes §B.16)
- dir expansion alphabetical = default order within a seed dir; umbrella expands SEED_DIRS order first; filename prefixes encode default order; `depends_on` (V71) overrides via stable topo sort, tie-break = expansion order
- feature file resolve: `acu-config/bootstrap/features.yaml`

## §V.2 — bootstrap source closure (extracted from SPEC.md §V.2)

- package-embedded config = what — never company surface hardcoded in plugin source
- bootstrap feature set sources from `acu-config/bootstrap/features.yaml` (absent → built-in six)
- bootstrap endpoint contract = packaged `bootstrap_project.xml` sole SoT — never data-repo `project.xml` override; present data-repo file → hard error on bootstrap/publish/load_contract

## §V.3 — discovery resolution matrix (extracted from SPEC.md §V.3)

- required keys post-merge: `ACU_BASE_URL`, `ACU_PASSWORD` — unresolved → hard error naming key(s)
- `ACU_SSH`: key absent + base_url host → default `Administrator@<host>`; key present blank → empty (hosted/data-plane only); flag/env explicit wins; empty post-default fine for data-plane cmds
- `api_version`: `--api-version` flag ? → else `ACU_API_VERSION` env → else code default `25.200.001` (V27)
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

## §V.27 — env-sole-config gate (extracted from SPEC.md §V.27)

- cmd: `.spec/scripts/check-extras.sh` — V27 row; scope `src/`; pattern `matrix\.yaml|--cell|load_matrix|DatasetMatrix|MATRIX_FILENAME`
- exemptions: leftover/never/`no ``matrix.yaml``` comments (present leftover ignored)
- surviving match (`V27|VIOLATE|file:line: …`, exit 1) → bail: `live matrix.yaml/--cell/load_matrix site per §V.27 — leftover ignored, never loaded`
- allowlisted data-plane cmds: `apply`/`diff`/`run`/`survey extract`/`schema`/`bootstrap`/`state` + `config check` load `Instance` via `_resolve_instance`
- `api_version`: `--api-version` flag ? → else `ACU_API_VERSION` env → else code default `25.200.001`
- `base_url`: `--url` flag ? → else `ACU_BASE_URL` env → else hard error naming sources
- never load `matrix.yaml` (present leftover ignored)
- never `--cell`; never `target.yaml`
- `config check` never matrix line; never erp-match
- unknown `ACU_*` ignored except known keys incl `ACU_API_VERSION`

## §V.47 — no-lifecycle-cmd gate (extracted from SPEC.md §V.47)

- cmd: `.spec/scripts/check-extras.sh` — V47 row; scope `src/`; pattern `check_cmd|_run_lifecycle|_lifecycle_|_resolve_check_tenant`
- surviving match (`V47|VIOLATE|file:line: …`, exit 1) → bail: `dropped lifecycle-check helper per §V.47 — cold rebuild is compose, never wrap cmd`
- `acu config check` stays

## §V.19 — release-pipeline recipe (extracted from SPEC.md §V.19)

- sole path: `make release <part>` — `make check` first, then bump + CHANGELOG promote + commit, `make check` again on the promoted tree, then tag + push that tag (not `--tags`)
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
- layout `acu-config/{bootstrap,baseline,setup,master}/` + observer `acu-config/views/10-trial-balance.yaml` (numeric EndingBalance-class via `inquire:` or `gi:`; `acu-config/views/` ! SEED_DIRS)
- package templates ! derive from sibling `acumatica-gitops` seed trees; prune non-seed extras (`demo/`, Makefile, live `.env`, committed `state/`)
- package `templates/**/*.yaml` ! data only — no `#` comments (full-line `^\s*#` or trailing `\s#\s`); unit gate offline
- narrative/docs comments ! sibling `acumatica-gitops` separate files, never inlined package YAML
- never scaffolds `project.xml` — Bootstrap contract stays packaged SoT (V2/V21); not a second Bootstrap identity in data repo
- never scaffolds `matrix.yaml`; `.env` includes `ACU_BASE_URL` + `ACU_API_VERSION` placeholders
- `acu-config/views/` TB only w/ EndingBalance-class numeric money capture (V33)
- inventory-summary ! golden this pass
- golden `acu-scenario/` lifecycle: `10-seed-capital` (once+present) + `20-buy` + `30-build` + `40-sell` + README (gitops names)
- monoscenario `buy-sell` forbidden; skip-if-exists unchanged
- root SEED_DIRS ! scaffold; no customization tree; `<dir>` still optional (cwd)

## §V.32 — derived-state-observation recipe (extracted from SPEC.md §V.32)

- views `acu-config/views/*.yaml`, captures `state/<name>.yaml`
- `state` views path required (V59); `--out` default `state/` (no `config/snapshot/` fallback)
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

- every packaged `templates/acu-config/**` seed yaml (except `project.xml`, features synthesis, `acu-config/views/`) ! exactly one `seed_catalog.yaml` row
- multi-file same entity ! filter-split (one row per numbered file + optional filter/match partition)
- template file set = catalog file set = extract emit paths
- exclusion: row `template: false` = CLI test seed only — file ! under repo-root `acu-config/`, ! under packaged templates; out of template=catalog bijection; extract still emits (B43)
- no hand_only markers
- golden seed ! claim fields GET never returns (B11 class; packaging UOMs drop until verified read-back)

## §V.35 — dual-reader single-writer + vocab (extracted from SPEC.md §V.35)

- REST = live data-plane transport only
- entity = contract/REST identity
- seed = `acu-config/` SEED_DIRS YAML (CaC artifact, ! synonym for REST)
- tenant-snapshot artifact = SM203520 XML ZIP or `ac.exe export xml` folder (ingress only; ! side name like XMLDUMP; bare "snapshot" ! `state`)
- table/DAC = artifact identity
- inventory/ = offline IR (`tables/<Table>.yaml`)
- reconcile map = table↔entity (`snapshot_map` or identity match) never seed-name↔inventory-name
- readers = REST `extract` (seed under `acu-config/`) + offline `inventory` (artifact → `inventory/`)
- sole tenant mutator = `apply` PUT (V4)
- never SM203520 restore / `ac.exe import` / binary import
- `inventory/` + `findings/` never SEED_DIRS never `apply`/`diff` load
- v1 artifact path writes `inventory/` + `findings/` only — never `acu-config/` from artifact
- `extract` REST-only
- CLI cmds under `survey` (V15); never L1 extract/inventory/reconcile

## §V.38 — reconcile-normalize (extracted from SPEC.md §V.38)

- entity-vs-table compare ! pad-trim both sides on string keys+fields
- seed→inv key/field aliases declarative via snapshot_map (or package defaults); v1 `{table,entity}` rows still load
- FK fields resolve inv int IDs → CD via inventory lookup indexes (prefer compare in seed/CD space)
- first targets Account+Sub for *AcctID/*SubID on ReasonCode/VendorClass-class + PostingClass/CashAccount/OrderType freight
- global enums: per-row field→enum fold REST labels → DAC codes (Usage, Account Type/PostOption, bool_bit Active, CreditTerms Due/Disc/Visible, …)
- decimal-looking strings collapse trailing zeros (0 vs 0.000000) without mangling bare CDs like 000000
- never silent promote `acu-config/` (V36)

## §V.42 — inventory-map-coverage (extracted from SPEC.md §V.42)

- dual-reader masters (catalog entity or demo seed claims) ! snapshot_map table→entity (+ aliases/resolves/enums as needed) so `acu survey reconcile` findings = real gaps not join-alias noise
- intentional unmapped (txn/history/Notes/LoginTrace/ScreenPreferences/FeaturesSet-via-plugin) ! short docs table
- demo-not-seeded Default master → explicit non-goal note ok
- ! Bootstrap bump for map/catalog-only polish (gh #27)

## §V.43 — period-token (extracted from SPEC.md §V.43)

- `acu run` interpolates `${current_period}` → `MMyyyy` from host-local date @ process start
- expand every run `${var}` site (steps + expect + once present params)
- pure `period_mmYYYY(date)` sole format
- built-in available pre-steps (present ok); capture `${var}` still post-capture only
- `acu-config/views` / `acu state` ! expand token (params pinned literals)
- unknown `${…}` hard fail
- no ERP business-date probe; no `--as-of`/`--period` CLI v1 (gh #28)

## §V.25 — seed-file key identity (extracted from SPEC.md §V.25)

- extract dup key tuple → row failure per V24, file never emitted
- `load_baseline` dup key tuple → hard error naming entity + first dup tuple
- catches hand-authored files (§B.14 workaround class; closes §B.21)

## §V.30 — seed-layout (extracted from SPEC.md §V.30)

- apply/diff/run/state data path required (V59); never cwd-fill `acu-config/` or root SEED_DIRS
- umbrella dir w/ any SEED_DIRS child expands nested those subdirs fixed SEED_DIRS order; else `*.yaml` (skip `features.yaml`)
- never merge two trees; typed path used as given
- bootstrap features resolve `acu-config/bootstrap/features.yaml` only
- `project.xml` ! data-repo resolve (package SoT per V2/V21)
- extract `--out` default `acu-config/`; emit SEED_DIRS into that tree root (no nested `acu-config/acu-config`; no root emit, no `--layout`)

## §V.36 — seed authority (extracted from SPEC.md §V.36)

- v1 writers into `acu-config/` = REST extract + human only
- REST extract success for entity → seed/entity always wins vs inventory/table
- field/key conflict → `findings/` never silent overwrite seed
- post-v1 SnapshotMap promote only on REST gap (not prefer-artifact flag)
- unmapped tables never enter `acu-config/`

## §V.37 — tenant-snapshot artifact (extracted from SPEC.md §V.37)

- accept SM203520 XML ZIP (`manifest.xml` + `*.xml`) or `ac.exe export xml` table-XML folder
- both normalize one IR → inventory/
- binary `.adb` fail-closed named error
- never matrix `erp` match
- format tag "XML dump" ? for ingress only — never identity side name (table owns identity)

## §V.40 — runtime-counters (extracted from SPEC.md §V.40)

- seed bounds + NewSymbol fields: `NumberingID`, `StartNbr`, `EndNbr`, `WarnNbr`, `NbrStep` + StartDate? if screen requires
- NewSymbol insert-required per numbering-new-symbol invariant
- `LastNbr` (+ advanced counter) = runtime state — extract strips; diff ignores; apply never requires
- ! reset live counters every apply (gh #25)
- NumberingSequence seed ! bounds + NewSymbol, `LastNbr` (+ advanced counter)
- Tax `TaxSchedule.RevisionID` (bumped every schedule save) = counter under `include`d detail → path-qualified `_DIFF_IGNORE_PATHS`, ! bare field-name deny (`RevisionID` = KitSpecification key) (closes §B.46)

## §V.44 — pin-overlay (extracted from SPEC.md §V.44)

- optional Default-half overlays under `overlays/default-<half>/` keyed by resolved `api_version` (flag or `ACU_API_VERSION` or code default)
- config init scaffolds layout + known rewrites
- apply/diff/run require explicit overlay paths (V59); omit path prints help, never auto-compose
- `run` later same-basename wins across given dirs (`acu run acu-scenario overlays/default-<half>/acu-scenario`)
- apply/diff extra dirs append (umbrella SEED_DIRS if present)
- never `matrix.yaml`; never `--cell`
- CLI never requires long-running product branches
- never commit multi-version OpenAPI trees as SoT (`acu schema` live dump/gitignored)
- full seed version matrices live in data repos not CLI (extends V27)

## §V.48 — agent-help (extracted from SPEC.md §V.48)

- root `acu --help` ! MENTAL MODEL (data-repo layout, REST vs SSH planes, sole writer=`apply`, drift=`diff` exit 2, txns=`run`) + TYPICAL WORKFLOW (SSH box) + HOSTED path + CONFIG RESOLUTION + COMMAND MAP by intent + EXIT CODES + PATHS (`acu-config/` `acu-scenario/` only; apply/diff/run/state examples always show explicit data path)
- LLM agents learn tool from `--help` alone (README points agents @ root help)
- every subcommand help ! prerequisites + concrete examples + exit codes + related cmds
- accepts `-h` + `--help`
- help layout wide (`max_content_width` ≥ 100)

## §V.50 — segmented-key-seed (extracted from SPEC.md §V.50)

- SegmentedKey seed ! update existing `DimensionID` only (never insert new)
- package seeds `INVENTORY` + `BIZACCT` one alphanumeric segment `Length` 30 (DAC max)
- `ACCOUNT` + `INSITE` stay `Length` 10 (`SiteCD` NVarChar(10))
- never shrink `Length` after data exists
- key-URL GET after PUT ! return `SegmentID` + `Length`
- PUT `Length` ! persist so live InventoryID mask accepts 30
- silent HTTP 200 w/ omitted detail fields = mapping miss (closes §B.28) (gh #30)

## §V.52 — company-qty-precision (extracted from SPEC.md §V.52)

- Company CS101500 maps `DecPlQty` (`ShortValue`) + `WeightUOM`/`VolumeUOM` to view `commonsetup` (aspx DataMember; graph `Commonsetup`)
- unmapped PUT 200 ignores (B28 class)
- GET ! return mapped fields so extract/diff round-trip
- package seed `DecPlQty: 3` (kit BOM milligram-scale KG; DAC default 2)
- DistributionModule on → PUT ! send WeightUOM+VolumeUOM (`CommonSetup_RowPersisting`) even when extract strips GET-omit siblings (B26 StockItem class)
- contract shape change ! version bump (V21) (closes §B.30) (gh #34)

## §V.53 — user-role-membership (extracted from SPEC.md §V.53)

- SM201010 User.Roles and SM201005 Role.Users contract-detail PUT never write UsersInRoles (silent 200) on AllowedRoles, RolesByUser, RoleList, and UsersByRole
- persist path ! mapped contract detail
- prove via SQL UsersInRoles (session CompanyID)
- GET/diff membership per membership-diff-empty invariant
- UserRole ! Selected; never AllowedRoles (EPLoginTypeAllowsRole)
- closes §B.31, §B.32, §B.33 (gh #35)

## §V.54 — unwrap-row-id (extracted from SPEC.md §V.54)

- unwrap keeps `id` + `delete` on records and detail rows that also have value fields (wrap already leaves them bare)
- files-style descriptors that unwrap to only `id` stay elided
- GET `$expand` detail `id` ! round-trip onto later PUT so contract updates existing line
- missing id → insert + 500 Components commit (KitAssembly StockComponents class)
- closes §B.34 (gh #38)

## §V.57 — membership-diff-empty (extracted from SPEC.md §V.57)

- mapped GET Role.Users / User.Roles ? empty while UsersInRoles has session-company rows
- diff ! flag Role.Users / User.Roles missing
- skip those details in diff or read live source that returns rows
- AssignUser apply + SQL-proven membership → `acu diff` exit 0 on Role.Users / User.Roles
- closes §B.37 (gh #39)

## §V.58 — numbering-new-symbol (extracted from SPEC.md §V.58)

- NumberingSequence insert ! Header NewSymbol (or UserNumbering if screen requires pair)
- Bootstrap contract maps it
- apply PUT new NumberingID w/ `NewSymbol: <NEW>` ! 422
- re-apply existing id succeeds (no NewSymbol mask 422 on update)
- NewSymbol seed; mapped GET returns it
- GET-omit → `not returned by endpoint` drift (mapping-miss per segmented-key-seed invariant; not write-only-get-omit ignore)
- LastNbr strip per runtime-counters invariant
- closes §B.38 (gh #44)

## §V.62 — remote-command-safety (extracted from SPEC.md §V.62)

- identity strings (`--login`, `ACU_USER`) ! match `^[A-Za-z0-9_-]+$` before any SSH, bad → exit 1 (never click exit 2 — 2 = drift)
- secrets (`ACU_PASSWORD`) via stdin only — never local `ssh` argv, never remote cmd text, never PS double-quoted string
- other free string single-quoted `'`→`''`
- closes §B.39 (gh #48)

## §V.64 — idempotent-retry (extracted from SPEC.md §V.64)

- idempotent set: GET (incl 202 status poll) + seed `apply` keyed PUT + inquiry PUT (`run` present/expect, `state`)
- bounded `retry_tries` 3 total, exp backoff (1s base, x2) + ≤50% jitter, each retry `output.info` stderr line
- retry on ConnectError/ReadTimeout/RemoteProtocolError + 502/503/504
- login retries ConnectError + 502/503/504 only (timed-out login may hold session, V6)
- never auto-retry: action POST (`invoke`), `run` `put` steps, CustomizationApi POSTs
- last failure surfaces unchanged (V45 classes) (gh #52)

## §V.65 — generated-load-idempotence (extracted from SPEC.md §V.65)

- per-doc `present.get` `{entity, filter}` row-exists probe on external ref (`$top=1`)
- report label `<path>#<n>` multi-doc (1-based, empty doc skipped, numbering kept), `<path>` one-doc
- not-run tail of one file folds to one range line, JSON keeps every label (gh #73)

## §V.66 — bulk-load-concurrency (extracted from SPEC.md §V.66)

- N=1 = pre-gh-#71 behavior
- server serializes per session; each session = 1 API-user license slot
- login fail → logout opened ones (V6), exit 1 naming N + license cap, no write (fail fast, never degrade)
- files stay sequential in order (V22 apply order, run lifecycle chain)
- serial-only: ActionFile, Company file (+ V5 re-login every pooled session after Company PUT), scenario file w/ any `inquire` delta expect (concurrent docs corrupt pre/post snapshot) → `output.info` line naming file
- V45 under pool: record fail → continue; hard-stop (ApplyStopped) | run step error → no new dispatch, in-flight finish, report stopped-at + not-applied|not-run
- summary + JSON counts same as N=1 for same outcomes; result lines self-labelled (interleave ok) (gh #71)

## §V.67 — resume-checkpoint (extracted from SPEC.md §V.67)

- JSONL append, flush per line (kill-safe); line `{tenant, unit, digest}`
- unit = `<entity> [<keys>]` | `action <name> [<entity>]` (apply) | scenario name (run)
- digest = sha256 canonical JSON of record (password fields excluded) | action file | {scenario doc, file basename, doc number} + referenced built-ins (`${current_period}` value) (run)
- written after success only (apply PUT/invoke ok; run passed|skipped)
- load: same tenant (case-insensitive) + unit + digest → skip, stdout `skip <label> (checkpoint)`, no HTTP; digest or tenant differ → normal path (edited record re-applies)
- summary + JSON `checkpoint` count
- absent file → created; malformed line → exit 1 before any HTTP; torn last line (no newline, unparseable) dropped, unit re-runs
- `--checkpoint` × `--plan`|`--dry-run` → usage error (gh #71)

## §V.68 — migration-entity split (extracted from SPEC.md §V.68)

- transaction document: one doc per ERP document, `once.get` external-ref guard per V65
- transaction ! catalog row, ! `acu-config/` seed, ! extract (gh #72)

## §V.69 — key-URL-read entities (extracted from SPEC.md §V.69)

- seed display value (name), never per-tenant surrogate id
- diff + apply live-read + extract read via key-URL GET only (no list `$filter`, no `_prefetch`; extract lists keys via `$select` then key-URL per row, B9 path)
- opt-in = code table in seed.py (apply/diff never load seed_catalog)
- key-URL miss = missing on tenant (gh #78)

## §V.70 — external-file-storage (extracted from SPEC.md §V.70)

- SM202540 tables keyed CompanyID only; Corrigan company = tenant
- `Provider` as UI label `Local File Folder` (DB stores `PX.SM.PXBlobStorageFileProvider`; GET returns label; PUT accepts label)
- top-level `LocalFolder` → `Settings.Value` current row (file provider has one setting; folder ! pre-exist + writable; acu never creates it)
- contract detail arrays on SM202540 bind fieldless rows → ! map `Settings` or `FilesInDatabase` as details
- setup action files order Enable Provider → Switch Direction, `done_when` `IsActive eq true`, `AllowWrite eq true` (Switch Direction toggles — ungated re-run reverses direction)
- Move Files to Storage + Move to Database ! map, ! invoke (tenant `FilesInDatabase` lists System CompanyID 1 shared revisions → no tenant-scoped done gate, move risks shared rows)
- pre-enable DB files stay in DB, new files → folder
- second `apply` → 0 invoke, seed PUT on active (read-only) config ! error
- `DisableDeleteOnExternalFileStorage` ! seed, ! catalog include, ! extract
- `diff` + `survey extract` read `Provider` + `LocalFolder` back via Bootstrap; `diff` covers actions via `done_when` probe only (gh #74)

## §V.71 — seed-depends-on (extracted from SPEC.md §V.71)

- `depends_on` = list of paths relative to seed-tree root (dir holding SEED_DIRS, e.g. `acu-config/`; catalog `file` form, e.g. `master/77-taxes.yaml`); tree root = parent of file's SEED_DIRS dir
- `depends_on` on file outside SEED_DIRS dir → load error
- topo sort covers `apply`/`diff` incl `--plan`, `--changed-only`, `--dry-run`; tie-break = V30 expansion order (SEED_DIRS, alpha, typed-arg order)
- hard error exit 1 before session/PUT, reported w/ load failures (V45): unknown dep (no such file), cycle (print cycle path; self-dep = cycle)
- dep ∉ expanded set → warn (named), no edge, assumed applied, never auto-include (single-file diff, stopped-run remainder)
- `--concurrency` (V66) + `--checkpoint` (V67) unchanged — files sequential, order only; action checkpoint digest excludes `depends_on`
- `survey extract` ! emit `depends_on` (`--force` re-extract drops hand-authored) (gh #80)
