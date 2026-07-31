# Changelog

## Unreleased

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

