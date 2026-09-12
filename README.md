# Acumatica ERP - GitOps CLI

**`acu`** configures Acumatica ERP from YAML files in a git repo (GitOps). 

**No UI clicks, no Configuration Wizard.**

> **Tested against** Acumatica ERP **26.101.0225** on Windows Server 2025,
> contract REST endpoint **25.200.001**. Other versions will likely work,
> but only this combination is verified.

## Why

Acumatica configuration normally lives in the web UI: wizards, screens, and manual data entry that nobody can review, version, or reproduce. 

`acu` moves that configuration into YAML files in a git repo, so a tenant can be rebuilt from scratch, audited in a pull request, and checked for drift like any other infrastructure.

## What this tool is capable of

YAML in git is the source of truth; the live tenant is the target. `apply` is the only writer.

| You can | Command | Effect |
| ------- | ------- | ------ |
| Seed a tenant from YAML | `apply` | Idempotent PUT of `acu-config/` into the live tenant |
| Detect drift | `diff` | Compare seed YAML to live (exit 2 when they diverge) |
| Pull live config into YAML | `survey extract` | Inverse of `apply` — GET into `acu-config/{bootstrap,baseline,setup,master}/` |
| Run transaction scripts | `run` | Forward documents from `acu-scenario/` (capital, buy, build, sell) |
| Capture derived balances | `state` | Inquire trial-balance etc. into `state/` (not seed) |
| Create and destroy tenants | `tenant` | SSH control plane: list / create / delete / recycle |
| Publish the Bootstrap contract | `bootstrap` | REST publish of AcuBootstrap (or `--export` zip for the UI) |
| Prove a cold rebuild | `tenant create` then `apply` then `run` then `diff` | Compose on a fresh tenant. No wrap command. |
| Snapshot a site offline | `survey inventory` | SM203520 XML ZIP or `ac.exe export xml` writes `inventory/` |
| Cross-check snapshot vs seed | `survey reconcile` | `inventory/` plus optional `--config` writes `findings/` only |
| Dump the contract schema | `schema` | OpenAPI `swagger.json` for the pinned endpoint |
| Scaffold a data repo | `config init` | Full `acu-config/`, `acu-scenario/`, `.env` tree |
| Preflight a target | `config check` | Discovery, secrets, REST, endpoints, SSH |

Seed YAML covers features, company, credit terms, subaccounts, chart of accounts, ledger, UOMs, financial year / calendar / periods, numbering sequences, inventory and distribution masters, roles, and users.

REST is the data plane (`apply`, `diff`, `run`, `survey extract`, `state`, `bootstrap`, `schema`).
SSH is the control plane (`tenant *`).

Hosted sites skip SSH.

## Quick start

```sh
uv tool install acumatica-cli

acu config init --host erp.example.com my-erp
cd my-erp                                # edit .env: ACU_PASSWORD, ACU_TENANT
                                         # ACU_BASE_URL is where; ACU_API_VERSION pins Default
                                         # start from a brand-new empty tenant

acu config check                         # read-only preflight
acu tenant create --login DEV            # create + bootstrap (SSH; --id optional)
# or hosted: acu --tenant DEV bootstrap
acu --tenant DEV apply acu-config        # seed acu-config/{bootstrap,baseline,setup,master}/
acu --tenant DEV run acu-scenario        # once capital → buy → build → sell
acu --tenant DEV diff acu-config         # prove zero drift (exit 2 on drift)
acu --tenant DEV state acu-config/views  # capture state/ trial-balance
```

apply / diff / run / state require an explicit data path.

**Hosted Acumatica (no SSH):** the tenant already exists; set a blank `ACU_SSH=` in `.env`.
The scaffold omits the key — without it, acu defaults to `Administrator@<ACU_BASE_URL host>` for SSH boxes.

