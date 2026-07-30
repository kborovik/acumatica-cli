# Changelog

## Unreleased

### Added

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
