# LAB5 demo seed (`acu config init`)

Packaged virgin-tenant demo for finance + inventory/distribution (lab5 Demo
Tenant Factory class). Single full seed — no `--flavor`.

**Start from a brand-new empty tenant.** Do not apply onto a half-configured company.

## Rebuild order

```sh
# 1. Credentials in .env (ACU_PASSWORD, ACU_TENANT, …)
#    Default API pin + REST where = committed matrix.yaml cell
#    (default_api + base_url; not sticky ACU_BASE_URL / ACU_API_VERSION)
acu config check

# 2. Publish Bootstrap (features + contract from config/bootstrap/)
acu bootstrap

# 3. Seed config umbrella (bootstrap → baseline → setup → master)
#    bare `acu apply` also appends overlays/default-<default_api>/ when present
acu apply

# 4. Lifecycle scenarios (once capital → buy → build → sell)
#    bare `acu run` replaces same-name files from pin overlay scenario/
acu run

# 5. Prove no drift
acu diff

# 6. Capture derived-state observations (EndingBalance trial-balance)
acu state
# warm gate: once-capital only — additive buy/sell moves numeric observations
acu run scenario/10-seed-capital.yaml && acu state --assert-unchanged

# 7. Cold matrix lifecycle (SSH + tenant; optional multi-cell --all)
# acu check --yes

# Optional: re-seed from live (inverse of apply; always under config/)
# acu extract --out . --force
```

Bare `acu apply` / `acu diff` also prefer `config/` when those trees exist.
`acu extract` hard-cuts emit to `config/{bootstrap,baseline,setup,master}/` (never root SEED_DIRS).

## Layout

| Path | Role |
|------|------|
| `matrix.yaml` | Multi-host pin+where: cells `id`+`erp`+`default_api`+`base_url` (V27); `--cell` selects |
| `config/bootstrap/` | Company, features, credit terms (Bootstrap contract is package SoT — never scaffolded) |
| `config/baseline/` | GL foundation (COA, ledger, subaccounts, UOMs) |
| `config/setup/` | Financial year, master calendar, open periods |
| `config/master/` | Numbering (`05-numbering-sequences`) before module prefs; inventory, warehouse, items, vendors, customers; roles/users (`90-roles` then `91-users`) |
| `scenario/10-seed-capital.yaml` | Once-class owner capital JE (skip-if-present when present); Period = `${current_period}` |
| `scenario/20-buy.yaml` | Additive component PO → receipt → bill → AP pay |
| `scenario/30-build.yaml` | Additive kit assembly |
| `scenario/40-sell.yaml` | Additive SO → ship → invoice → AR pay |
| `overlays/` | Default-half rewrites (`default-<default_api>/`); bare apply/run/diff auto-compose |
| `overlays/default-24.200.001/` | Lab 25r1 half: KitAssembly Type Assembly |
| `config/views/10-trial-balance.yaml` | Observer view (EndingBalance inquire; Period pinned literal; not SEED_DIRS) |
| `state/` | Written by `acu state` (derived-state observations) |

`acu run` expands `${current_period}` to host-local `MMyyyy`. Views for `acu state` stay pinned so committed `state/` rows do not rewrite every month.

Monoscenario `buy-sell` is not part of this package.
