# ac.exe — verified CLI reference

Distilled notes on the Acumatica Configuration Wizard CLI, covering only what
this repo uses. **Verified against the 26.101.0225 binary on acu-dev1**
(`ac.exe -?` output) and the official docs, 2026-07-07. Re-verify on version
upgrades — run `ac.exe -?` on the box; the binary is the ground truth.

Sources:
- [Command-Line Tool: Possible Parameters and Values](https://help.acumatica.com/(W(8))/Wiki/Print.aspx?wikiname=HelpRoot_Install&PageID=9e64f453-3f47-4b2b-979b-3b5c22ae1f8b)
- [Command-Line Tool: General Information](https://help.acumatica.com/Help?ScreenId=ShowWiki&pageid=4f9af995-3f23-4da6-a6c0-f6435979c4c4)
- Working `NewInstance` invocation: `acumatica-infra`
  `ansible/roles/acumatica/tasks/instance.yml`

## Basics

- Location: `C:\Program Files\Acumatica ERP\Data\ac.exe` (on the Windows
  guest). Runs elevated; the infra repo runs it as Administrator over SSH.
- `-configmode:<mode>` (`-cm`) selects the operation. Modes in 26.101:
  `NewInstance`, `CompanyConfig`, `DBMaint`, `DBConection`, `DeleteSite`,
  `RenameSite`, `SiteMaint`, `UpgradeSite`, `NewDBLessInstance`,
  `NewTrainingInstance`.
- Booleans accept `True/False` or `Yes/No`.
- `-output:"Forced"` (`-op`) makes it log to stdout — use it in automation.
- Exit code is meaningful; propagate `$LASTEXITCODE` in PowerShell wrappers.
- `-file:"<path>"` (`-f`) points at an XML configuration file exported from
  the Configuration Wizard — the alternative to spelling out flags. Format
  verified 2026-07-09 (decompiled `AcumaticaConfig.ConsoleOptionExtensions`
  in the 26.101 binary, then generated a real file by calling the wizard's
  own `SaveToFile` over PowerShell reflection): root element is literally
  `<Root>`; one child element per option, named by the option's *long* CLI
  name, value in a `Value` attribute (never element text), a decorative
  `Description` attribute the loader ignores. Null-valued options are
  omitted and fall back to built-in defaults on load; unknown elements are
  silently ignored. Tenants ride in a `<company>` element holding one
  `<Company CompanyID=".." Deleted=".." ParentID=".." CompanyType=".."
  Visible=".." LoginName=".."/>` per tenant; RabbitMQ parameters in
  `<rabbitconnparams>`. Two trailing write-only elements
  (`ShortConsoleCommand`, `LongConsoleCommand`) carry the equivalent
  command line as self-documentation. Passwords, if set, are written in
  clear text. The console binary has no save flag — only the GUI wizard
  writes this file. Note this file is **installer parameters only** (DB,
  IIS, instance, tenant list); it contains no in-tenant ERP data.

## Tenant management — `-cm:CompanyConfig`

Adds new tenants or deletes existing ones in an existing instance's database.
Combine with the DB connection flags (`-dbsrvname`, `-dbname`,
`-dbsrvwinauth` / `-dbsrvuser` + `-dbsrvpass`) and one `-company` parameter
per tenant. **Also pass `-dbnew:"False"`** — the flag defaults to `True`
(create database), which is wrong for operating on an existing DB.

**Two undocumented flags are both required** (verified 2026-07-08 on 26.101);
the official CompanyConfig example omits both:

- **`-iname:"<InstanceName>"`** — the tenant scenario resolves the registered
  site by name and dies with `Site with name '' doesn't exist in the system`
  without it.
- **`-h:"<InstancePath>"`** (a.k.a. `-ipath`, the instance's physical folder,
  e.g. `C:\Acumatica\AcumaticaERP`) — without it the *web.config* stage of the
  run throws `System.ArgumentNullException: … path1` inside
  `AcumaticaConfig.Installation.WebCompanyConfig()` and rolls the whole
  operation back. `-iname` alone is not enough. Creating a clean tenant takes
  ~40–60 s.

More verified quirks:

- **`LoginName` lands in `dbo.Company.CompanyKey`, not `CompanyCD`** (verified
  2026-07-08; the 2026-07-07 "it didn't take" note was wrong — I only checked
  CompanyCD). `CompanyCD` is an auto-generated internal code (`Company2`,
  `Company3`, …); `CompanyKey` holds the name you passed (`Scratch`) and **is**
  the value the sign-in page shows and the REST login's `tenant` field
  matches. So `acu tenant list` reads CompanyKey as the login name.
- **The delete sub-key is `Deleted`, not the documented `Delete`** (verified
  2026-07-08). `Delete=Yes` is silently ignored — worse, the run then treats
  the tenant as a data-insert target and *overwrites it*. And a delete must
  carry the **full spec** (`ParentID` + `CompanyType`): with only
  `CompanyID=N;Deleted=Yes;` the preflight (`dataCpnTest` → `CompanyMaint`
  `.CheckAndUpdate`) reads the empty CompanyType as the system tenant and
  aborts with `The system company cannot be removed.`
- **`-aun`/`-aup`/`-auc` preset the tenant's admin login/password.** Passing
  `-aun:"admin" -aup:"<password>" -auc:"False"` on a create makes the new
  tenant's admin usable immediately (no `PasswordChangeOnNextLogin`), so REST
  login works right after the app-pool recycle — no screen-flow first-login
  dance needed. This is what `acu tenant create` does; the `Login.aspx` flow
  (below) is kept only as a fallback for tenants created without these flags.
- Every run logs a harmless `InitializeOEMVersion error` /
  `Could not find assembly 'mscorlib'` line at startup — it appears on exit-0
  runs too; ignore it.
- The running site does **not** see a newly created tenant (sign-in page and
  REST tenant selection both miss it) — the tenant map loads at app start. An
  `AcumaticaERP` app-pool recycle after CompanyConfig fixes it (**verified
  2026-07-08**: `Restart-WebAppPool -Name AcumaticaERP` made tenant 3 appear
  on the sign-in page and route over REST). `acu tenant create` must run this
  recycle itself after `ac.exe` returns. See `rest-api.md` for the
  tenant-selection foot-gun that skipping the recycle causes.
- Without the `-aup` admin-preset flags above, a newly created clean tenant
  seeds `admin` with password `setup` and `PasswordChangeOnNextLogin = 1`,
  which the contract REST endpoint can't clear (**verified 2026-07-08** — see
  `rest-api.md`); first login then has to go through the `Login.aspx` form
  flow. Presetting the password with `-aup` is the simpler route and what
  `acu tenant create` uses.

`-company:"key=value;key=value;"` sub-keys (official list):

| Key | Meaning |
|---|---|
| `CompanyID` | Integer tenant ID. ID 1 is the hidden system/parent tenant; real tenants start at 2. Reference an existing ID to modify it. |
| `ParentID` | Parent tenant ID — `1` for normal tenants. Pass it on delete too (see below). |
| `Visible` | `Yes` → tenant appears on the sign-in page. |
| `LoginName` | Tenant name — lands in `CompanyKey`, shown on the sign-in page and matched by the REST login `tenant` field. Domain-style names (dots and hyphens, e.g. `lab5.ca-dev1`) are accepted end to end — verified 2026-07-08. Keep to alphanumerics + `.` + `-`; never `;` or `=` (the `-company` string's own delimiters). |
| `CompanyType` | Data set inserted at creation: empty = clean tenant, `SalesDemo` = demo data. (Official doc text mislabels this as a boolean; the working values are dataset names — verified by the infra deployment.) Also required on delete. |
| `Deleted` | `Yes` → delete this tenant. **Note the `d`** — the officially documented `Delete` is silently ignored (and turns the run destructive). |

Example — add a visible clean tenant with ID 3, admin ready to use, then
delete it (both verified 2026-07-08):

```powershell
# create — -h and the -aup admin preset are what make it automation-ready
& 'C:\Program Files\Acumatica ERP\Data\ac.exe' -configmode:"CompanyConfig" -output:"Forced" `
  -iname:"AcumaticaERP" -h:"C:\Acumatica\AcumaticaERP" `
  -dbsrvname:"(local)" -dbsrvwinauth:"True" -dbname:"AcumaticaDB" -dbnew:"False" `
  -company:"CompanyID=3;ParentID=1;Visible=Yes;LoginName=Tenant2;" `
  -aun:"admin" -aup:"<password>" -auc:"False"

# delete — full spec (ParentID + CompanyType), sub-key is Deleted
& 'C:\Program Files\Acumatica ERP\Data\ac.exe' -configmode:"CompanyConfig" -output:"Forced" `
  -iname:"AcumaticaERP" -h:"C:\Acumatica\AcumaticaERP" `
  -dbsrvname:"(local)" -dbsrvwinauth:"True" -dbname:"AcumaticaDB" -dbnew:"False" `
  -company:"CompanyID=3;ParentID=1;CompanyType=Custom;Deleted=Yes;"
```

### Available `CompanyType` datasets (verified on the box)

The insert-data sets are the folders under
`C:\Program Files\Acumatica ERP\Database\Data`: `SalesDemo`, `System`, `T100`,
`U100`. T100/U100 are Acumatica University training datasets (579+ table XMLs
including AP invoices and payments — transactional data, not clean baselines).
None of them is a bootstrap shortcut for a clean client baseline.

## Instance creation — `-cm:NewInstance`

Owned by `acumatica-infra` (see `instance.yml` for the full working command:
DB + tenants + IIS site in one shot). Kept out of scope here; this repo
assumes the instance exists.

## Hidden data verbs: `export` / `import` — verified 2026-07-09

`ac.exe -?` does not list them, but the 26.101 binary dispatches a second
command family when the first argument is a bare verb instead of a flag
(found by decompiling `ConfigStart.opcodeHandlers`): `database`, `export`,
`import`, `xmlentity`, `delta`, `util`, `mobilesitemap`, and others. The
interesting pair for config-as-code:

```text
ac.exe export <adb|csv|xml> <datasource-url> <folder>   # dump one tenant's data
ac.exe import <adb|xml> <folder> <datasource-url>       # load it back
```

The datasource URL is `<proto>://<connection-string>?<params>` where proto
is `mssql` (also `sql`, `mysql`, `pgsql`, `file`) and params include
`companyid=N` (or `companycd=`, `parentid=`, `schema=`, `timeout=`).
Live-verified export on acu-dev1:

```powershell
& 'C:\Program Files\Acumatica ERP\Data\ac.exe' export xml `
  'mssql://Server=(local);Database=AcumaticaDB;Integrated Security=SSPI?companyid=2' `
  "$env:TEMP\company2-export"   # exit 0
```

Output: one XML file per table that has rows for that tenant (a near-empty
tenant produced 10 files; a `SalesDemo` tenant would produce hundreds).
The format is exactly the shipped-dataset format under
`C:\Program Files\Acumatica ERP\Database\Data\<DataSet>\` — a `<data>`
document with a typed `<table><col .../></table>` schema header followed by
`<rows><row Attr="value" .../></rows>`. In other words, `SalesDemo` *is* an
`ac.exe export xml` dump, and `CompanyType` at tenant creation is the
import path for it.

Caveats before treating this as an editing surface: rows are raw table
data — internal integer IDs cross-reference across files, and imports
bypass all business logic (no defaults, validations, or side effects run,
unlike REST). The schema header is version-coupled to the build. `import`
is **unverified** (it mutates; test on a scratch tenant first).

## Snapshots: NOT supported by ac.exe — verified finding

Neither the 26.101 binary help nor the current parameter docs expose any
snapshot save/restore operation. The Part 1 spec's assumption ("ac.exe
creates the tenant, then load a snapshot" with a CLI flag) **does not hold**.
Options, in order of preference for this repo:

1. **Reference data as code (primary)** — seed config through the REST API
   (see [rest-api.md](rest-api.md)); fully scriptable, diffable, no snapshot
   needed for the baseline.
2. **Snapshot via the Tenants screen (SM203520)** — supported but UI-driven
   (create/restore/import/export). Community attempts to automate it through
   extended endpoints invoking the screen's actions are reported unreliable
   ([community thread](https://community.acumatica.com/develop-customizations-288/snapshot-automation-32047)).
   Treat snapshots as a manual backup/restore convenience, not an automation
   building block.

The deprecated `PXInstanceHelper` API (`CreateTenant`/`LoadSnapshot`) is gone
in current builds — do not reference it.

## Prior art

- [Acumatica/AcuCustomizationUtil](https://github.com/Acumatica/ACUCustomizationUtil) — `acu` CLI (customization-package centric)
