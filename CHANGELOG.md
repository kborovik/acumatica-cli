# Changelog

## Unreleased

### Changed

- **`acu check` leaves the rebuilt tenant (V47):** lifecycle is now
  `delete → create → apply → run → diff` with **no post-clean delete**.
  Green cells keep the tenant for manual inspect (Account Summary, `acu state`,
  demos). Failures also leave the tenant (no post-clean). Pre-clean delete
  still runs so each check starts cold. `--yes` remains accepted for
  muscle-memory only.

## [v0.24.0] - 2026-08-02

### Added

- **`matrix.yaml` multi-host pin+where (V27/V44):** ordered `cells` with
  `id`+`erp`+`default_api`+`base_url`. Global `--cell` selects a cell
  (omit → first). Active cell sources `Instance.api_version` and `base_url`
  when flag/env leave them unset. Retires `target.yaml`.
- **`acu check` lifecycle (V47):** cold
  `delete → create → apply → run → diff` per matrix cell (post-clean delete
  dropped in Unreleased — tenant left for inspect). Requires matrix + SSH +
  tenant. `--all` walks cells continue-on-fail + aggregate exit; never exit 2
  (diff drift = cell fail). Distinct from `acu config check` preflight.
- **Overlays in `config init` template (V44):** scaffold `overlays/README.md` +
  `overlays/default-24.200.001/` (KitAssembly Type `Assembly` for lab 25r1 /
  Default half `24.200.001`). Future halves = new `overlays/default-<half>/`
  dirs; no long-running product branches.
- **Bare pin auto-compose (V44):** when path args are omitted, `acu apply` /
  `acu diff` append `overlays/default-<api>/` config seed dirs when present;
  `acu run` replaces same-basename files from
  `overlays/default-<api>/scenario/`. Explicit path args stay fully manual.
  `api` = resolved `Instance.api_version` (matrix cell `default_api`).

### Changed

- **`config init` scaffolds `matrix.yaml`** (one default cell); omits sticky
  `ACU_BASE_URL` when the cell carries where. No `target.yaml`.

## [v0.23.1] - 2026-08-02

### Added

