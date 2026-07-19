# Acumatica GitOps (Config-as-Code)

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
cd my-erp                                # edit .env: set ACU_PASSWORD
                                         # keep ACU_API_VERSION in sync with target.yaml

acu config check                         # read-only preflight (incl. target.yaml)
acu tenant create --id 3 --login DEV     # create the tenant + bootstrap it (needs SSH)
acu --tenant DEV apply                   # seed bootstrap/, baseline/, setup/
acu --tenant DEV diff                    # prove zero drift (exit 2 on drift)
```

**Hosted Acumatica (no SSH):** the tenant already exists; leave `ACU_SSH` blank.

```sh
acu config init --host customer.acumatica.com my-erp
cd my-erp                                # edit .env: ACU_TENANT, ACU_PASSWORD; ACU_SSH=
acu config check                         # REST preflight; ssh probe is skipped
acu --tenant DEV bootstrap               # publish AcuBootstrap via REST only
acu --tenant DEV apply
acu --tenant DEV diff
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
├── extract [--out DIR] [--only NAME]... [--force] [--dry-run]
│                                     dump live tenant state as seed YAML (inverse of apply)
├── schema [--out DIR]                dump the endpoint's OpenAPI schema (swagger.json)
│
└── config                            configuration ops
    ├── init [--host HOST] [DIR]      scaffold a data repo (.env, target.yaml, example YAML)
    ├── show                          print the resolved config as a complete .env
    └── check [--strict]              preflight: discovery, secrets, target, REST, endpoints, SSH
```

`apply` and `diff` called without FILES default to the scaffolded directories, in order: `bootstrap/`, then `baseline/`, then `setup/`.
`run` called without FILES defaults to `scenario/`.
`acu --completion` emits a completion script for bash, zsh, or fish — source it from your shell profile.
Run `acu <command> --help` for details on any command.

## The data repo

Your configuration lives in its own git repo. `acu config init` scaffolds everything except `scenario/`, which you author by hand:

| Path          | What it holds                                                              |
| ------------- | -------------------------------------------------------------------------- |
| `bootstrap/`  | what makes a virgin tenant configurable: features, company, credit terms   |
| `baseline/`   | reference data: subaccounts, chart of accounts, ledger, units of measure   |
| `setup/`      | one-time actions: financial year, master calendar, open periods            |
| `scenario/`   | transaction scenarios for `acu run`: purchase, build, sell flows            |
| `target.yaml` | committed verified matrix: `erp` + `default_api` (what, not where)         |
| `.env`        | where to apply and who signs in, every key an `ACU_*` variable             |

Files in each directory apply alphabetically; the numbered prefixes (`10-`, `20-`, and so on) encode dependency order.
The scaffolded `.gitignore` keeps `.env` out of git — store it encrypted (for example as `.env.gpg`) and decrypt once per clone.
Commit `target.yaml` with the seeds so every clone knows the verified ERP line and Default API generation.

Seed YAML in `bootstrap/`, `baseline/`, and `setup/` is state: `apply` upserts it, `diff` proves it.
Scenario YAML is different — it describes transactions that flow forward.
`acu run` executes each step in order (`put`, `action`, `wait`, `get`), captures server-assigned document numbers into `${var}` references for later steps, and checks `expect:` assertions as deltas against a pre-run snapshot, so a scenario re-runs safely on a warm tenant.

### Seed `endpoint:` symbols

Dual-served entities (on both Bootstrap and Default) need an explicit `endpoint:` line.

| Value | Resolves to |
| ----- | ----------- |
| omitted | `Default/<ACU_API_VERSION>` for Default-only entities |
| `bootstrap` | active `Bootstrap/<ver>` from `bootstrap/project.xml` or the packaged contract |
| `default` | `Default/<ACU_API_VERSION>` — tracks the operator API version |
| `Bootstrap/1.9.0` or `Default/25.200.001` | literal pin (ignores `ACU_API_VERSION` for Default) |

Prefer symbolic `default` over a pinned `Default/25.200.001` so the seed tree travels with the configured API generation.

## Installation

Requires Python 3.14 or newer.

```sh
uv tool install acumatica-cli
```

`pipx install acumatica-cli` and `pip install acumatica-cli` work too. For the latest development version straight from the main branch:

```sh
uv tool install git+https://github.com/kborovik/acumatica-cli.git
```

Verify with `acu --version`.

## Configuration

Everything lives in one `.env` file: *where* to apply and *who* signs in.
Three values are required; everything else has a code default matching a stock Acumatica install:

```sh
ACU_BASE_URL=http://acu-dev1.vm.internal/AcumaticaERP  # required: REST root
ACU_TENANT=LAB5                                        # sign-in name of the tenant API sessions use
ACU_SSH=Administrator@acu-dev1.vm.internal             # optional: control-plane user@host (tenant CRUD)
ACU_API_VERSION=25.200.001                             # Default contract version half only
ACU_USER=admin                                         # optional, defaults to admin
ACU_PASSWORD=...                                       # required for live commands
```

`ACU_API_VERSION` is the version half only (`25.200.001`), never `Default/25.200.001`.
A full path would nest as `/entity/Default/Default/...`.

The committed `target.yaml` next to `.env` declares the verified matrix (what, not where):

```yaml
erp: "26.101.0225"           # claimed product line/build (README-level detail)
default_api: "25.200.001"    # must match ACU_API_VERSION
```

When `target.yaml` is present, `apply` / `diff` / `run` / `extract` / `schema` / `bootstrap` hard-fail if `default_api` does not match the configured API version.
`acu config check` reports the same match as a probe line.
Missing `target.yaml` only warns on check unless you pass `--strict`.

Worth knowing:

- The file is found by walking up from the current directory, so any subdirectory of the data repo works.
- Without a `.env`, global flags plus the process environment supply the full configuration.
- `ACU_SSH` is optional.
- Leave it blank on hosted instances; only `acu tenant` needs it.
- Nothing is derived: split-horizon DNS, port forwards, and jump hosts are all handled by writing the address you actually want into the address keys.
- `acu config show` prints the fully resolved configuration as a complete, valid `.env` — every knob visible, the password excluded.
- When `target.yaml` is present, `config show` also comments `erp` / `default_api`.
- Redirect it to turn resolved state into a working config: `acu config show > .env`.

Verify before touching anything live:

```sh
acu config check           # discovery, secrets, target, REST, endpoints, SSH
acu config check --strict  # missing target.yaml becomes fail
acu apply --dry-run        # show what would be written, write nothing
```

## Control and Data Planes

`acu` talks to an instance over two independent channels:

- **Control plane (SSH):** `acu tenant` runs `ac.exe -cm:CompanyConfig` and `sqlcmd` on the Windows guest — see [`docs/ac-exe.md`](docs/ac-exe.md).
- **Data plane (REST):** `acu bootstrap`, `apply`, `diff`, `run`, `extract`, and `schema` use the contract-based API and CustomizationApi — see [`docs/rest-api.md`](docs/rest-api.md).

If you never touch `acu tenant`, you never need SSH.
On a hosted instance, publish AcuBootstrap with `acu bootstrap`, then seed with `apply`.
`acu bootstrap --export PATH` writes the package zip for manual import on the Customization Projects screen (SM204505) when you cannot call the API.

## SSH setup (control plane)

`acu tenant` runs commands on the Windows guest through plain `ssh`. Two things about this setup are not obvious, and both are hard requirements.

**1. The default SSH shell on the Windows guest must be PowerShell.**
`acu` sends PowerShell syntax over the wire, and every one of those commands fails under `cmd.exe`, the Windows OpenSSH default. Switch it once, in an elevated PowerShell on the guest:

```powershell
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -PropertyType String -Force
```

**2. Authentication must be key-based and non-interactive.**
`acu` connects with `BatchMode=yes`, so it will never answer a password prompt.
Because the default user is `Administrator` (an administrators-group member), Windows OpenSSH reads the key from the *machine-wide* file `C:\ProgramData\ssh\administrators_authorized_keys` — **not** from `~\.ssh\authorized_keys` like on Linux. On the guest:

Install + start the server (once):

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd
```