```sh
acu config init --host customer.acumatica.com my-erp
cd my-erp                                # edit .env: ACU_TENANT, ACU_PASSWORD; add ACU_SSH=
acu config check                         # REST preflight; ssh probe is skipped
acu --tenant DEV bootstrap               # publish AcuBootstrap via REST only
acu --tenant DEV apply acu-config
acu --tenant DEV diff acu-config
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
│   ├── create --login NAME [--id N]  create + bootstrap; re-run to republish (SSH)
│   │          [--type SalesDemo|T100|U100] [--parent N] [--hidden] [--no-init]
│   │                                 omit --id → next free CompanyID (max list + 1)
│   ├── delete --id N | --login NAME [--yes]
│   │                                 delete the tenant and its data, recycle app pool
│   └── recycle [--yes]               restart site app pool (tenant map + free API slots)
│
├── bootstrap [--export PATH]         publish AcuBootstrap (REST); --export = offline zip
├── apply [--dry-run] <FILES...>      push YAML via REST (idempotent PUT upserts)
├── diff  <FILES...>                  drift check vs the live tenant (exit 2 on drift)
├── run   [--dry-run] <FILES...>      execute transaction scenario YAML (exit 1 on any miss)
├── state [--out DIR] [--diff] [--assert-unchanged] [--dry-run] <FILES...>
│                                     capture derived state into state/ (not seed)
├── survey                            existing-tenant dual-reader (never L1 extract/inventory/reconcile)
│   ├── extract [--out DIR] [--only NAME]... [--force] [--dry-run]
│   │                                 inverse of apply into acu-config/{bootstrap,baseline,setup,master}/
│   ├── inventory [--out DIR] [--force] [--dry-run] ARTIFACT
│   │                                 offline snapshot artifact → inventory/ (not seed)
│   └── reconcile [--inventory DIR] [--config DIR] [--out DIR] [--force] [--dry-run]
│                                     inventory/ + optional --config DIR → findings/ only
├── schema [--out DIR]                dump the endpoint's OpenAPI schema (swagger.json)
│
└── config                            configuration ops
    ├── init [--host HOST] [DIR]      scaffold full data repo (acu-config/, acu-scenario/, .env)
    ├── show                          print the resolved config as a complete .env
    └── check                         preflight: discovery, secrets, REST, endpoints, SSH
```

`apply` / `diff` / `run` / `state` require an explicit data path. Zero args print that command's help and do not call HTTP.

A dir with SEED_DIRS children (`bootstrap|baseline|setup|master`) expands those subdirs in that order. Typed path is used as given (`acu apply acu-config`, or `acu apply config/` if that dir exists).

`--out` defaults: `state` → `state/`; `survey extract` → `acu-config/` (SEED_DIRS into that root); `survey inventory` → `inventory/`; `survey reconcile` → `findings/`; `schema` → `schemas/`.

Scenario YAML may use `${current_period}` (host-local `MMyyyy`) on steps, expect params, and `once.present` params.
`acu-config/views` and `state` keep Period pinned.

`survey extract` writes SEED_DIRS into `--out` (default `acu-config/`).

`survey inventory` and `survey reconcile` are offline (no REST, SSH, or password).
`survey inventory` turns an SM203520 Settings XML ZIP or `ac.exe export xml` folder into `inventory/`.

`survey reconcile` compares `inventory/` to optional `--config DIR` and writes `findings/` only.
It never writes seed.

Optional `snapshot_map.yaml` (data-repo root, or package defaults) maps DAC tables to catalog entities.
It normalizes the join: pad-trim, key/field aliases, Account/Sub FK CD resolve, enum label to code.

`acu --completion` emits a completion script for bash, zsh, or fish — source it from your shell profile.

Run `acu --help` for the full mental model (workflow, planes, exit codes, command map).
That is enough for an agent to learn the tool without extra docs.

Run `acu <command> --help` (or `-h`) for flags, examples, and prerequisites.

### Dual readers, one writer

Two read paths, one mutator (V35).
Do not confuse them with each other or with `state`:

