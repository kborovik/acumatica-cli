# Acumatica CLI

**`acu`** — configure Acumatica ERP purely from source code: no UI clicks,
no Configuration Wizard.

Every operation is idempotent:

- `acu apply` upserts records by key — running it twice is safe
- `acu diff` detects drift between your YAML and the live tenant
- `acu tenant create` bootstraps the new tenant at birth, so it is ready
  for `acu apply` in one step

> **Development environment.** All development and live verification is done
> against Acumatica ERP **26.101.0225** running on **Windows Server 2025**,
> using the contract-based REST API endpoint **`Default/25.200.001`**. Other
> versions will likely work, but only this combination is tested.

## Why?

Acumatica configuration normally lives in the web UI: wizards, screens, and
manual data entry that nobody can review, version, or reproduce. `acu` moves
that configuration into YAML files in a git repo, so a tenant can be rebuilt
from scratch, audited in a pull request, and checked for drift like any other
infrastructure.

## Quick start

From the data repo:

```sh
acu config show     # sanity-check the resolved target
```

```sh
acu tenant create --id 3 --login DEV    # create the tenant + bootstrap it
acu --tenant DEV apply                  # seed bootstrap/, baseline/, setup/
acu --tenant DEV diff                   # prove zero drift (exit 2 on drift)
```

## CLI Map

```text
acu [-t TENANT]
│
├── tenant                            tenant CRUD (ac.exe over SSH — control plane)
│   ├── list                          CompanyID, sign-in name, internal CD, type
│   ├── create --id N --login NAME    create a tenant + bootstrap it, ready to apply
│   │          [--type SalesDemo] [--parent N] [--hidden] [--no-init]
│   └── delete                        delete a tenant and its data, recycle app pool
│
├── apply [--dry-run] FILES...        seed baseline YAML via REST (idempotent PUT upserts)
├── diff FILES...                     drift check: YAML vs live tenant (exit 2 on drift)
├── schema [--out DIR]                dump the endpoint's OpenAPI schema (swagger.json)
│
└── config                            local, read-only — never talks to a live instance
    └── show                          print the resolved target as a complete acu.yaml
```

The target instance comes from `acu.yaml` (found by walking up from the
current directory); the one global option `-t/--tenant` overrides the tenant
that API sessions sign in to. Run `acu <command> --help` for details on any
command.

## Installation

`acu` requires Python 3.12 or newer. Pick one of the options below — each
code block is a complete, standalone command.

### From PyPI (recommended)

With [uv](https://docs.astral.sh/uv/), installed as an isolated tool:

```sh
uv tool install acumatica-cli
```

With pipx:

```sh
pipx install acumatica-cli
```

With plain pip:

```sh
pip install acumatica-cli
```

### From the GitHub repository

Install the latest development version straight from the main branch:

```sh
uv tool install git+https://github.com/kborovik/acumatica-cli.git
```

Or with pip:

```sh
pip install git+https://github.com/kborovik/acumatica-cli.git
```

Verify the installation:

```sh
acu --help
```

## Configuration

Configuration is split into three layers, each answering one question:

1. **`acu.yaml`** — *where* to apply. The whole file is the target
   instance: a flat map with two required keys, one explicit address per
   plane — `base_url` (the REST root: scheme, host, and site path) and
   `ssh` (the control-plane `user@host`). Everything else has a code
   default matching a stock Acumatica install:

   ```yaml
   base_url: http://acu-dev1.vm.internal/AcumaticaERP
   ssh: Administrator@acu-dev1.vm.internal
   tenant: Company     # sign-in name of the tenant API sessions use
   ```

   Nothing is derived: split-horizon DNS, port forwards, and jump hosts
   are all handled by writing the address you actually want into the two
   required keys.

   `acu config show` prints the fully resolved instance as a complete,
   valid `acu.yaml` — every knob visible, credentials excluded. To turn
   the resolved state into your working config, redirect and edit:

   ```sh
   acu config show > acu.yaml
   ```

2. **`.env`** — *who* signs in to the REST API. Loaded from the data-repo
   root (or the plain environment):

   ```sh
   ACU_PASSWORD=...    # required
   ACU_USER=admin      # optional, defaults to admin
   ```

   The data repo stores it encrypted as `.env.gpg`; `make decrypt` produces
   the plaintext `.env` once per clone.

3. **SSH** — *how* the control plane reaches the Windows guest. Configured on
   the guest and in your `~/.ssh`, not in `acu.yaml` — see the next section.

Verify the result before touching anything live:

```sh
acu config show           # the fully resolved instance as acu.yaml, no secrets
acu apply --dry-run baseline/
```

## Two planes

`acu` talks to an instance over two independent channels:

- **Control plane (SSH):** tenant create/delete/list via `ac.exe
  -cm:CompanyConfig` and `sqlcmd` on the Windows guest — see
  [`docs/ac-exe.md`](docs/ac-exe.md). Used by `acu tenant`.
- **Data plane (REST):** baseline configuration and reference data through
  the contract-based API (`/entity/Default/25.200.001/`), where `PUT` is a
  keyed upsert — see [`docs/rest-api.md`](docs/rest-api.md). Used by
  `acu apply`, `acu diff`, and `acu schema`.

If you only apply and diff baseline YAML, you never need SSH. SSH setup is
required only for `acu tenant`.

## SSH setup (control plane)

`acu tenant` runs commands on the Windows guest through
plain `ssh`. Two things about this setup are not obvious, and both are hard
requirements:

**1. The default SSH shell on the Windows guest must be PowerShell.**
`acu` sends PowerShell syntax over the wire (`& 'C:\...\ac.exe' ...`,
`Import-Module WebAdministration`, `exit $LASTEXITCODE`). Windows OpenSSH
ships with `cmd.exe` as the default shell, where every one of those commands
fails. Switch it once, in an elevated PowerShell on the guest:

```powershell
New-ItemProperty -Path "HKLM:\SOFTWARE\OpenSSH" -Name DefaultShell `
  -Value "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe" `
  -PropertyType String -Force
```

**2. Authentication must be key-based and non-interactive.**
`acu` connects with `BatchMode=yes`, so it will never answer a password
prompt — without a working key the connection just fails. And because the
default `ssh_user` is `Administrator` (an administrators-group member),
Windows OpenSSH reads the key from the *machine-wide* file
`C:\ProgramData\ssh\administrators_authorized_keys` — **not** from
`~\.ssh\authorized_keys` like on Linux. On the guest:

```powershell
# install + start the server (once)
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Set-Service sshd -StartupType Automatic
Start-Service sshd

# authorize your public key for administrators
Add-Content -Path C:\ProgramData\ssh\administrators_authorized_keys Value "ssh-ed25519 AAAA... you@laptop"

# the file must be readable by SYSTEM/Administrators only, or sshd ignores it
icacls C:\ProgramData\ssh\administrators_authorized_keys /inheritance:r /grant "Administrators:F" /grant "SYSTEM:F" 
```

Then verify from your workstation — this one test proves both requirements
at once (key auth works, and the shell is PowerShell):

```sh
ssh -o BatchMode=yes Administrator@acu-dev1.vm.internal '$PSVersionTable.PSVersion'
```

A version table means you are ready; a password prompt or
`'$PSVersionTable.PSVersion' is not recognized...` means requirement 2 or 1
(respectively) is not met yet. Host aliases, jump hosts, and ports go in your
`~/.ssh/config` as usual, or set `ssh: user@alias` in `acu.yaml`.
