# Acumatica ERP - GitOps CLI

**`acu`** configures Acumatica ERP from YAML files in a git repo (GitOps). 

**No UI clicks, no Configuration Wizard.**

> **Tested against** Acumatica ERP **26.101.0225** on Windows Server 2025,
> contract REST endpoint **25.200.001**. Other versions will likely work,
> but only this combination is verified.

## Why

Acumatica configuration normally lives in the web UI: wizards, screens, and manual data entry that nobody can review, version, or reproduce. 

`acu` moves that configuration into YAML files in a git repo, so a tenant can be rebuilt from scratch, audited in a pull request, and checked for drift like any other infrastructure.

## Quick start

```sh
uv tool install acumatica-cli

acu config init --host erp.example.com my-erp
cd my-erp                                # edit .env: set ACU_PASSWORD, ACU_TENANT
                                         # Default API pin = target.yaml default_api
                                         # start from a brand-new empty tenant

acu config check                         # read-only preflight (incl. target.yaml)
acu tenant create --id 3 --login DEV     # create the tenant + bootstrap it (needs SSH)
# or hosted: acu --tenant DEV bootstrap
acu --tenant DEV apply config/           # seed config/{bootstrap,baseline,setup,master}/
acu --tenant DEV run scenario/           # once capital → buy → build → sell
acu --tenant DEV diff config/            # prove zero drift (exit 2 on drift)
acu --tenant DEV state                   # capture state/ trial-balance
acu --tenant DEV run scenario/           # replay transaction scenarios
```

Bare `apply` / `diff` (no path args) also prefer `config/` when those trees exist.
See [docs/demo-seed.md](docs/demo-seed.md) for the entity map, once-guard, and apply-order notes.

**Hosted Acumatica (no SSH):** the tenant already exists; set a blank `ACU_SSH=` in `.env` (scaffold omits the key — without it, acu defaults to `Administrator@<ACU_BASE_URL host>` for SSH boxes).

```sh
acu config init --host customer.acumatica.com my-erp
cd my-erp                                # edit .env: ACU_TENANT, ACU_PASSWORD; add ACU_SSH=
acu config check                         # REST preflight; ssh probe is skipped
acu --tenant DEV bootstrap               # publish AcuBootstrap via REST only
acu --tenant DEV apply config/
acu --tenant DEV diff config/
# offline UI fallback when REST publish is blocked:
acu bootstrap --export AcuBootstrap.zip  # import + publish on SM204505
```

## CLI map

```text
acu [--tenant NAME] [--url URL] [--ssh USER@HOST] [--api-version V]
    [--username U] [--password P] [--version] [--completion [SHELL]]
│
├── tenant                            tenant CRUD (ac.exe over SSH — control plane)
│   ├── list                          CompanyID, sign-in name, internal CD, type
│   ├── create --id N --login NAME    create + bootstrap; re-run to republish (SSH)
│   │          [--type SalesDemo|T100|U100] [--parent N] [--hidden] [--no-init]
│   └── delete --id N [--yes]         delete the tenant and its data, recycle app pool
│
├── bootstrap [--export PATH]         publish AcuBootstrap (REST); --export = offline zip
├── apply [--dry-run] [FILES...]      push YAML via REST (idempotent PUT upserts)
├── diff  [FILES...]                  drift check vs the live tenant (exit 2 on drift)
├── run   [--dry-run] [FILES...]      execute transaction scenario YAML (exit 1 on any miss)
├── state [--out DIR] [--diff] [--assert-unchanged] [--dry-run] [FILES...]
│                                     capture derived state into state/ (not seed)
├── extract [--out DIR] [--only NAME]... [--force] [--dry-run]
│                                     inverse of apply into config/{bootstrap,baseline,setup,master}/
├── inventory [--out DIR] [--force] [--dry-run] ARTIFACT
│                                     offline snapshot artifact → inventory/ (not seed)
├── reconcile [--inventory DIR] [--config DIR] [--out DIR] [--force] [--dry-run]
│                                     inventory/ + optional config/ → findings/ only
├── schema [--out DIR]                dump the endpoint's OpenAPI schema (swagger.json)
│
└── config                            configuration ops
    ├── init [--host HOST] [DIR]      scaffold full data repo (config/, scenario/, target.yaml)
    ├── show                          print the resolved config as a complete .env
    └── check [--strict]              preflight: discovery, secrets, target, REST, endpoints, SSH
```

`apply` and `diff` without FILES prefer `config/<name>/` when any seed child exists under `config/`; otherwise root `bootstrap/`, `baseline/`, `setup/`, then `master/` when present.
A path like `config/` expands nested seed dirs in that fixed order.
`run` without FILES defaults to `scenario/`.
`state` without FILES defaults to `config/views/`; writes go to `state/` (`--out`).
`extract` always writes under `config/{bootstrap,baseline,setup,master}/` (catalog-driven; never root SEED_DIRS).
`inventory` is offline (no REST/SSH/password): SM203520 Settings XML ZIP or `ac.exe export xml` folder writes to `inventory/`.
`reconcile` is offline: compare `inventory/` to optional `config/` and write `findings/` only (never writes seed).
`acu --completion` emits a completion script for bash, zsh, or fish — source it from your shell profile.
Run `acu <command> --help` for details on any command.

