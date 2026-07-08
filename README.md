# acumatica-cli

**`acu`** — configure Acumatica ERP purely from source code: no UI clicks, no
Configuration Wizard. Every operation is idempotent — `apply` upserts records
by key, `diff` detects drift (exit 1) — so re-runs converge to the same
state. Definition of done: clone a data repo, decrypt, run one command, get a
byte-identical configured tenant every time.

```sh
acu tenant list                        # tenants on the instance
acu tenant create --id 3 --login T2    # create → app-pool recycle → login-ready
acu apply baseline/                    # YAML → keyed REST upserts (idempotent)
acu diff baseline/                     # live vs source; exit 1 on drift
acu schema                             # dump OpenAPI schema → schemas/
```

Two planes, deliberately separate:

- **Control plane (SSH):** tenant CRUD via `ac.exe -cm:CompanyConfig` and
  `sqlcmd` on the Windows guest — see [`docs/ac-exe.md`](docs/ac-exe.md)
- **Data plane (REST):** baseline config and reference data through the
  contract-based API (`/entity/Default/25.200.001/`), where `PUT` is a keyed
  upsert — see [`docs/rest-api.md`](docs/rest-api.md)

## Tool vs data

This repo is the **tool only**. The data it applies — what to configure
(`baseline/*.yaml`), where (`acu.toml`), and encrypted credentials
(`.env.gpg`) — lives in separate, self-contained **data repos**;
[`acumatica-baseline`](../acumatica-baseline) is the first. `acu` discovers
the data repo by walking up from the current directory to `acu.toml`, so
day-to-day apply/diff runs from there: `make install` in the data repo puts
`acu` on PATH (`uv tool install --editable`), then `make decrypt / apply /
diff`.

C# customization projects are out of scope. This tool covers everything
between "the instance exists" and "the tenant is configured."

## Where it sits

Infrastructure (KVM Windows VMs, SQL Server, IIS, the Acumatica instance
itself) is built by [`acumatica-infra`](../acumatica-infra) — that repo ends
with a running, empty instance. This tool starts there:

| Layer | Repo |
|---|---|
| Host, VM, SQL Server, IIS, Acumatica instance | `acumatica-infra` |
| The `acu` CLI (tenant + config tooling) | `acumatica-cli` (this repo) |
| Baseline config, reference data, instance targets, secrets | `acumatica-baseline` |

First target instance: **acu-dev1** — `http://acu-dev1.vm.internal/AcumaticaERP`
(Acumatica 26.101.0225, reachable over the tailnet; DNS via the infra repo's
split-DNS `network` role).

## Layout

- `src/acumatica_cli/` — the `acu` CLI (uv project): `tenant list|create|delete`
  (ac.exe over SSH), `apply` (baseline YAML → REST upserts), `diff` (drift
  check, exit 1 on drift), `schema` (OpenAPI schema dump)
- `docs/` — verified references; `schemas/` — dumped OpenAPI schemas
  (`acu schema`, gitignored)
- `journal/` — the engineering record: every problem, friction, and dead end
- `Makefile` — dev loop: `make install / check` (lint + offline tests)

```sh
make install                   # deps (uv sync)
make check                     # ruff + basedpyright + offline pytest
```

The SSH layer requires the Acumatica box; the REST layer runs from anywhere
that can reach the API. The test suite needs neither — SSH and REST are both
mocked, fully offline.

## Status

Working skeleton, verified against acu-dev1: tenant listing, idempotent
apply, and zero-drift diff all pass end-to-end (`baseline/uoms.yaml`).
Verified findings so far:

- [`docs/ac-exe.md`](docs/ac-exe.md) — CLI reference for tenant operations;
  ac.exe has **no snapshot support**
- [`docs/rest-api.md`](docs/rest-api.md) — auth flow, entity coverage;
  payment terms missing from the Default endpoint; a clean tenant needs a
  **company/branch bootstrap** before most entities respond

## Requirements

- A running Acumatica instance from `acumatica-infra` (e.g. acu-dev1)
- Network path to it (Tailscale / the `vm.internal` split-DNS zone)
- API credentials for the tenant (`admin` user; not stored in this repo)
