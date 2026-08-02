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

`apply` continues after a failed record (V45): later records and files still
run; exit 1 with a multi-error summary when any PUT failed (never silent
partial; exit 2 stays with `diff`). Field-level 422 detail surfaces in the
error text (V46).

### Multi-host trunk + overlays (V44)

Keep one trunk seed; pin each host with `target.yaml`; put version-specific
rewrites under a data-repo overlay directory and pass it after trunk:

```sh
acu apply config/ overlays/default-24.200.001/
```

No long-running release branches for version fan-out. No `apply --overlay`
flag (data-repo path args are enough). Cross-link:
[acumatica-gitops#2](https://github.com/kborovik/acumatica-gitops/issues/2).

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

### Period token (`${current_period}`) vs pinned views

| Surface | Period handling |
| ------- | --------------- |
| `acu run` scenario YAML | Built-in `${current_period}` → host-local `MMyyyy` at process start (V43). Expands on steps, `expect` params, and `once.present` params. Package lifecycle scenarios use the token so month roll does not require hand-editing inquire Period. |
| `acu state` / `config/views/` | **Pinned literals only.** No `${current_period}` expand (V33). Package TB view keeps a fixed Period for e2e/mock alignment and reproducible committed `state/` rows. Operators hand-set Period when capturing the live current-month window. |

Why the split: scenario expects measure the period transactions just posted (calendar-relative). Committed observations under `state/` are git evidence — expanding a token at capture time would rewrite files every month without a real ERP change.

**ERP business-date skew:** the token uses the **host** local date, not an ERP business-date probe and not a `--as-of` / `--period` flag (v1). If the ERP business date differs from the host calendar, set Period explicitly in the scenario or align the host date.

```yaml
# scenario expect / once.present (calendar-relative)
parameters: {Ledger: ACTUAL, Period: "${current_period}"}

# config/views (pinned for reproducible state/)
params: {Ledger: ACTUAL, Period: "082026"}
```

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

`reconcile` never writes `config/` (V36): unmapped tables, REST gaps, rest-vs-snapshot field deltas, and custom `Usr*` columns land under `findings/` only. When `target.yaml` is present and the artifact reports a build, `inventory` requires `erp` match (sibling of V27). `config init` does not scaffold `inventory/` or `findings/` — engagement-generated (gitignored by the scaffolded `.gitignore`).

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

### Inventory table → entity → endpoint (V42)

Package `snapshot_map.yaml` (and identity match when table name equals catalog
entity). Endpoint column = catalog symbolic contract for the entity (not the
inventory transport — inventory is always offline XML).

| Inventory table | Entity | Endpoint | Notes |
| --------------- | ------ | -------- | ----- |
| `Account` | Account | default | Type/PostOption/Active enums |
| `Sub` | Subaccount | default | `SubaccountCD`→`SubCD` |
| `Terms` | CreditTerms | bootstrap | Due/Disc/Visible enums |
| `UnitOfMeasure` | UnitsOfMeasure | default | `UnitID`→`Unit` |
| `OrganizationLedgerLink` | LedgerCompany | bootstrap | |
| `GLSetup` / `INSetup` / `APSetup` / `ARSetup` / `SOSetup` / `POSetup` / `CASetup` | module Preferences | bootstrap | bool_bit policy bits |
| `ReasonCode` | ReasonCode | bootstrap | Usage enum + Account/Sub FK |
| `INPostClass` | PostingClass | bootstrap | *Acct/*Sub FK resolve |
| `INAvailabilityScheme` | AvailabilityCalculationRule | bootstrap | |
| `INItemClass` | ItemClass | default | `ClassID`→`ItemClassCD`; ItemType/ValMethod |
| `INSite` | Warehouse | bootstrap \| default | `WarehouseID`→`SiteCD` |
| `INLocation` | Warehouse | bootstrap \| default | bin rows; detail lists not field-compared |
| `InventoryItem` | StockItem | default | `InventoryID`→`InventoryCD`; ItemStatus |
| `INKitSpecHdr` | KitSpecification | default | |
| `SOOrderType` | OrderType | bootstrap | Freight FK |
| `ARStatementCycle` | StatementCycle | bootstrap | PrepareOn |
| `CashAccount` | CashAccount | bootstrap | Account/Sub/Branch FK |
| `PaymentMethod` | PaymentMethod | default | Descr/IsActive/UseFor*/PaymentType |
| `PaymentMethodAccount` | PaymentMethod | default | detail cash accounts |
| `VendorClass` | VendorClass | bootstrap | *Acct/*Sub FK |
| `CustomerClass` | CustomerClass | default | `ClassID`→`CustomerClassID` + AR/Sales FK |
| `Vendor` | Vendor | default | identity; CD on `BAccount` (v1 no multi-table join) |
| `Customer` | Customer | default | identity; CD on `BAccount` (v1 no multi-table join) |
| `Roles` / `Users` / `UsersInRoles` | Role / User | bootstrap | membership detail |
| `NumberingSequence` / `Numbering` | NumberingSequence | bootstrap | bounds only (V40) |
| `Company` / `Ledger` / `TaxCategory` | (identity) | bootstrap or default | table name = entity; no map row required |

#### Intentionally unmapped (findings noise, not CaC gaps)

| Class | Examples |
| ----- | -------- |
| Txn / history | `INTran*`, `GLTran`, `APInvoice`, `ARPayment`, `Batch`, `PO*`, `SO*`, cost/status hist |
| System / audit | `Note`, `LoginTrace`, `ScreenPreferences` (when present) |
| Features via plugin | `FeaturesSet` (bits applied by Bootstrap plugin, not seed table) |
| Party CD host | `BAccount` — natural CD for Vendor/Customer; v1 maps extension tables only |
| Party address/contact | `Location`, `Contact`, `Address` (linked entities, not flat seed headers) |
| Lot/serial | `INLotSerClass` — demo non-goal (see [LotSerialClass](#lotserialclass--non-goal-gh-27--v42)) |
| Kit/detail noise | `INKitSpecStkDet`, `INUnit`, site-status / cost tables |

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
| Numbering | `05-numbering-sequences.yaml` before any prefs that may set `*NumberingID` — see [Numbering sequences](#numbering-sequences) |
| Master prefs | Reason codes and IN prefs before warehouse; warehouse before item classes and stock items |
| Parties | Vendor/customer classes before vendors/customers |
| Roles / users | `90-roles.yaml` then `91-users.yaml` (Role before User; membership rides User detail) — see [Role, User, and password seed](#role-user-and-password-seed) |
| Scenario | Runs only after master apply; capital once-guard then additive buy/build/sell |

Feature closure: every feature-gated form used by a seed file must appear in `config/bootstrap/features.yaml`.

Reference closure: every foreign key must resolve to a tenant-native row or an earlier-sorting seed file.

## Role, User, and password seed

Default contract has **no** Role or User surface. Both live on the **Bootstrap**
endpoint only (`endpoint: bootstrap` → active package version, currently
`Bootstrap/1.4.0`). Screens: Role = SM201005, User = SM201010.

### Apply order (V22)

Numbered prefixes under `config/master/` enforce **Role → User → membership**:

| File | Entity | Notes |
| ---- | ------ | ----- |
| `config/master/90-roles.yaml` | Role | key `Rolename`; fields `Rolename`, `Descr` |
| `config/master/91-users.yaml` | User | key `Username`; identity fields + detail `Roles` |

Membership is **not** a separate seed file. It is the User detail list
`Roles` (`detail_keys: { Roles: Rolename }`), each row `Rolename` +
`Selected`. A role must exist (tenant-native or earlier Role seed) before a
User PUT that selects it.

Package demo: full pre-build role dump in `90-roles.yaml` + demo user
`soadmin` with `SO Admin` selected in `91-users.yaml`. Built-in system roles
(Administrator, Customizer, …) are present as identity/header rows for
reference closure; access-rights matrix beyond role header is out of scope.

### User Roles membership limit (T189)

Live lab (Bootstrap contract, cold PUT): **User detail `Roles` membership is
not durable** — a successful User PUT with `Roles: [{Rolename, Selected:
true}]` still answers later GET with `Roles: []`. Identity fields (Username,
names, flags) round-trip; membership does not.

| Path | Practical rule |
| ---- | -------------- |
| **apply** | Identity seed is reliable. Membership rows may no-op on the server. |
| **diff** | Expect possible permanent `Roles[…]: missing on tenant` when seed claims membership. |
| **Data repos** | Prefer identity-only User seed (e.g. soadmin/apadmin/aradmin without `Roles`); assign roles in UI or accept drift until a contract fix. |
| **Package template** | Still ships sparse `soadmin` + `SO Admin` membership as the offline contract shape (T148); not a live membership guarantee. |

Not a password issue (V39). Not fixed by re-apply order. Track contract/screen
gaps via V14 when re-verified on a new Bootstrap version.

### Password rule (V39)

| Path | Behavior |
| ---- | -------- |
| **apply** | `Password` is **write-only** when present in seed YAML. Virgin create (no live User) sends it; **warm** re-apply (User already live) **strips** password fields so identity re-PUT does not reset passwords. Package User seed needs no password. |
| **extract** | Always strips `Password` and `b64__Password` — hashes never enter seed (even if a catalog `include` mistakenly listed them). |
| **diff** | Ignores password fields — seed with or without `Password` vs live hash is never drift. |

```yaml
# config/master/91-users.yaml (shape)
entity: User
key: Username
endpoint: bootstrap
detail_keys:
  Roles: Rolename
records:
- Username: soadmin
  FirstName: SO
  LastName: Admin
  Email: soadmin@example.com
  IsApproved: true
  PasswordNeverExpires: true
  PasswordChangeOnNextLogin: false
  # Password: optional; write-only on virgin create only
  Roles:
  - Rolename: SO Admin
    Selected: true
```

Authoring a first-login password: put `Password` on the User record for the
initial apply only. After that, omit it — warm re-apply and extract stay
identity-only. Never commit password hashes from SM203520 / inventory dumps.

## Numbering sequences

Default contract has **no** Numbering Sequences surface. Sequences live on the
**Bootstrap** endpoint only (`endpoint: bootstrap` → active package version,
currently `Bootstrap/1.4.0`). Screen: CS201010 (Numbering Sequences).

### Apply order (V22)

Numbered prefix under `config/master/` places sequences **before** every module
prefs file that may reference a sequence id via `*NumberingID` (IN/SO/PO/AR/AP/CA
setup and kin):

| File | Entity | Notes |
| ---- | ------ | ----- |
| `config/master/05-numbering-sequences.yaml` | NumberingSequence | key `NumberingID`; bounds only |
| later `20-in-preferences.yaml`, `56-so-preferences.yaml`, … | module prefs | may point `*NumberingID` at a sequence id |

Package demo pins LAB5-class module sequences (`APBILL`, `APPAYMENT`,
`ARINVOICE`, `ARPAYMENT`, `BATCH`, `INADJUST`, `INISSUE`, `INKITASSY`,
`INRECEIPT`, `POORDER`, `PORECEIPT`, `SOORDER`, `SOSHIPMENT`) as bounds-only
rows. A custom sequence id used by a prefs seed must exist (tenant-native or
earlier NumberingSequence seed) before that prefs PUT.

### Bounds vs runtime (V40)

Seed is **bounds only**. Issued progress is runtime state and never desired
config:

| Field class | Fields | Seed? |
| ----------- | ------ | ----- |
| Identity + bounds | `NumberingID`, `StartNbr`, `EndNbr`, `WarnNbr`, `NbrStep`, `StartDate`?, `Descr`? | yes |
| Runtime counter | `LastNbr` (+ advanced counter if ever exposed) | **never** |

| Path | Behavior |
| ---- | -------- |
| **apply** | PUT bounds only. `LastNbr` is never sent — even if hand-authored seed includes it — so re-apply does not reset live counters. |
| **extract** | Always strips `LastNbr` (catalog include is bounds-only; hard strip even if include mistakenly lists it). |
| **diff** | Ignores `LastNbr` — seed bounds vs live counter is never drift. |

```yaml
# config/master/05-numbering-sequences.yaml (shape)
entity: NumberingSequence
key: NumberingID
endpoint: bootstrap
records:
- NumberingID: BATCH
  StartNbr: '000000'
  EndNbr: '999999'
  WarnNbr: '999990'
  NbrStep: 1
  StartDate: '1900-01-01'
  # LastNbr: never seed — runtime issued progress (V40)
```

Do not author `LastNbr` in seed. Scenario documents that advance counters are
out of scope for apply; warm re-apply of bounds must leave live `LastNbr`
alone.

## Module preferences field depth (V41)

Bootstrap `*Preferences` entities are a **curated subset** of inventory
`*Setup` tables — not a full DAC mirror (gh #26). Each seed/catalog field has
a reason: demo need or rebuild risk when virgin ERP defaults drift across
builds. Server-derived and runtime fields stay out (B11 class).

Active package: `Bootstrap/1.4.0` (shape change = version bump, V21).

### Added fields (beyond pre-1.3.0 surface)

| Entity | Screen | Added fields | Reason |
| ------ | ------ | ------------ | ------ |
| GLPreferences | GL102000 | `BatchNumberingID`, `AutoPostOption`, `HoldEntry`, `RequireControlTotal` | pin BATCH + post/hold policy vs virgin defaults |
| INPreferences | IN101000 | `BatchNumberingID`, `ReceiptNumberingID`, `IssueNumberingID`, `AdjustmentNumberingID`, `KitAssemblyNumberingID`, `AutoPost`, `SummPost`, `NegQty`, `RequireControlTotal` | LAB5 numbering + post/qty/control policy |
| APPreferences | AP101000 | `BatchNumberingID`, `InvoiceNumberingID`, `CheckNumberingID`, `AutoPost`, `RequireControlTotal`, `RequireVendorRef`, `RequireApprovePayments` | bill/payment sequences + vendor-ref scenario needs |
| ARPreferences | AR101000 | `BatchNumberingID`, `InvoiceNumberingID`, `PaymentNumberingID`, `AutoPost`, `RequireControlTotal`, `RequireExtRef`, `CreditCheckError` | invoice/payment sequences + credit policy |
| SOPreferences | SO101000 | `ShipmentNumberingID`, `CreditCheckError` | SOSHIPMENT pin + credit check |
| POPreferences | PO101000 | `RegularPONumberingID`, `ReceiptNumberingID`, `AutoReleaseAP` | POORDER/PORECEIPT pin; AP auto-release off |
| CAPreferences | CA101000 | `BatchNumberingID`, `AutoPostOption`, `ReleaseAP`, `ReleaseAR` | batch + cash release policy |

Pre-existing surfaces kept: GL accounts; IN transit/progress/reason codes /
`UpdateGL` / `HoldEntry`; SO default order type / hold-shipment / auto-release
IN; PO hold receipts / return reason / auto-release IN; CA transit account/sub
/ `HoldEntry`.

### Explicitly not seeded (V41)

| Skip class | Examples | Why |
| ---------- | -------- | --- |
| Apply-order FKs | IN `DfltLotSerClassID`, `TransitSiteID`, `DfltPostClassID`; AP/AR default class | Prefer files sort **before** warehouse / lot class / posting class / vendor-customer class |
| Unsequenced numbering | CA `TransferNumberingID` / `RegisterNumberingID`; IN `PINumberingID` | Sequence ids not in package `05-numbering-sequences.yaml` |
| Full-DAC noise | aging buckets, retainage, print flags, assignment maps | Not demo-critical; rebuild risk low vs maintenance cost |

### Apply order vs numbering / warehouse / lot class

| Step | Must exist first |
| ---- | ---------------- |
| `05-numbering-sequences.yaml` | every prefs `*NumberingID` claim |
| `10-reason-codes.yaml` | IN/PO reason-code FKs |
| `20-in-preferences.yaml` | before warehouse (IN prefs do **not** claim site/lot/posting defaults) |
| warehouse / posting class / item class | stock items — not prefs FKs this pass |

If you later seed `DfltLotSerClassID` or `TransitSiteID`, renumber master
prefixes so lot class / warehouse sort **before** IN preferences.

### LotSerialClass — non-goal (gh #27 / V42)

Demo does **not** seed lot/serial classes and does **not** claim
`DfltLotSerClassID` on IN preferences (V41 apply-order skip stands).

| Surface | Status |
| ------- | ------ |
| `seed_catalog.yaml` row for Default `LotSerialClass` | **not packaged** |
| Package template under `config/master/` | **none** |
| Inventory table `INLotSerClass` | intentionally **unmapped** in `snapshot_map` (system `DEFAULT` only on LAB5-class) |
| Bootstrap bump for lot/serial | **out of scope** this pass |

When a data repo later seeds lot/serial classes, add a catalog row + numbered
master template **before** any IN prefs FK claim, map `INLotSerClass` →
`LotSerialClass`, and re-check V34 completeness.

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
| `config/master/05-numbering-sequences.yaml` | NumberingSequence | bootstrap | CS201010 | catalog |
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
| `config/master/90-roles.yaml` | Role | bootstrap | SM201005 | catalog |
| `config/master/91-users.yaml` | User (+ Roles membership detail) | bootstrap | SM201010 | catalog |
| `scenario/10-seed-capital.yaml` | JournalTransaction (once) | default | GL301000 | run only |
| `scenario/20-buy.yaml` | PO / receipt / AP bill+pay | default | PO/IN/AP | run only |
| `scenario/30-build.yaml` | KitAssembly | default | IN307000 | run only |
| `scenario/40-sell.yaml` | SO / ship / invoice / AR pay | default | SO/IN/AR | run only |

`endpoint: bootstrap` resolves to the active Bootstrap package version from the packaged CLI contract (`bootstrap_project.xml` — package SoT). Data-repo `project.xml` is not a seed; remove it if present.

`endpoint: default` (or omitted on Default-only entities) resolves to `Default/<api_version>` where `api_version` comes from `--api-version`, else `target.yaml` `default_api`, else the code default.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover or opening balances from a legacy system
- Replacing external data repos for production cutovers
- **LotSerialClass** seed/catalog (demo never claims `DfltLotSerClassID`; see [LotSerialClass — non-goal](#lotserialclass--non-goal-gh-27--v42))