### Dual readers, one writer

Two read paths, one mutator (V35).
Do not confuse them with each other or with `state`:

| Command | Plane | Input | Writes | Role |
| ------- | ----- | ----- | ------ | ---- |
| `extract` | REST (live) | tenant via contract API | `config/{bootstrap,baseline,setup,master}/` | Inverse of `apply` — seed YAML |
| `inventory` | Offline | SM203520 Settings XML ZIP or `ac.exe export xml` folder | `inventory/` (`summary.yaml` + `tables/`) | Full-table snapshot IR — not seed |
| `reconcile` | Offline | `inventory/` + optional `config/` | `findings/` only | Cross-check gaps/deltas — never mutates seed or tenant |
| `state` | REST (live) | `config/views/` | `state/` | Derived balances/totals — not seed, not inventory |
| `apply` | REST (live) | seed YAML under `config/` | tenant | **Sole** tenant writer (keyed PUT) |

`inventory/` and `findings/` are engagement outputs: not SEED_DIRS, never loaded by `apply`/`diff`, not scaffolded by `config init`.
Binary `.adb` snapshots are rejected (XML only).
See [docs/ac-exe.md](docs/ac-exe.md) for export / SM203520 notes and [docs/demo-seed.md](docs/demo-seed.md) for the extract/state/inventory map.

## The data repo

Your configuration lives in its own git repo.
`acu config init` scaffolds a **single full seed** under `config/` (Bootstrap `project.xml` at `Bootstrap/1.0.0`, expanded COA, masters) plus lifecycle `scenario/`, observer `config/views/`, and README.
There is no `--flavor`.

| Path | What it holds |
| ---- | ------------- |
| `config/bootstrap/` | virgin-tenant config: features, company, credit terms, `project.xml` |
| `config/baseline/` | reference data: subaccounts, COA, ledger, UOMs |
| `config/setup/` | one-time actions: financial year, master calendar, open periods |
| `config/master/` | inventory/distribution masters (prefs, warehouse, items, parties) |
| `scenario/` | lifecycle txns for `acu run`: once capital, then buy, build, sell |
| `config/views/` | observer views for `acu state` (`inquire:` / `entity:` / `gi:`; not SEED_DIRS) |
| `state/` | committed derived-state observations (evidence, not seed; money/qty fixed-point) |
| `inventory/` | engagement: offline snapshot tables from `acu inventory` (not seed; not SEED_DIRS) |
| `findings/` | engagement: `acu reconcile` cross-check output (never apply path) |
| `target.yaml` | committed verified matrix: `erp` + `default_api` (what, not where) |
| `.env` | where to apply and who signs in, every key an `ACU_*` variable |

Legacy data repos may still keep root `bootstrap/`…`master/`; bare `apply`/`diff` prefer `config/` when present and never merge both trees.

Files in each directory apply alphabetically; the numbered prefixes (`10-`, `20-`, and so on) encode dependency order.
Commit `target.yaml` with the seeds so every clone knows the verified ERP line and Default API generation.

Seed YAML is state: `apply` upserts it, `diff` proves it.
`acu extract` is the inverse of `apply`: GET live tenant rows into seed YAML under `config/{bootstrap,baseline,setup,master}/` (hard-cut).
Packaged `seed_catalog.yaml` is the sole extract registry (entity, endpoint, keys, file, strip/include, filter-split); the demo entity map in [docs/demo-seed.md](docs/demo-seed.md) mirrors those catalog paths.
Features synthesize to `config/bootstrap/features.yaml`.
Existing files skip unless `--force`; empty live sets skip; row failures continue (exit 1 only if any row failed — drift stays with `diff`).

```sh
acu --tenant DEV extract --out . --force   # refresh config/** from live tenant
git diff config/                           # review extract delta before commit
acu --tenant DEV apply config/             # replay extracted seed
acu --tenant DEV diff config/              # expect exit 0
```
### Seed `endpoint:` symbols

Dual-served entities (on both Bootstrap and Default) need an explicit `endpoint:` line.

| Value | Resolves to |
| ----- | ----------- |
| omitted | `Default/<api_version>` for Default-only entities |
| `bootstrap` | active `Bootstrap/<ver>` from `bootstrap/project.xml` or the packaged contract |
| `default` | `Default/<api_version>` — tracks the resolved API version |
| `Bootstrap/1.0.0` or `Default/25.200.001` | literal pin (ignores the resolved Default version) |

`api_version` resolves as `--api-version` flag, else `target.yaml` `default_api` when present, else code default `25.200.001` (never `ACU_API_VERSION` in `.env`).
Prefer symbolic `default` over a pinned `Default/25.200.001` so the seed tree travels with the dataset pin.

## Installation

Requires Python 3.14 or newer.

