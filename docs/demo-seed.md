# Demo seed (`acu config init`)

Single full virgin-tenant demo seed for finance + inventory/distribution
(lab5 Demo Tenant Factory class). No `--flavor` — one packaged template set
under `config/` + lifecycle `scenario/`.

## Rebuild order

Start from a **brand-new empty tenant**.

Do not apply onto a half-configured company.

```sh
acu config init --host erp.example.com my-erp
cd my-erp
# edit .env: ACU_PASSWORD, ACU_TENANT (Default API pin = target.yaml default_api)

acu config check
acu bootstrap                 # publishes Bootstrap + features from config/bootstrap/
acu apply config/             # config/{bootstrap,baseline,setup,master}/ fixed order
acu run scenario/             # 10-seed-capital → 20-buy → 30-build → 40-sell
acu diff config/              # expect exit 0 (no drift)
acu state                     # write state/ from config/views/ (EndingBalance TB)
# warm gate: once-capital only — additive buy/sell would move numeric observations
acu run scenario/10-seed-capital.yaml && acu state --assert-unchanged
```

Bare `apply` / `diff` (no path args) also prefer `config/<name>/` when those trees exist (V30).

Legacy data repos may still use root `bootstrap/`…`master/`; init no longer scaffolds that layout.
`acu extract` never writes that root layout — emit is hard-cut to `config/` only (see [Extract](#extract)).

## Extract

`acu extract` is the **inverse of apply** for seed trees: read the live tenant, write seed YAML under `config/{bootstrap,baseline,setup,master}/` that `apply` / `diff` consume unchanged.

| Concern | Rule |
| ------- | ---- |
| Registry | Packaged `seed_catalog.yaml` (not operator-authored); every catalog `file:` path is under `config/` |
| Features | Synthesized `config/bootstrap/features.yaml` from catalog `features:` gates (not a catalog entity row) |
| Setup | Catalog `kind:` synthesizers rebuild action files (not raw record dumps) |
| Multi-file same entity | Filter-split or include-partition (one catalog row per numbered file) |
| Skip / fail | Exists: skip unless `--force`; zero records: skip; row fail: report and continue; exit 1 if any fail |
| Not seed | `scenario/` and `config/views/` are never extract targets |

```sh
acu extract --out . --force          # full catalog → config/**
acu extract --only StockItem --force # catalog rows matching entity or file stem
acu apply config/ && acu diff config/  # round-trip: expect clean diff after apply
```

**Migration (root emit to `config/`):** older extract wrote `bootstrap/`…`master/` at the data-repo root. That path is gone (no `--layout`). Re-extract into a `config/` data repo, or move root trees under `config/` before the next apply. Package data renamed `extract_manifest.yaml` becomes `seed_catalog.yaml`.

## State observations

`acu state` captures **derived state** (not seed config):

| Artifact | Path | Role |
| -------- | ---- | ---- |
| View defs | `config/views/*.yaml` | Observer config (`inquire:` / `entity:` / `gi:`, keys, capture allowlist; not SEED_DIRS) |
| Observations | `state/*.yaml` | Committed evidence; flow-style one row per line; money/qty = fixed-point strings |

Unlike `extract` / `diff`, `state` never writes seed trees, never carries `endpoint:` symbols, and never participates in apply. Exit codes invert `diff`'s default: bare capture treats change as normal (exit 0); exit 2 only under `--assert-unchanged` when state moved.

**Migration (T112–T114):** CLI verb is `state` only (no `snapshot` alias). Bare defaults hard-cut to `config/views/` and `state/`. Prior `config/snapshot/` is not a fallback — move views or pass explicit paths.

## Dual readers: inventory vs extract vs state

Three different products — same tenant, different jobs (V35 dual-reader single-writer):

| | `extract` | `inventory` | `state` |
| - | --------- | ----------- | ------- |
| Needs live REST | yes | **no** (offline) | yes |
| Input | contract entities (catalog) | SM203520 Settings XML ZIP or `ac.exe export xml` folder | `config/views/*.yaml` |
| Output | seed under `config/**` | `inventory/summary.yaml` + `tables/<Table>.yaml` | `state/<view>.yaml` |
| Shape | seed (`entity` / `key` / `records` / `endpoint:`) | raw DAC tables (columns + rows) | observations (keys + capture cols) |
| Apply path? | yes — inverse of `apply` | **never** | **never** |
| Who mutates tenant | — | — | — |

Sole tenant mutator remains `apply` (keyed PUT). Snapshot restore / `ac.exe import` / binary `.adb` are out of scope.

```sh
# Live seed inverse (REST)
acu --tenant DEV extract --out . --force

# Offline full-table read (artifact from SM203520 Settings export XML or ac.exe)
acu inventory ./tenant-export.xml.zip          # or a folder from: ac.exe export xml …
acu inventory --dry-run ./company2-export/     # would-write only
acu reconcile                                  # inventory/ + config/ → findings/
acu reconcile --inventory inv/ --config config/ --out findings/

# Derived balances (REST observers; not inventory)
acu --tenant DEV state
```

`reconcile` never writes `config/` (V36): unmapped tables, REST gaps, rest-vs-snapshot field deltas, and custom `Usr*` columns land under `findings/` only. When `target.yaml` is present and the artifact reports a build, `inventory` requires `erp` match (sibling of V27). `config init` does not scaffold `inventory/` or `findings/` — engagement-generated.

### `snapshot_map.yaml` (table→entity + normalize)

Join is always **table↔entity** (never seed filename↔inventory filename). Load
order: data-repo root `snapshot_map.yaml` if present, else package defaults,
else empty → identity match on catalog entity name. v1 rows stay
`{table, entity}` only.

Beyond renames, the map (and package defaults) support V38 normalize:

| Layer | YAML | Effect |
| ----- | ---- | ------ |
| Pad-trim | (always on) | string keys + fields strip both sides so NVarChar padding joins |
| Key/field aliases | per-row `keys:` / `fields:` | seed name → inventory column (e.g. `SubaccountCD: SubCD`, `UnitID: Unit`, `Description: Descr`) |
| FK CD resolve | global `resolvers:` + per-row `resolves:` | inv int id → CD via inventory index (prefer compare in seed CD space); package ships Account+Sub (+ Branch) for ReasonCode/VendorClass/PostingClass/CashAccount/OrderType freight |
| Enum labels | global `enums:` + per-row `enums:` | REST label → DAC code (e.g. `Usage: reason_usage`, `Active: bool_bit`, Account `Type`/`PostOption`) |

```yaml
# snapshot_map.yaml (excerpt — package ships defaults)
resolvers:
  account_cd: { table: Account, id: AccountID, cd: AccountCD }
  sub_cd:     { table: Sub,     id: SubID,     cd: SubCD }

enums:
  reason_usage: { Adjustment: A, Issue: I, Receipt: R }
  bool_bit: { "true": "1", "false": "0" }

tables:
  - table: Sub
    entity: Subaccount
    keys: { SubaccountCD: SubCD }
    enums: { Active: bool_bit }
  - table: UnitOfMeasure
    entity: UnitsOfMeasure
    keys: { UnitID: Unit }
    fields: { Description: Descr }
  - table: ReasonCode
    entity: ReasonCode
    resolves: { AccountID: account_cd, SubID: sub_cd }
    enums: { Usage: reason_usage }
```

Without aliases, renamed DACs clear **unmapped** but skip row compare (key
names miss). Without resolvers, ReasonCode/VendorClass report false
CD-vs-int deltas. Without enums, Usage/Account Type/Active report false
label-vs-code deltas. Findings report seed-side field names and resolved
inventory values (codes after enum fold).

Artifact notes and the SM203520 / `ac.exe export xml` distinction: [ac-exe.md](ac-exe.md).

Default scaffold golden is **trial-balance only** (V28/V33):

| Stem | Source | Capture (must be numeric money) |
| ---- | ------ | ------------------------------- |
| `trial-balance` | `inquire: AccountSummaryInquiry` | `EndingBalance` (+ balance columns) |

Roster-only `entity: Account` is forbidden for that stem.
`inventory-summary` / `QtyOnHand` is not packaged this pass (B25:
`InventorySummaryInquiry` warehouse-only yields empty Results). Custom views via
`inquire:` or `gi:` remain supported when a V12-verified path is known.
`gi:` stays optional when a GenericInquiry is V12-verified: enable **Expose via
OData** on SM208000, seed the GI under `config/master/`, point `source.gi:` with
params pinned in YAML (`$metadata` fail-closed). See [rest-api.md](rest-api.md)
state view sources.

Warm `--assert-unchanged` after a full `run scenario/` fails on purpose when
buy/sell re-stack cash on the TB — re-run only once-class capital (skip path)
or re-capture without mutation.

## Once vs additive scenarios

| File | Class | Behavior on warm re-run |
| ---- | ----- | ----------------------- |
| `scenario/10-seed-capital.yaml` | once | `once: true` + `present` inquire on Owner Capital 30000 `EndingBalance gte 50000`; skip when already present |
| `scenario/20-buy.yaml` | additive | re-runs; per-leg delta expects |
| `scenario/30-build.yaml` | additive | kit assembly; re-runs |
| `scenario/40-sell.yaml` | additive | re-runs; per-leg delta expects |

Warm capital non-stack proof (live or e2e):

```sh
acu --tenant DEV run scenario/    # cold: capital posts 50k
acu --tenant DEV run scenario/    # warm: skip 10-seed-capital; capital stays 50000
# optional absolute check after second run:
# AccountSummaryInquiry Account=30000 EndingBalance must remain 50000 (not 100000)
```

Offline unit tests cover the once skip path (`tests/test_run.py::test_once_skip_when_present`).

Live virgin-tenant scenario + state chain: `make e2e FILE=test_scenario_lifecycle`.

## Apply-order and dependency notes

Numbered file prefixes encode order within each directory (alphabetical expansion).

Cross-directory order is fixed: bootstrap, then baseline, then setup, then master (umbrella expand under `config/`).

| Phase | Must exist before later files |
| ----- | ----------------------------- |
| Features | `config/bootstrap/features.yaml` enables Inventory, DistributionModule, Warehouse, WarehouseLocation, KitAssemblies, SubAccount, … |
| Company | Org CD **LAB5** (single placeholder across ledger link, open periods, TransitBranchID, cash BranchID) |
| COA / GL | Expanded accounts for inventory, in-transit, PO accrual, PPV/LCV, freight, discounts |
| Setup calendar | Financial year, then master calendar, then open periods for LAB5 |
| Master prefs | Reason codes and IN prefs before warehouse; warehouse before item classes and stock items |
| Parties | Vendor/customer classes before vendors/customers |
| Scenario | Runs only after master apply; capital once-guard then additive buy/build/sell |

Feature closure: every feature-gated form used by a seed file must appear in `config/bootstrap/features.yaml`.

Reference closure: every foreign key must resolve to a tenant-native row or an earlier-sorting seed file.

## Entity map

**Catalog mirror:** seed-file paths and entity/endpoint pairs below match packaged
`seed_catalog.yaml` (templates ↔ catalog ↔ extract emit; V34). Screen IDs are
operator notes only (not catalog fields). `scenario/` rows are `acu run` only —
not in the catalog, not extracted.

| Seed file | Entity / action | Endpoint | Screen | Extract |
| -------- | --------------- | -------- | ------ | ------- |
| `config/bootstrap/company.yaml` | Company | bootstrap | CS101500 | catalog |
| `config/bootstrap/credit-terms.yaml` | CreditTerms | bootstrap | CS206500 | catalog |
| `config/bootstrap/features.yaml` | FeaturesSet gates (synthesized) | — | CS100000 | synth |
| `config/baseline/10-subaccounts.yaml` | Subaccount | default | GL203000 | catalog |
| `config/baseline/20-accounts.yaml` | Account | default | GL202500 | catalog |
| `config/baseline/40-ledger.yaml` | Ledger | default | GL201500 | catalog |
| `config/baseline/50-gl-preferences.yaml` | GLPreferences | bootstrap | GL102000 | catalog |
| `config/baseline/60-ledger-company.yaml` | LedgerCompany | bootstrap | GL201500 | catalog |
| `config/baseline/90-uoms.yaml` | UnitsOfMeasure | default | CS203500 | catalog |
| `config/setup/10-financial-year.yaml` | FinancialYearSettings / GeneratePeriods | bootstrap | GL101000 | setup synth |
| `config/setup/20-master-calendar.yaml` | MasterCalendar / GenerateCalendar | bootstrap | GL201000 | setup synth |
| `config/setup/30-open-periods.yaml` | ManagePeriods / ProcessAll | bootstrap | GL503000 | setup synth |
| `config/master/10-reason-codes.yaml` | ReasonCode | bootstrap | CS211000 | catalog |
| `config/master/20-in-preferences.yaml` | INPreferences | bootstrap | IN101000 | catalog |
| `config/master/30-availability-rules.yaml` | AvailabilityCalculationRule | bootstrap | IN201500 | catalog |
| `config/master/40-posting-classes.yaml` | PostingClass | bootstrap | IN206000 | catalog |
| `config/master/50-warehouse.yaml` | Warehouse | bootstrap | IN204000 | catalog |
| `config/master/51-warehouse-locations.yaml` | Warehouse (locations) | default | IN204000 | catalog |
| `config/master/52-warehouse-defaults.yaml` | Warehouse (receive/ship/RMA bins) | default | IN204000 | catalog |
| `config/master/53-tax-categories.yaml` | TaxCategory | default | TX205500 | catalog |
| `config/master/54-item-classes.yaml` | ItemClass | default | IN201000 | catalog |
| `config/master/56-so-preferences.yaml` | SOPreferences | bootstrap | SO101000 | catalog |
| `config/master/57-po-preferences.yaml` | POPreferences | bootstrap | PO101000 | catalog |
| `config/master/58-order-types.yaml` | OrderType | bootstrap | SO201000 | catalog |
| `config/master/60-ar-preferences.yaml` | ARPreferences | bootstrap | AR101000 | catalog |
| `config/master/61-ap-preferences.yaml` | APPreferences | bootstrap | AP101000 | catalog |
| `config/master/62-ca-preferences.yaml` | CAPreferences | bootstrap | CA101000 | catalog |
| `config/master/63-cash-account.yaml` | CashAccount | bootstrap | CA202000 | catalog |
| `config/master/64-payment-methods.yaml` | PaymentMethod | default | CA204000 | catalog |
| `config/master/65-statement-cycles.yaml` | StatementCycle | bootstrap | AR202800 | catalog |
| `config/master/70-vendor-classes.yaml` | VendorClass | bootstrap | AP201000 | catalog |
| `config/master/71-customer-classes.yaml` | CustomerClass | default | AR201000 | catalog |
| `config/master/75-vendors.yaml` | Vendor | default | AP303000 | catalog |
| `config/master/76-customers.yaml` | Customer | default | AR303000 | catalog |
| `config/master/80-stock-items-parts.yaml` | StockItem | default | IN202500 | catalog |
| `config/master/82-stock-items-kits.yaml` | StockItem | default | IN202500 | catalog |
| `config/master/85-kit-specifications.yaml` | KitSpecification | default | IN209500 | catalog |
| `scenario/10-seed-capital.yaml` | JournalTransaction (once) | default | GL301000 | run only |
| `scenario/20-buy.yaml` | PO / receipt / AP bill+pay | default | PO/IN/AP | run only |
| `scenario/30-build.yaml` | KitAssembly | default | IN307000 | run only |
| `scenario/40-sell.yaml` | SO / ship / invoice / AR pay | default | SO/IN/AR | run only |

`endpoint: bootstrap` resolves to the active Bootstrap package version from `config/bootstrap/project.xml` (or root `bootstrap/project.xml` on legacy layout).

`endpoint: default` (or omitted on Default-only entities) resolves to `Default/<api_version>` where `api_version` comes from `--api-version`, else `target.yaml` `default_api`, else the code default.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover or opening balances from a legacy system
- Replacing external data repos for production cutovers
