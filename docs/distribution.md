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
acu bootstrap                 # publishes Bootstrap + features from bootstrap/
acu apply                     # bootstrap/ → baseline/ → setup/ → master/
acu run scenario/             # golden buy/receive → sell/ship/invoice/pay
acu diff                      # expect exit 0 (no drift)
```

`apply` and `diff` with no path args use existing dirs in that order.
Finance-minimal repos have no `master/`; the dir is skipped when absent.

## Apply-order and dependency notes

Numbered file prefixes encode order within each directory (alphabetical expansion).

Cross-directory order is fixed: bootstrap → baseline → setup → master.

| Phase | Must exist before later files |
| ----- | ----------------------------- |
| Features | `bootstrap/features.yaml` enables Inventory, DistributionModule, Warehouse, WarehouseLocation, KitAssemblies, SubAccount, … |
| Company | Org CD **LAB5** (single placeholder across ledger link, open periods, TransitBranchID, cash BranchID) |
| COA / GL | Expanded accounts for inventory, in-transit, PO accrual, PPV/LCV, freight, discounts |
| Setup calendar | Financial year → master calendar → open periods for LAB5 |
| Master prefs | Reason codes and IN prefs before warehouse; warehouse before item classes and stock items |
| Parties | Vendor/customer classes before vendors/customers |
| Scenario | Runs only after master apply; expects warm-tenant re-runnable deltas |

Feature closure: every feature-gated form used by a seed file must appear in `bootstrap/features.yaml`.

Reference closure: every foreign key must resolve to a tenant-native row or an earlier-sorting seed file.

## Entity map

| Seed file | Entity / action | Endpoint | Screen |
| -------- | --------------- | -------- | ------ |
| `bootstrap/company.yaml` | Company | bootstrap | CS101500 |
| `bootstrap/credit-terms.yaml` | CreditTerms | bootstrap | CS206500 |
| `baseline/10-subaccounts.yaml` | Subaccount | default | GL203000 |
| `baseline/20-accounts.yaml` | Account | default | GL202500 |
| `baseline/40-ledger.yaml` | Ledger | default | GL201500 |
| `baseline/50-gl-preferences.yaml` | GLPreferences | bootstrap | GL102000 |
| `baseline/60-ledger-company.yaml` | LedgerCompany | bootstrap | GL201500 |
| `baseline/90-uoms.yaml` | UnitsOfMeasure | default | CS203500 |
| `setup/10-financial-year.yaml` | FinancialYearSettings / GeneratePeriods | bootstrap | GL101000 |
| `setup/20-master-calendar.yaml` | MasterCalendar / GenerateCalendar | bootstrap | GL201000 |
| `setup/30-open-periods.yaml` | ManagePeriods / ProcessAll | bootstrap | GL503000 |
| `master/10-reason-codes.yaml` | ReasonCode | bootstrap | CS211000 |
| `master/20-in-preferences.yaml` | INPreferences | bootstrap | IN101000 |
| `master/30-availability-rules.yaml` | AvailabilityCalculationRule | bootstrap | IN201500 |
| `master/40-posting-classes.yaml` | PostingClass | bootstrap | IN206000 |
| `master/50-warehouse.yaml` | Warehouse | bootstrap | IN204000 |
| `master/51-warehouse-locations.yaml` | Warehouse (locations) | default | IN204000 |
| `master/52-warehouse-defaults.yaml` | Warehouse (receive/ship/RMA bins) | default | IN204000 |
| `master/53-tax-categories.yaml` | TaxCategory | default | TX205500 |
| `master/54-item-classes.yaml` | ItemClass | default | IN201000 |
| `master/56-so-preferences.yaml` | SOPreferences | bootstrap | SO101000 |
| `master/57-po-preferences.yaml` | POPreferences | bootstrap | PO101000 |
| `master/58-order-types.yaml` | OrderType | bootstrap | SO201000 |
| `master/60-ar-preferences.yaml` | ARPreferences | bootstrap | AR101000 |
| `master/61-ap-preferences.yaml` | APPreferences | bootstrap | AP101000 |
| `master/62-ca-preferences.yaml` | CAPreferences | bootstrap | CA101000 |
| `master/63-cash-account.yaml` | CashAccount | bootstrap | CA202000 |
| `master/64-payment-methods.yaml` | PaymentMethod | default | CA204000 |
| `master/65-statement-cycles.yaml` | StatementCycle | bootstrap | AR202800 |
| `master/70-vendor-classes.yaml` | VendorClass | bootstrap | AP201000 |
| `master/71-customer-classes.yaml` | CustomerClass | default | AR201000 |
| `master/75-vendors.yaml` | Vendor | default | AP303000 |
| `master/76-customers.yaml` | Customer | default | AR303000 |
| `master/80-stock-items-parts.yaml` | StockItem | default | IN202500 |
| `master/82-stock-items-kits.yaml` | StockItem | default | IN202500 |
| `master/85-kit-specifications.yaml` | KitSpecification | default | IN209500 |
| `scenario/buy-sell.yaml` | (transactions via `acu run`) | mixed | SO/PO/IN/AR |

`endpoint: bootstrap` resolves to the active Bootstrap package version from `bootstrap/project.xml`.

`endpoint: default` (or omitted on Default-only entities) resolves to `Default/<ACU_API_VERSION>`.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover or opening balances from a legacy system
- Replacing external data repos for production cutovers

## Changelog note (release)

When shipping the version that lands this flavor:

- Call out `acu config init --flavor distribution`.
- Call out default `apply` / `diff` directory order: `bootstrap/`, `baseline/`, `setup/`, `master/` (existing dirs only).
