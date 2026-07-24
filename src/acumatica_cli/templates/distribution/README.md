# Distribution demo seed (acu config init --flavor distribution)

Packaged virgin-tenant demo for inventory/distribution (lab5 Demo Tenant Factory class).

**Start from a brand-new empty tenant.** Do not apply onto a half-configured company.

## Rebuild order

```sh
# 1. Credentials in .env (ACU_PASSWORD, ACU_TENANT, …); keep ACU_API_VERSION in sync with target.yaml
acu config check

# 2. Publish Bootstrap (features + contract from config/bootstrap/)
acu bootstrap

# 3. Seed config umbrella (bootstrap → baseline → setup → master)
acu apply config/

# 4. Lifecycle scenarios (once capital → buy → build stub → sell)
acu run scenario/

# 5. Prove no drift
acu diff config/
```

Bare `acu apply` / `acu diff` also prefer `config/` when those trees exist.

## Layout

| Path | Role |
|------|------|
| `config/bootstrap/` | Company, features, credit terms, Bootstrap `project.xml` (shared Bootstrap/1.0.0) |
| `config/baseline/` | GL foundation (COA, ledger, subaccounts, UOMs) |
| `config/setup/` | Financial year, master calendar, open periods |
| `config/master/` | Inventory, warehouse, items, vendors, customers, module prefs |
| `scenario/` | Lifecycle: `10-seed-capital` (once) + `20-buy-gateways` + `30-build` (stub) + `40-sell` |
| `target.yaml` | Verified ERP / Default API matrix |

Org CD is the single placeholder **LAB5** across company, ledger-company, open periods, inventory transit branch, and cash account.

## Once vs additive scenarios

`scenario/10-seed-capital.yaml` declares `once: true` with an inquire-absolute `present` probe on Owner Capital (account 30000).

A warm second `acu run scenario/` skips capital when the probe already holds, so Owner Capital stays 50000 (not 100000).

Additive buy/sell legs re-run with per-leg delta expects.

Monoscenario `buy-sell` is not part of this flavor.

## Non-goals

- Multi-org, multicurrency, full tax engine
- Production cutover / opening balances from a legacy system
- Replacing external data repos for production cutovers

## Default flavor

`acu config init` without `--flavor` scaffolds the finance-minimal package set at the **root** (same Bootstrap `project.xml`, no `config/`, no `master/`, no scenario). That path stays offline-e2e green.
