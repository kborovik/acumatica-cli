# Distribution flavor (`acu config init --flavor distribution`)

Opt-in full virgin-tenant demo seed for inventory and distribution.

Default `acu config init` (no `--flavor`) stays finance-minimal so offline e2e stays green.

## Rebuild order

Start from a **brand-new empty tenant**.

Do not apply onto a half-configured company.

```sh
acu config init --flavor distribution --host erp.example.com my-dist
cd my-dist
# edit .env: ACU_PASSWORD, ACU_TENANT; keep ACU_API_VERSION aligned with target.yaml

acu config check
acu bootstrap                 # publishes Bootstrap + features from config/bootstrap/
acu apply config/             # config/{bootstrap,baseline,setup,master}/ fixed order
acu run scenario/             # 10-seed-capital → 20-buy-gateways → 30-build → 40-sell
acu diff config/              # expect exit 0 (no drift)
```

Bare `apply` / `diff` (no path args) also prefer `config/<name>/` when those trees exist (V30).

Finance-minimal repos keep root `bootstrap/`…`setup/` and have no `master/`.

## Once vs additive scenarios

| File | Class | Behavior on warm re-run |
| ---- | ----- | ----------------------- |
| `scenario/10-seed-capital.yaml` | once | `once: true` + `present` inquire on Owner Capital 30000 `EndingBalance gte 50000`; skip when already present |
| `scenario/20-buy-gateways.yaml` | additive | re-runs; per-leg delta expects |
| `scenario/30-build.yaml` | stub | empty steps (kit body later) |
| `scenario/40-sell.yaml` | additive | re-runs; per-leg delta expects |

Warm capital non-stack proof (live or e2e):

```sh
acu --tenant DEV run scenario/    # cold: capital posts 50k
acu --tenant DEV run scenario/    # warm: skip 10-seed-capital; capital stays 50000
# optional absolute check after second run:
# AccountSummaryInquiry Account=30000 EndingBalance must remain 50000 (not 100000)
```

Offline unit tests cover the once skip path (`tests/test_run.py::test_once_skip_when_present`).

Live virgin-tenant chain: `make e2e FILE=test_distribution_flavor`.

## Apply-order and dependency notes

Numbered file prefixes encode order within each directory (alphabetical expansion).

Cross-directory order is fixed: bootstrap → baseline → setup → master (umbrella expand under `config/`).

| Phase | Must exist before later files |
| ----- | ----------------------------- |
| Features | `config/bootstrap/features.yaml` enables Inventory, DistributionModule, Warehouse, WarehouseLocation, KitAssemblies, SubAccount, … |
| Company | Org CD **LAB5** (single placeholder across ledger link, open periods, TransitBranchID, cash BranchID) |
| COA / GL | Expanded accounts for inventory, in-transit, PO accrual, PPV/LCV, freight, discounts |
| Setup calendar | Financial year → master calendar → open periods for LAB5 |
| Master prefs | Reason codes and IN prefs before warehouse; warehouse before item classes and stock items |
| Parties | Vendor/customer classes before vendors/customers |
| Scenario | Runs only after master apply; capital once-guard then additive buy/sell |

Feature closure: every feature-gated form used by a seed file must appear in `config/bootstrap/features.yaml`.

Reference closure: every foreign key must resolve to a tenant-native row or an earlier-sorting seed file.

## Entity map

Paths are under `config/` for seed trees (distribution flavor).

