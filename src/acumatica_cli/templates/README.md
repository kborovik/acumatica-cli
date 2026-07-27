# LAB5 demo seed (`acu config init`)

Packaged virgin-tenant demo for finance + inventory/distribution (lab5 Demo
Tenant Factory class). Single full seed — no `--flavor`.

**Start from a brand-new empty tenant.** Do not apply onto a half-configured company.

## Rebuild order

```sh
# 1. Credentials in .env (ACU_PASSWORD, ACU_TENANT, …); keep ACU_API_VERSION in sync with target.yaml
acu config check

# 2. Publish Bootstrap (features + contract from config/bootstrap/)
acu bootstrap

# 3. Seed config umbrella (bootstrap → baseline → setup → master)
acu apply config/

# 4. Lifecycle scenarios (once capital → buy → build → sell)
acu run scenario/

# 5. Prove no drift
acu diff config/

# 6. Capture derived-state observations (EndingBalance trial-balance)
acu state
# warm gate: once-capital only — additive buy/sell moves numeric observations
acu run scenario/10-seed-capital.yaml && acu state --assert-unchanged
```

Bare `acu apply` / `acu diff` also prefer `config/` when those trees exist.

## Layout

| Path | Role |
|------|------|
| `config/bootstrap/` | Company, features, credit terms, Bootstrap `project.xml` (Bootstrap/1.0.0) |
| `config/baseline/` | GL foundation (COA, ledger, subaccounts, UOMs) |
| `config/setup/` | Financial year, master calendar, open periods |
| `config/master/` | Inventory, warehouse, items, vendors, customers, module prefs |
| `scenario/10-seed-capital.yaml` | Once-class owner capital JE (skip-if-present when present) |
| `scenario/20-buy.yaml` | Additive component PO → receipt → bill → AP pay |
| `scenario/30-build.yaml` | Additive kit assembly |
| `scenario/40-sell.yaml` | Additive SO → ship → invoice → AR pay |
| `config/views/10-trial-balance.yaml` | Observer view (EndingBalance inquire; not SEED_DIRS) |
| `state/` | Written by `acu state` (derived-state observations) |

Monoscenario `buy-sell` is not part of this package.