| Command | Plane | Input | Writes | Role |
| ------- | ----- | ----- | ------ | ---- |
| `survey extract` | REST (live) | tenant via contract API | `acu-config/{bootstrap,baseline,setup,master}/` | Inverse of `apply` — seed YAML |
| `survey inventory` | Offline | SM203520 Settings XML ZIP or `ac.exe export xml` folder | `inventory/` (`summary.yaml` + `tables/`) | Full-table snapshot IR — not seed |
| `survey reconcile` | Offline | `inventory/` + optional `--config DIR` | `findings/` only | Cross-check gaps/deltas — never mutates seed or tenant |
| `state` | REST (live) | `acu-config/views/` | `state/` | Derived balances/totals — not seed, not inventory |
| `apply` | REST (live) | seed YAML under `acu-config/` | tenant | **Sole** tenant writer (keyed PUT) |

`inventory/` and `findings/` are engagement outputs: not SEED_DIRS, never loaded by `apply`/`diff`, not scaffolded by `config init`.
Binary `.adb` snapshots are rejected (XML only).

## The data repo

Your configuration lives in its own git repo.
`acu config init` scaffolds a **single full seed** under `acu-config/` (features, company, credit terms, expanded COA, masters) plus lifecycle `acu-scenario/`, observer `acu-config/views/`, and README.

It does not write a customization tree.

The Bootstrap endpoint contract is package SoT (`bootstrap_project.xml` inside the CLI — `Bootstrap/1.4.0`).
`config init` never writes `project.xml`, and data repos must not keep one (a present file hard-errors on bootstrap/publish).

There is no `--flavor`.

| Path | What it holds |
| ---- | ------------- |
| `acu-config/bootstrap/` | virgin-tenant config: features, company, credit terms (no `project.xml`) |
| `acu-config/baseline/` | reference data: subaccounts, COA, ledger, UOMs |
| `acu-config/setup/` | one-time actions: financial year, master calendar, open periods |
| `acu-config/master/` | inventory/distribution masters: numbering (`05-…`) before prefs, warehouse, items, parties + Role/User (`90-roles` then `91-users` then `92-role-users`) |
| `acu-scenario/` | lifecycle txns for `acu run`: once capital, then buy, build, sell |
| `acu-config/views/` | observer views for `acu state` (`inquire:` / `entity:` / `gi:`; not SEED_DIRS) |
| `state/` | committed derived-state observations (evidence, not seed; money/qty fixed-point) |
| `inventory/` | engagement: offline snapshot tables from `acu survey inventory` (not seed; not SEED_DIRS) |
| `findings/` | engagement: `acu survey reconcile` cross-check output (never apply path) |
| `.env` | secrets + where (`ACU_BASE_URL`) + pin (`ACU_API_VERSION`) |

Files in each directory apply alphabetically; the numbered prefixes (`10-`, `20-`, and so on) encode dependency order.
Commit seed YAML; keep `.env` out of git (scaffold `.gitignore` already lists it).

Seed YAML is state: `apply` upserts it, `diff` proves it.
`acu survey extract` is the inverse of `apply`: GET live tenant rows into seed YAML under `acu-config/{bootstrap,baseline,setup,master}/` (hard-cut).

Packaged `seed_catalog.yaml` is the sole extract registry (entity, endpoint, keys, file, strip/include, filter-split).

Features synthesize to `acu-config/bootstrap/features.yaml`.
Existing files skip unless `--force`; empty live sets skip.

Row failures continue (exit 1 only if any row failed).
Drift stays with `diff`.

```sh
acu --tenant DEV survey extract --force           # refresh acu-config/** from live tenant
git diff acu-config/                           # review extract delta before commit
acu --tenant DEV apply acu-config             # replay extracted seed
acu --tenant DEV diff acu-config              # expect exit 0
```
### Seed `endpoint:` symbols

Dual-served entities (on both Bootstrap and Default) need an explicit `endpoint:` line.

| Value | Resolves to |
| ----- | ----------- |
| omitted | `Default/<api_version>` for Default-only entities |
| `bootstrap` | active `Bootstrap/<ver>` from the packaged contract only |
| `default` | `Default/<api_version>` — tracks the resolved API version |
| `Bootstrap/1.4.0` or `Default/25.200.001` | literal pin (ignores the resolved Default version) |