```sh
uv tool install acumatica-cli
```

`pipx install acumatica-cli` and `pip install acumatica-cli` work too.
For the latest development version straight from the main branch:

```sh
uv tool install git+https://github.com/kborovik/acumatica-cli.git
```

Verify with `acu --version`.

## Configuration

Everything lives in one `.env` file: *where* to apply and *who* signs in
(`ACU_*` vars only).
The Default contract API pin is **not** in `.env` — it
lives in committed `target.yaml` (`default_api`).

```sh
ACU_BASE_URL=http://acu-dev1.vm.internal/AcumaticaERP  # required: REST root
ACU_TENANT=LAB5                                        # sign-in name of the tenant API sessions use
# ACU_SSH omitted → defaults to Administrator@acu-dev1.vm.internal
# ACU_SSH=                                         # hosted opt-out (blank key)
ACU_USER=admin                                         # optional, defaults to admin
ACU_PASSWORD=...                                       # required for live commands
```

There is no `ACU_API_VERSION` env key (unknown `ACU_*` vars are ignored).
Ad-hoc override: `acu --api-version 24.200.001 …` (version half only, never
`Default/25.200.001` — a full path would nest as `/entity/Default/Default/...`).

The committed `target.yaml` next to `.env` declares the verified matrix (what, not where) and is the **sole data-repo Default pin**:

```yaml
erp: "26.101.0225"           # claimed product line/build
default_api: "25.200.001"    # sources Instance.api_version when --api-version absent
```

When `target.yaml` is present, live commands resolve `api_version` from
`default_api` (source-merge). `acu config check` reports
`ok target (api_version from default_api=…; erp=… claimed)`.
Missing `target.yaml` only warns on check unless you pass `--strict` (then
the code default `25.200.001` is used).

Worth knowing:

- The file is found by walking up from the current directory, so any subdirectory of the data repo works.
- Without a `.env`, global flags plus the process environment supply the full configuration.
- When `ACU_SSH` is **absent**, acu defaults to `Administrator@` + the `ACU_BASE_URL` hostname.
  A **present blank** `ACU_SSH=` is the hosted opt-out.
  Only `acu tenant` requires a non-empty value post-default.
- `acu config show` prints the resolved `.env` (password excluded; never `ACU_API_VERSION`) and comments `erp` / `default_api` plus the `api_version` source when `target.yaml` is present.
- Redirect it to turn resolved state into a working config: `acu config show > .env`.

Verify before touching anything live:

```sh
acu config check           # discovery, secrets, target, REST, endpoints, SSH
acu config check --strict  # missing target.yaml becomes fail
acu apply --dry-run        # show what would be written, write nothing
```

## Development

Requires **GNU Make at least 3.82** — the Makefile uses `.ONESHELL`.
On macOS use Homebrew's `gmake` (`brew install make`); `/usr/bin/make` is 3.81 and fails the guard.
Elsewhere plain `make` is fine when it is GNU Make.

```sh
git clone https://github.com/kborovik/acumatica-cli.git
cd acumatica-cli
gmake install    # editable install as a global uv tool
gmake check      # offline gate: ruff, basedpyright strict, pytest
```

The default test suite is fully offline.
REST is faked with `httpx.MockTransport`, SSH with a monkeypatched `subprocess.run` — no live instance is needed.
`gmake check` must pass before every commit.
GitHub Actions runs the same gate on every push and pull request to `main`.

### Release

```sh
gmake release patch   # or minor | major
```

Local release runs `gmake check`, bumps the version, commits, tags `v<version>`, and pushes.
GitHub Actions re-runs the check on the tag, then publishes the GitHub release and PyPI package only if that check passes.

### Live end-to-end tier

`gmake e2e` runs the opt-in live tier against a real Acumatica instance (pytest marker `e2e`, deselected by the default suite).

Configuration is one file: a decrypted `.env` at the repo root names the instance — `ACU_BASE_URL`, `ACU_TENANT`, `ACU_PASSWORD` (and optional `ACU_SSH`; omitted defaults to `Administrator@` + base-url host).
`gmake e2e` refuses to start without it.

The tier is self-contained.
Each run scaffolds a synthetic single-org company from the packaged `acu config init` templates into a temporary directory, copies the real `.env` into it, and runs the installed `acu` binary from there — no data repo, no pre-existing fixtures on the instance.
Scratch tenants (`E2E`, `E2EA`, `E2EB`, `E2ESCEN`) are created on the way in and always deleted on the way out, so nothing persists.
The packaged full `config init` seed (under `config/`) is the only scaffold.

```sh
gmake e2e                                # whole tier, about 20 minutes
gmake e2e FILE=test_provision_lifecycle  # apply/diff focus
gmake e2e FILE=test_scenario_lifecycle   # scenario + state focus
```

## License

This project is licensed under the PolyForm Noncommercial License 1.0.0.
Noncommercial use is free under that license.
Commercial use requires a separate license — contact [lab5.ca](https://lab5.ca).

See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Konstantin Borovik.