- **Multi-host matrix docs (V44):** trunk seed + per-host `target.yaml` +
  optional Default-half overlays as ordinary path args (no `apply --overlay`);
  no long-running product branches; no multi-version OpenAPI trees. Cross-link
  [acumatica-gitops#2](https://github.com/kborovik/acumatica-gitops/issues/2).
  Curated CLI compat profiles deferred.

### Changed

- **`acu apply` multi-error summary (V45):** per-record PUT failure reports and
  continues (later records and files still run); exit 1 with aggregated errors
  when any failed — never silent partial. Exit 2 stays with `diff`.
- **422 field-level errors (V46):** PUT/action `RuntimeError` detail includes
  nested `Field.error` paths under the body/`entity` (and detail rows), not
  status or top-level `exceptionMessage` alone.

### Fixed

- **User Roles membership docs (T189):** Bootstrap cold PUT does not durable
  User `Roles` membership (live GET often `Roles: []`); identity seed is the
  reliable path; document package membership as offline shape only.

## [v0.23.0] - 2026-08-01

### Fixed

- **Bootstrap NumberingSequence view maps (T159):** CS201010 graph is
  `NumberingMaint` with views `Header` (Numbering) + `Sequence`
  (NumberingSequence). Contract 1.2.0/1.3.0 mapped `Sequence`/`SequenceDetail`
  (wrong) so every PUT answered 422
  `The provided value ' <NEW>' does not match the required input mask
  '>aaaaaaaaaa'`. Contract bump **1.3.0 → 1.4.0** (V21); republish Bootstrap
  before `acu apply` of `05-numbering-sequences.yaml`.

## [v0.22.0] - 2026-08-01

### Changed

- **Bootstrap contract is package SoT only:** `load_contract` / publish /
  symbolic `endpoint: bootstrap` always use packaged `bootstrap_project.xml`.
  Data-repo `config/bootstrap/project.xml` or `bootstrap/project.xml` is not a
  seed — present → hard error naming package SoT. `acu config init` no longer
  scaffolds `project.xml`. Remove any checked-in data-repo copy on upgrade.

## [v0.21.0] - 2026-08-01

### Added

- **`${current_period}` on `acu run` (gh #28 / V43):** scenario interpolator
  expands the built-in token to host-local `MMyyyy` at process start on every
  `${var}` site — steps, `expect` inquire params, and `once.present` params.
  Package lifecycle scenarios use the token for AccountSummaryInquiry Period;
  `config/views` / `acu state` stay pinned literals (reproducible `state/`
  commits). Unknown `${…}` still hard-fails. No ERP business-date probe and
  no `--as-of` / `--period` CLI in v1 (host calendar can skew from ERP
  business date if those differ).

## [v0.20.2] - 2026-07-31

### Changed

- **CLI progress on long single-ops (T169–T171 / V9):** `acu tenant delete`
  wraps ac.exe delete in `output.step` (matching create) and keeps the
  recycle step; `acu inventory` steps artifact parse before banner + write/skip
  emit; `acu reconcile` steps load+compare before findings emit. TTY spinner /
  piped stderr process line; never silence multi-second work.

## [v0.20.1] - 2026-07-31

### Added

- **`acu tenant create --login NAME` without `--id`:** omit `--id` to allocate
  the next free CompanyID (`max(list)+1` from the live tenant list). When the
  login already exists, omit `--id` to adopt the live id and skip ac.exe create
  (republish route). Pass `--id` only when you need a specific CompanyID; a
  mismatch against an existing login is still a hard error.
- **`acu tenant delete --login NAME`:** delete a tenant by sign-in name as an
  alternative to `--id`. Pass exactly one of `--id` or `--login`.

## [v0.20.0] - 2026-07-30

### Changed

- **Keep-a-Changelog release path (T163–T165 / V19):** `gmake release`
  hard-fails when `## Unreleased` has no bullets, promotes the body to
  `## [vX.Y.Z] - YYYY-MM-DD` via `scripts/changelog`, and commits
  `CHANGELOG.md` with `pyproject.toml`. Tag `v*` GH release notes come from
  that promoted section (`scripts/changelog notes` + `--notes-file`), not
  sole auto-generated notes. README documents Unreleased duty + sole release
  path. No PyPI publish (private repo) — install via git URL or editable
  clone. SPEC §I.pkg, §V.19, `.spec/check-extras.md` §V.19.
- **Primary repo URL:** docs and `pyproject.toml` point at
  https://github.com/kborovik/acu-cli (active). `kborovik/acumatica-cli` is
  demo-only and is no longer referenced for install/clone.

### Added

- **Inventory map coverage polish (T159–T162, gh #27 / V42):** package
  `snapshot_map.yaml` joins Default masters without Bootstrap bumps —
  PaymentMethod field/enum aliases (`Descr`/`IsActive`/`UseFor*`/`PaymentType`),
  CustomerClass `ClassID`→`CustomerClassID` + AR/Sales account FK resolve,
  INSite `WarehouseID`→`SiteCD`, `INLocation`→Warehouse, ItemClass/StockItem
  natural-key aliases + ItemStatus enum, `PaymentMethodAccount`→PaymentMethod.
  LotSerialClass is an explicit **non-goal** (demo never claims
  `DfltLotSerClassID`; `INLotSerClass` stays unmapped). Offline tests prove
  LAB5-class fixtures produce 0 join-alias false deltas for those masters.
  Docs: [docs/demo-seed.md](docs/demo-seed.md) inventory table → entity →
  endpoint table + intentional-unmapped classes.
- **Bootstrap *Preferences field depth (T154–T158, gh #26 / V41):** Bootstrap
  contract `1.3.0` deepens GL/IN/AP/AR/SO/PO/CA Preferences with a **curated**
  field set (numbering IDs where package sequences exist, post/hold/control
  policy knobs that LAB5 rebuilds care about). Not a full DAC mirror —
  lot-class/site/default-class FKs and unsequenced CA transfer numbering stay
  out. Catalog includes + package templates updated; `INADJUST` added to
  `05-numbering-sequences.yaml`; snapshot_map bool_bit enums for new policy
  bits. Docs: [docs/demo-seed.md](docs/demo-seed.md) prefs field list + apply
  order vs numbering/warehouse/lot class.
- **Bootstrap NumberingSequence (T150–T153, gh #25):** Bootstrap contract
  `1.2.0` exposes NumberingSequence on CS201010 — key `NumberingID`, bounds
  `StartNbr`/`EndNbr`/`WarnNbr`/`NbrStep`/`StartDate` (+ `Descr`). `LastNbr`
  is runtime state and is omitted from the contract (V40). Catalog +
  `snapshot_map` cover the entity; package seed
  `config/master/05-numbering-sequences.yaml` (LAB5-class module sequences,
  bounds only) sorts before master prefs that may reference `*NumberingID`
  (V22). **LastNbr pipeline (T152/V40):** extract hard-strips `LastNbr`;
  diff ignores it; apply never PUTs it (hand-authored seed cannot reset
  live counters). Docs: [docs/demo-seed.md](docs/demo-seed.md) numbering
  seed vs prefs apply order + bounds/runtime rule; README + package
  template map.
- **Bootstrap Role + User + membership (T145–T149, gh #24):** Bootstrap
  contract `1.1.0` exposes Role (SM201005) and User (SM201010) with User
  detail `Roles` for membership. Package seeds `config/master/90-roles.yaml`
  then `91-users.yaml` (demo `soadmin` / `SO Admin`). Password is write-only
  on virgin apply when present in YAML; extract strips `Password` /
  `b64__Password`; diff ignores password fields (V39). Warm re-apply never
  resets an existing user's password. Docs: [docs/demo-seed.md](docs/demo-seed.md)
  Role/User apply order + password rule; README + package template map.
- **`acu tenant recycle` (T142–T144):** site-wide IIS app-pool restart over SSH
  (`Restart-WebAppPool`). Reloads the tenant map (V5) and drops every session
  so concurrent API-user license slots free up. Confirm prompt; `--yes` skips.
  Empty `ACU_SSH` hard-errors like other tenant cmds. Landed-tenant mismatch
  errors now point at `acu tenant recycle`.
- **Dual-reader offline path (T127–T131):** `acu inventory ARTIFACT` parses an
  SM203520 Settings **XML** ZIP (`manifest.xml` + table XML) or an
  `ac.exe export xml` folder into `inventory/` (`summary.yaml` +
  `tables/<Table>.yaml`). Offline — no REST, SSH, or password. Binary `.adb`
  is rejected. When `target.yaml` is present and the artifact reports a build,
  `erp` must match.
- **`acu reconcile`:** offline cross-check of `inventory/` against optional
  `config/` seed; writes `findings/` only (unmapped tables, REST gaps,
  rest-vs-snapshot deltas, custom columns). Never writes `config/` or mutates
  the tenant (V35/V36).
- **`snapshot_map.yaml` normalize (T132–T140, V38):** package (and optional
  data-repo) map goes beyond table→entity. Compare pad-trims string keys and
  fields; optional per-row `keys:` / `fields:` rename seed names → inventory
  columns (e.g. SubaccountCD→SubCD, UnitID→Unit); global `resolvers:` plus
  per-row `resolves:` map inv int FKs → CD via inventory indexes (Account+Sub
  first; ReasonCode/VendorClass/PostingClass/CashAccount/OrderType freight);
  global `enums:` plus per-row `enums:` fold REST labels → DAC codes
  (ReasonCode.Usage, Account Type/PostOption/Active, CreditTerms Due/Disc,
  bool Active on prefs/warehouse/sub, …). Decimal-looking values collapse
  trailing zeros (`0` vs `0.000000`) without mangling bare CDs like `000000`.
  v1 `{table, entity}` rows still load.
- Docs: README CLI map + dual-reader table (inventory vs extract vs state);
  [docs/demo-seed.md](docs/demo-seed.md) dual-reader section + snapshot_map;
  [docs/ac-exe.md](docs/ac-exe.md) export-xml + SM203520 Settings XML notes
  for the inventory input path.

### Changed

- **Default API pin from `target.yaml` (T125–T126):** `Instance.api_version`
  resolves as `--api-version` flag, else `target.yaml` `default_api`, else
  code default `25.200.001`. `ACU_API_VERSION` is no longer a config key
  (ignored if present); scaffolded `.env` omits it; `acu config show` never
  emits it. Dual-source match gate retired (source-merge). `config check`
  reports `api_version from default_api=…` instead of a mismatch fail.
- **`acu extract` layout hard-cut (T115–T120):** extract always writes under
  `config/{bootstrap,baseline,setup,master}/`. Root `bootstrap/`…`master/`
  emit is gone; there is no `--layout`. Re-extract into a modern data repo or
  move root trees under `config/` before the next apply.
- Packaged registry rename: `extract_manifest.yaml` becomes `seed_catalog.yaml`
  (package data; operators do not author it). Catalog completeness equals the
  packaged `templates/config/**` seed set (V34), including master and
  filter-split multi-file entities.
- Docs: README + [docs/demo-seed.md](docs/demo-seed.md) treat extract as the
  inverse of apply under `config/`; entity map mirrors the catalog; CLI help
  documents the hard-cut. Sole data-repo Default pin = `target.yaml`
  `default_api` (no operator `ACU_API_VERSION` pin).

### Fixed

- Seed packaging no longer claims `WeightUOM` / `VolumeUOM` fields the endpoint
  GET never returns (B26 class permanent red diff).