`api_version` resolves as `--api-version` flag, else `ACU_API_VERSION` in `.env`, else code default `25.200.001`.
`base_url` resolves as `--url`, else `ACU_BASE_URL` (required).

Prefer symbolic `default` over a pinned `Default/25.200.001` so the seed tree travels with the dataset pin.

## Installation

Requires Python 3.14 or newer.

```sh
uv tool install acumatica-cli
```

`pipx install acumatica-cli` and `pip install acumatica-cli` work too.

Or clone and install editable for development:

```sh
git clone https://github.com/kborovik/acumatica-cli.git
cd acumatica-cli
gmake install    # uv sync; does not replace the PyPI tool
```

Verify with `uv run acu --version` (`+dev`).

## Configuration

Secrets, REST where, and the Default contract pin live in one `.env` file (`ACU_*` vars).
Leftover `matrix.yaml` is ignored and never loaded.

```sh
ACU_BASE_URL=http://erp.example.com/AcumaticaERP        # REST where (required)
ACU_API_VERSION=25.200.001                              # Default contract half
ACU_TENANT=LAB5                                         # sign-in name of the tenant API sessions use
# ACU_SSH omitted → defaults to Administrator@ + resolved base_url host
# ACU_SSH=                                              # hosted opt-out (blank key)
ACU_USER=admin                                          # optional, defaults to admin
ACU_PASSWORD=...                                        # required for live commands
```

Ad-hoc override: `acu --api-version 24.200.001 …` (version half only, never
`Default/25.200.001` — a full path would nest as `/entity/Default/Default/...`).

`api_version` resolves as `--api-version`, else `ACU_API_VERSION`, else `25.200.001`.
`base_url` resolves as `--url`, else `ACU_BASE_URL`, else a hard error naming sources.

`acu config show` prints the resolved `.env` (password excluded; includes `ACU_API_VERSION`).

### Multi-host overlays (V44)

One **trunk seed** in the data repo serves every host.
Version fan-out is **not** long-running product branches (`acu-25r1`, `acu-26r1`, …).

| Piece | Role |
| ----- | ---- |
| Trunk seed | Canonical `acu-config/` + `acu-scenario/` (newest supported Default half) |
| `.env` pin | Per-checkout `ACU_BASE_URL` + `ACU_API_VERSION` |
| Optional overlays | Surgical seed deltas keyed by Default half (e.g. `overlays/default-24.200.001/`) |
| OpenAPI | Live `acu schema` dump only (gitignored); never multi-version swagger trees in package or data repo |

**Overlays** live under `overlays/default-<api_version>/` (scaffolded by `acu config init`).
No `--overlay` flag.

Pass overlay trees as extra explicit paths when you need them.

Pin = resolved `api_version` (`--api-version`, else `ACU_API_VERSION`, else code default).

```sh
# ACU_API_VERSION=24.200.001 — this overlay is scenario-only
acu apply acu-config
acu diff acu-config
acu run acu-scenario overlays/default-24.200.001/acu-scenario/
```

Add a future half by creating `overlays/default-<new-half>/` with the
minimal rewrite; no long-running product branches and no multi-version
OpenAPI trees (V11/V44).

