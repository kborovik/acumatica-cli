# Distribution demo seed (acu config init --flavor distribution)

Packaged virgin-tenant demo for inventory/distribution (lab5 Demo Tenant Factory class).

**Start from a brand-new empty tenant.** Do not apply onto a half-configured company.

## Rebuild order

```sh
# 1. Credentials in .env (ACU_PASSWORD, ACU_TENANT, …); keep ACU_API_VERSION in sync with target.yaml
acu config check

# 2. Publish Bootstrap (features + contract from bootstrap/project.xml)
acu bootstrap

# 3. Seed config (default order includes master/ when present)
acu apply

# 4. Linked transaction scenario
acu run scenario/

# 5. Prove no drift
acu diff
```

Or explicit paths: `acu apply bootstrap/ baseline/ setup/ master/` then `acu diff` on the same set.

## Layout

| Path | Role |
|------|------|
| `bootstrap/` | Company, features, credit terms, Bootstrap `project.xml` (shared Bootstrap/1.0.0) |
| `baseline/` | GL foundation (COA, ledger, subaccounts, UOMs) |
| `setup/` | Financial year, master calendar, open periods |
| `master/` | Inventory, warehouse, items, vendors, customers, module prefs |
| `scenario/` | Golden path (buy/receive → sell/ship/invoice/pay; kits) |
| `target.yaml` | Verified ERP / Default API matrix |

Org CD is the single placeholder **LAB5** across company, ledger-company, open periods, inventory transit branch, and cash account.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover / opening balances from a legacy system
- Replacing external data repos for production cutovers

## Default flavor

`acu config init` without `--flavor` scaffolds finance-minimal (same Bootstrap `project.xml`, no `master/`, no scenario). That path stays offline-e2e green.
