# LAB5 demo seed (`acu config init`)

Packaged virgin-tenant demo for finance + inventory/distribution (lab5 Demo
Tenant Factory class). Single full seed — no `--flavor`.

**Start from a brand-new empty tenant.** Do not apply onto a half-configured company.

## Rebuild order

```sh
# 1. Credentials in .env (ACU_PASSWORD, ACU_TENANT, ACU_BASE_URL, ACU_API_VERSION)
acu config check

# 2. Publish Bootstrap (features + contract from package)
acu bootstrap

# 3. Seed acu-config umbrella (bootstrap → baseline → setup → master)
acu apply acu-config

# 4. Lifecycle scenarios (once capital → buy → build → sell)
acu run acu-scenario

# 5. Prove no drift
acu diff acu-config

# 6. Capture derived-state observations (EndingBalance trial-balance)
acu state acu-config/views
# warm gate: once-capital only — additive buy/sell moves numeric observations
acu run acu-scenario/10-seed-capital.yaml && acu state --assert-unchanged acu-config/views

# SSH cold rebuild: acu tenant create --login NAME, then apply, run, diff

# Optional: re-seed from live (inverse of apply; --out default acu-config/)
# acu survey extract --force
```

apply / diff / run / state require an explicit data path.
`acu survey extract` writes SEED_DIRS into `--out` (default `acu-config/`).

## Layout

| Path | Role |
|------|------|
| `.env` | Secrets + where (`ACU_BASE_URL`) + pin (`ACU_API_VERSION`) |
| `acu-config/bootstrap/` | Company, features, credit terms (Bootstrap contract is package SoT — never scaffolded) |
| `acu-config/baseline/` | GL foundation (COA, ledger, subaccounts, UOMs) |
| `acu-config/setup/` | Financial year, master calendar, open periods |
| `acu-config/master/` | Numbering (`05-numbering-sequences`) before module prefs; inventory, warehouse, items, vendors, customers; roles/users (`90-roles` then `91-users` then `92-role-users`) |
| `acu-scenario/10-seed-capital.yaml` | Once-class owner capital JE (skip-if-present when present); Period = `${current_period}` |
| `acu-scenario/20-buy.yaml` | Additive component PO → receipt → bill → AP pay |
| `acu-scenario/30-build.yaml` | Additive kit assembly |
| `acu-scenario/40-sell.yaml` | Additive SO → ship → invoice → AR pay |
| `overlays/` | Default-half rewrites (`default-<api_version>/`); pass as extra explicit paths |
| `overlays/default-24.200.001/` | Lab 25r1 half: KitAssembly Type Assembly |
| `acu-config/views/10-trial-balance.yaml` | Observer view (EndingBalance inquire; Period pinned literal; not SEED_DIRS) |
| `state/` | Written by `acu state` (derived-state observations) |

`acu run` expands `${current_period}` to host-local `MMyyyy`. Views for `acu state` stay pinned so committed `state/` rows do not rewrite every month.

Monoscenario `buy-sell` is not part of this package.
