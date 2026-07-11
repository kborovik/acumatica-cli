# Acumatica Config-as-Code CLI

**`acu`** configures Acumatica ERP from YAML files in a git repo. **No UI clicks, no Configuration Wizard.**

The idea is a loop:

1. **Read** the target Acumatica deployment and translate its configuration into YAML — config as code.
2. **Develop** changes in the YAML, in git: edit, review, version.
3. **Re-deploy** with `acu apply`, then prove the live tenant matches the code with `acu diff`.

Three commands do the work, and every one is safe to re-run:

- **`acu tenant create`** — creates a tenant and bootstraps it in one step, ready for `apply`. Re-running it against an existing tenant republishes the bootstrap package instead of failing.
- **`acu apply`** — pushes your YAML into the tenant as keyed upserts. Running it twice changes nothing.
- **`acu diff`** — compares your YAML against the live tenant and exits with code 2 on drift.

> **Tested against** Acumatica ERP **26.101.0225** on Windows Server 2025,
> contract REST endpoint **25.200.001**. Other versions will likely work,
> but only this combination is verified.

## Why

Acumatica configuration normally lives in the web UI: wizards, screens, and
manual data entry that nobody can review, version, or reproduce. `acu` moves
that configuration into YAML files in a git repo, so a tenant can be rebuilt
from scratch, audited in a pull request, and checked for drift like any
other infrastructure.

## Quick start

```sh
uv tool install acumatica-cli

acu config init --host erp.example.com my-erp
cd my-erp                                # edit .env: set ACU_PASSWORD

acu config check                         # read-only preflight
acu tenant create --id 3 --login DEV     # create the tenant + bootstrap it
acu --tenant DEV apply                   # seed bootstrap/, baseline/, setup/
acu --tenant DEV diff                    # prove zero drift (exit 2 on drift)
```

## CLI map

```text
acu [--tenant NAME] [--url URL] [--ssh USER@HOST] [--api-version V]
    [--username U] [--password P]
│
├── tenant                            tenant CRUD (ac.exe over SSH — control plane)
│   ├── list                          CompanyID, sign-in name, internal CD, type
│   ├── create --id N --login NAME    create + bootstrap; re-run to republish
│   │          [--type SalesDemo] [--parent N] [--hidden] [--no-init]
│   └── delete --id N [--yes]         delete the tenant and its data, recycle app pool
│
├── apply [--dry-run] [FILES...]      push YAML via REST (idempotent PUT upserts)
├── diff  [FILES...]                  drift check vs the live tenant (exit 2 on drift)
├── schema [--out DIR]                dump the endpoint's OpenAPI schema (swagger.json)
│
└── config                            configuration ops
    ├── init [--host HOST] [DIR]      scaffold a data repo (.env plus example YAML)
    ├── show                          print the resolved config as a complete .env
    └── check                         read-only preflight: discovery, secrets, REST, SSH
```

`apply` and `diff` called without FILES default to the scaffolded directories, in order: `bootstrap/`, then `baseline/`, then `setup/`. Run `acu <command> --help` for details on any command.

## The data repo

Your configuration lives in its own git repo, which `acu config init` scaffolds:

| Path         | What it holds                                                              |
| ------------ | -------------------------------------------------------------------------- |
| `bootstrap/` | what makes a virgin tenant configurable: features, company, credit terms   |
| `baseline/`  | reference data: subaccounts, chart of accounts, ledger, units of measure   |
| `setup/`     | one-time actions: financial year, master calendar, open periods            |
| `.env`       | where to apply and who signs in, every key an `ACU_*` variable             |

Files in each directory apply alphabetically; the numbered prefixes (`10-`, `20-`, and so on) encode dependency order. The scaffolded `.gitignore` keeps `.env` out of git — store it encrypted (for example as `.env.gpg`) and decrypt once per clone.

## Installation

Requires Python 3.12 or newer.

```sh
uv tool install acumatica-cli
```

`pipx install acumatica-cli` and `pip install acumatica-cli` work too. For the latest development version straight from the main branch:

```sh
uv tool install git+https://github.com/kborovik/acumatica-cli.git
```

Verify with `acu --version`.

## Configuration

Everything lives in one `.env` file: *where* to apply and *who* signs in. Three values are required; everything else has a code default matching a stock Acumatica install:

```sh
ACU_BASE_URL=http://acu-dev1.vm.internal/AcumaticaERP  # required: REST root
ACU_TENANT=LAB5                                        # sign-in name of the tenant API sessions use
ACU_SSH=Administrator@acu-dev1.vm.internal             # required: control-plane user@host
ACU_USER=admin                                         # optional, defaults to admin
ACU_PASSWORD=...                                       # required
```

Worth knowing:

- The file is found by walking up from the current directory, so any subdirectory of the data repo works. Without a `.env`, global flags plus the process environment supply the full configuration.
- Nothing is derived: split-horizon DNS, port forwards, and jump hosts are all handled by writing the address you actually want into the two address keys.
- `acu config show` prints the fully resolved configuration as a complete, valid `.env` — every knob visible, the password excluded. Redirect it to turn resolved state into a working config: `acu config show > .env`.

Verify before touching anything live:

```sh
acu config check       # read-only preflight: discovery, secrets, REST, SSH
acu apply --dry-run    # show what would be written, write nothing
```

## Control and Data Planes

`acu` talks to an instance over two independent channels:

- **Control plane (SSH):** `acu tenant` runs `ac.exe -cm:CompanyConfig` and `sqlcmd` on the Windows guest — see [`docs/ac-exe.md`](docs/ac-exe.md).
- **Data plane (REST):** `acu apply`, `diff`, and `schema` use the contract-based API (`/entity/Default/25.200.001/`), where `PUT` is a keyed upsert — see [`docs/rest-api.md`](docs/rest-api.md).

If you only apply and diff YAML, you never need SSH. SSH setup is required only for `acu tenant`.

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
