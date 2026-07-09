# Acumatica CLI

**`acu`** — configure Acumatica ERP purely from source code: no UI clicks,
no Configuration Wizard.

Every operation is idempotent:

- `acu apply` upserts records by key — running it twice is safe
- `acu diff` detects drift between your YAML and the live tenant
- `acu provision` takes an empty instance to a fully configured tenant in
  one command, and skips every step that is already done

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
acu provision --id 3 --login DEV        # create → bootstrap → apply → diff
acu diff baseline/                      # later: prove zero drift (exit 2 on drift)
```

## CLI Map

```text
acu [-t TENANT]
│
├── provision --id N --login NAME     one command: create → bootstrap → apply → diff
│             [--type SalesDemo] [--parent N]
│
├── tenant                            tenant CRUD (ac.exe over SSH — control plane)
│   ├── list                          CompanyID, sign-in name, internal CD, type
│   ├── create                        create a tenant and make it automation-ready
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

## Configuration

Configuration is split into three layers, each answering one question:

1. **`acu.yaml`** — *where* to apply. The whole file is the target
   instance: a flat map where `host` is the only required key; everything
   else has a code default matching a stock Acumatica install:

   ```yaml
   host: acu-dev1.vm.internal
   tenant: Company     # sign-in name of the tenant API sessions use
   ```

   Optional overrides (defaults shown) for nonstandard installs,
   split-horizon DNS, port forwards, or jump hosts:

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
  [`docs/ac-exe.md`](docs/ac-exe.md). Used by `acu tenant` and the first step
  of `acu provision`.
- **Data plane (REST):** baseline configuration and reference data through
  the contract-based API (`/entity/Default/25.200.001/`), where `PUT` is a
  keyed upsert — see [`docs/rest-api.md`](docs/rest-api.md). Used by
  `acu apply`, `acu diff`, and `acu schema`.

If you only apply and diff baseline YAML, you never need SSH. SSH setup is
required only for `acu tenant` and `acu provision`.

## SSH setup (control plane)

`acu tenant` and `acu provision` run commands on the Windows guest through
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