Sibling data-repo retirement of release branches:
[acumatica-gitops#2](https://github.com/kborovik/acumatica-gitops/issues/2).

Worth knowing:

- The `.env` file is found by walking up from the current directory, so any subdirectory of the data repo works.
- Without a `.env`, global flags plus the process environment supply the configuration.
- When `ACU_SSH` is **absent**, acu defaults to `Administrator@` + the resolved base_url hostname.
  A **present blank** `ACU_SSH=` is the hosted opt-out.
  Only `acu tenant` requires a non-empty value post-default.
- `acu config show` prints the resolved `.env` (password excluded).
- Redirect it to turn resolved state into a working config: `acu config show > .env`.

Verify before touching anything live:

```sh
acu config check           # discovery, secrets, REST, endpoints, SSH
acu apply --dry-run        # show what would be written, write nothing
```

## Development

Requires **GNU Make at least 3.82** — the Makefile uses `.ONESHELL`.
On macOS use Homebrew's `gmake` (`brew install make`); `/usr/bin/make` is 3.81 and fails the guard.

Elsewhere plain `make` is fine when it is GNU Make.

```sh
git clone https://github.com/kborovik/acumatica-cli.git
cd acumatica-cli
gmake install    # uv sync; does not replace the PyPI tool
gmake check      # offline gate: ruff, basedpyright strict, pytest
```

The default test suite is fully offline.
REST is faked with `httpx.MockTransport`, SSH with a monkeypatched `subprocess.run` — no live instance is needed.

`gmake check` must pass before every commit.
GitHub Actions runs the same gate on every push and pull request to `main`.

### Release

Human release notes live in root [`CHANGELOG.md`](CHANGELOG.md) (Keep a Changelog).
During development, append user-facing work under `## Unreleased` in `### Added` / `### Changed` / `### Fixed` as appropriate.

Empty Unreleased (no bullets) hard-fails the release — nothing to ship.

```sh
gmake release patch   # or minor | major
```

`gmake release` is the sole release path (never local `gh release create`):

1. `gmake check` (ruff, basedpyright, offline pytest)
2. Fail if `## Unreleased` has no bullets
3. Bump `pyproject.toml` version (`major` | `minor` | `patch`)
4. Promote Unreleased body to `## [vX.Y.Z] - YYYY-MM-DD`, leave an empty `## Unreleased`
5. Commit `CHANGELOG.md` + `pyproject.toml` (+ lock if bumped) together
6. `gmake check` again on the promoted tree — fail keeps the commit, creates no tag, pushes nothing
7. Tag `vX.Y.Z` and push branch + that tag — GitHub Actions publishes

GitHub Actions on tag `v*` re-runs CI, builds sdist+wheel, publishes to PyPI via OIDC trusted publishing, and creates a GitHub Release whose notes are the promoted CHANGELOG section for that tag (plus the artifacts).

### Live end-to-end tier

`gmake e2e` runs the opt-in live tier against a real Acumatica instance (pytest marker `e2e`, deselected by the default suite).

Configuration is one file: a decrypted `.env` at the repo root names the instance — `ACU_BASE_URL`, `ACU_TENANT`, `ACU_PASSWORD` (and optional `ACU_SSH`; omitted defaults to `Administrator@` + base-url host).
`gmake e2e` refuses to start without it.

The tier is three pipeline files: provision (apply/diff), scenario (run/state), and extract round-trip.
Per-bug contract probes fold onto those tenants.

Each run scaffolds a synthetic single-org company from the packaged `acu config init` templates into a temporary directory, copies the real `.env` into it, and runs the installed `acu` binary from there — no data repo, no pre-existing fixtures on the instance.

Scratch tenants (`E2E`, `E2EA`, `E2EB`, `E2ESCEN`) are created on the way in and always deleted on the way out, so nothing persists.
The packaged full `config init` seed (under `acu-config/`) is the only scaffold.

```sh
gmake e2e                                # whole tier, three files
gmake e2e FILE=test_provision_lifecycle  # apply/diff + folded probes
gmake e2e FILE=test_scenario_lifecycle   # scenario + state + kit alloc
gmake e2e FILE=test_extract_roundtrip    # extract inverse
```

### CLI test-seed soak

This checkout carries a CLI test seed at repo-root `acu-config/` and `acu-scenario/` (no customization YAML under `acu-config/`).
Soak the live CLI tenant from here with `--tenant ACUCLI`.

Never use tenant `CNBN`. Never `cd` the sibling GitOps repo for CLI soak.

`gmake e2e` still scaffolds from packaged `config init` templates into a tmp dir.
That path is not the soak.

```sh
uv run acu --tenant ACUCLI apply acu-config
uv run acu --tenant ACUCLI run acu-scenario
uv run acu --tenant ACUCLI diff acu-config
uv run acu --tenant ACUCLI state acu-config/views
```

## License

This project is licensed under the PolyForm Noncommercial License 1.0.0.
Noncommercial use is free under that license.

Commercial use requires a separate license — contact [lab5.ca](https://lab5.ca).

See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Konstantin Borovik.