| Seed file | Entity / action | Endpoint | Screen |
| -------- | --------------- | -------- | ------ |
| `config/bootstrap/company.yaml` | Company | bootstrap | CS101500 |
| `config/bootstrap/credit-terms.yaml` | CreditTerms | bootstrap | CS206500 |
| `config/baseline/10-subaccounts.yaml` | Subaccount | default | GL203000 |
| `config/baseline/20-accounts.yaml` | Account | default | GL202500 |
| `config/baseline/40-ledger.yaml` | Ledger | default | GL201500 |
| `config/baseline/50-gl-preferences.yaml` | GLPreferences | bootstrap | GL102000 |
| `config/baseline/60-ledger-company.yaml` | LedgerCompany | bootstrap | GL201500 |
| `config/baseline/90-uoms.yaml` | UnitsOfMeasure | default | CS203500 |
| `config/setup/10-financial-year.yaml` | FinancialYearSettings / GeneratePeriods | bootstrap | GL101000 |
| `config/setup/20-master-calendar.yaml` | MasterCalendar / GenerateCalendar | bootstrap | GL201000 |
| `config/setup/30-open-periods.yaml` | ManagePeriods / ProcessAll | bootstrap | GL503000 |
| `config/master/10-reason-codes.yaml` | ReasonCode | bootstrap | CS211000 |
| `config/master/20-in-preferences.yaml` | INPreferences | bootstrap | IN101000 |
| `config/master/30-availability-rules.yaml` | AvailabilityCalculationRule | bootstrap | IN201500 |
| `config/master/40-posting-classes.yaml` | PostingClass | bootstrap | IN206000 |
| `config/master/50-warehouse.yaml` | Warehouse | bootstrap | IN204000 |
| `config/master/51-warehouse-locations.yaml` | Warehouse (locations) | default | IN204000 |
| `config/master/52-warehouse-defaults.yaml` | Warehouse (receive/ship/RMA bins) | default | IN204000 |
| `config/master/53-tax-categories.yaml` | TaxCategory | default | TX205500 |
| `config/master/54-item-classes.yaml` | ItemClass | default | IN201000 |
| `config/master/56-so-preferences.yaml` | SOPreferences | bootstrap | SO101000 |
| `config/master/57-po-preferences.yaml` | POPreferences | bootstrap | PO101000 |
| `config/master/58-order-types.yaml` | OrderType | bootstrap | SO201000 |
| `config/master/60-ar-preferences.yaml` | ARPreferences | bootstrap | AR101000 |
| `config/master/61-ap-preferences.yaml` | APPreferences | bootstrap | AP101000 |
| `config/master/62-ca-preferences.yaml` | CAPreferences | bootstrap | CA101000 |
| `config/master/63-cash-account.yaml` | CashAccount | bootstrap | CA202000 |
| `config/master/64-payment-methods.yaml` | PaymentMethod | default | CA204000 |
| `config/master/65-statement-cycles.yaml` | StatementCycle | bootstrap | AR202800 |
| `config/master/70-vendor-classes.yaml` | VendorClass | bootstrap | AP201000 |
| `config/master/71-customer-classes.yaml` | CustomerClass | default | AR201000 |
| `config/master/75-vendors.yaml` | Vendor | default | AP303000 |
| `config/master/76-customers.yaml` | Customer | default | AR303000 |
| `config/master/80-stock-items-parts.yaml` | StockItem | default | IN202500 |
| `config/master/82-stock-items-kits.yaml` | StockItem | default | IN202500 |
| `config/master/85-kit-specifications.yaml` | KitSpecification | default | IN209500 |
| `scenario/10-seed-capital.yaml` | JournalTransaction (once) | default | GL301000 |
| `scenario/20-buy-gateways.yaml` | PO / receipt / AP bill+pay | default | PO/IN/AP |
| `scenario/30-build.yaml` | (stub) | — | — |
| `scenario/40-sell.yaml` | SO / ship / invoice / AR pay | default | SO/IN/AR |

`endpoint: bootstrap` resolves to the active Bootstrap package version from `config/bootstrap/project.xml` (or root `bootstrap/project.xml` on finance-minimal).

`endpoint: default` (or omitted on Default-only entities) resolves to `Default/<ACU_API_VERSION>`.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover or opening balances from a legacy system
- Replacing external data repos for production cutovers

## Changelog note (release)

When shipping the version that lands config/ umbrella + once-guard (gh #19):

- Call out `config/` seed layout for `--flavor distribution`.
- Call out lifecycle scenarios and once-guard skip for capital.
- Call out bare `apply` / `diff` prefer `config/` when present; umbrella `acu apply config/`.