Authorize your public key for administrators:

```powershell
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys -Value "ssh-ed25519 AAAA... you@laptop"
```

The file must be readable by SYSTEM/Administrators only, or sshd ignores it:

```powershell
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F"
```

Then verify from your workstation — this one test proves both requirements at once (key auth works, and the shell is PowerShell):

```sh
ssh -o BatchMode=yes Administrator@acu-dev1.vm.internal '$PSVersionTable.PSVersion'
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

### Live end-to-end tier

`gmake e2e` runs the opt-in live tier against a real Acumatica instance (pytest marker `e2e`, deselected by the default suite).

Configuration is one file: a decrypted `.env` at the repo root names the instance — `ACU_BASE_URL`, `ACU_SSH`, `ACU_TENANT`, `ACU_PASSWORD`.
`gmake e2e` refuses to start without it.

The tier is self-contained.
Each run scaffolds a synthetic single-org company from the packaged `acu config init` templates into a temporary directory, copies the real `.env` into it, and runs the installed `acu` binary from there — no data repo, no pre-existing fixtures on the instance.
Scratch tenants (`E2E`, `E2EA`, `E2EB`) are created on the way in and always deleted on the way out, so nothing persists.

```sh
gmake e2e                                # whole tier, about 20 minutes
gmake e2e FILE=test_provision_lifecycle  # one file, by stem or path
```

## License

This project is licensed under the Apache License, Version 2.0.
See [LICENSE](LICENSE) and [NOTICE](NOTICE).

Copyright 2026 Konstantin Borovik.
