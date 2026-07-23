# Contract-based REST API — verified notes

Working notes for the reference-data-as-code layer. **Verified against
acu-dev1 (Acumatica 26.101.0225) on 2026-07-07.**

## Endpoint

Base: `http://acu-dev1.vm.internal/AcumaticaERP`

Newest Default contract endpoint on this build (from `GET /entity`):

```
/AcumaticaERP/entity/Default/25.200.001/
```

### `GET /entity` — endpoint list (config check probe)

Authenticated `GET /entity` returns the published contract endpoints.
Parse shape (vendor contract + offline fixtures; re-verify on upgrade, V12):

```json
[
  {
    "name": "Default",
    "version": "25.200.001",
    "href": "http://…/entity/Default/25.200.001/"
  },
  {
    "name": "Bootstrap",
    "version": "1.0.0",
    "href": "http://…/entity/Bootstrap/1.0.0/"
  }
]
```

`acu config check` requires a `Default` row whose `version` equals
`Instance.api_version` (exact string). Unparseable body → fail-closed
with status, content-type, and a short raw hint — never silent skip.

### Live ERP build probe (not available)

`target.yaml` field `erp` is **claimed only**.
No stable HTTP surface is verified for the ERP product build id
(e.g. `26.101.0225`) that is safe for `config check` without SSH.
Until such a probe is re-verified (V12), check emits:

```text
skip erp (live probe not available; claimed …)
```

Do not add an SSH/sqlcmd path for this — control plane stays tenant CRUD
only (V1). Full build detail may still appear in README prose.

The instance's own OpenAPI 3.0.1 schema is the authoritative field-level
reference — dump it with `acu schema`
into `schemas/` (gitignored; ~3 MB, regenerate rather than version).

## Authentication (cookie session)

```
POST /entity/auth/login    {"name":"admin","password":"...","tenant":"Company"}  → 204 + cookies
...work with the cookie jar...
POST /entity/auth/logout   (empty body)                                          → 204
```

Verified quirks:

- **Tenant routing validates the name only on a multi-tenant instance with a
  current tenant map (verified 2026-07-08, revised 2026-07-09).** Send an
  explicit, valid tenant name; do not omit it or pass an empty string. The
  full behavior matrix (all cells verified live on 26.101.0225):
    - multi-tenant, fresh map, unknown non-empty name (e.g.
      `DefinitelyBogus`) → **500 `PXUndefinedCompanyException`** ("A proper
      company ID cannot be determined"). No silent fallback.
    - multi-tenant, fresh map, valid name → routes to *that* tenant and
      enforces *that tenant's* credentials.
    - omitted / `""` → 204 on the **default tenant** (expected Acumatica
      semantics — never rely on it).
    - **single login-able tenant in the DB → ANY tenant value returns 204**
      and lands on that tenant. No validation at all (verified 2026-07-09:
      after deleting the second tenant and recycling, its old name and pure
      nonsense both logged in cleanly). The "strict" behavior above only
      exists when there are two or more tenants to choose between.
    - **stale map (tenant created or deleted without a recycle) → named
      logins silently land on the default tenant** — the B5 false-green
      `diff`. On 2026-07-07 marker UOM rows written under four names all hit
      CompanyID 2 this way.
  Lesson: a 204 from login proves nothing about *where* the session landed.
  Wrong-tenant writes and false-green diffs are only prevented by verifying
  the landed tenant after login (next bullet).
- **Landed-tenant probe (T21, verified 2026-07-09).** The contract API
  surface exposes nothing tenant-identifying (no cookie carries the tenant;
  `GET /entity` and `/CustomizationApi/getPublished` are landing-invariant;
  the `/Frames` page `<title>` is the tenant's internal `CompanyCD`, not its
  login name; `/Main`'s company markup and the `screenLink` `CompanyID`
  query param both disappear on a single-tenant instance). What works: an
  **authenticated `GET /Frames/Login.aspx`** renders a hidden input
  `id="txtSingleCompany"` whose value is the **landed tenant's login name in
  every observed state** — multi-tenant fresh map, single-tenant, and
  mid-reroute under a stale map — and the GET does not disturb the session.
  `AcumaticaClient.__enter__` parses it and refuses the session (after
  logging out) when it does not match the requested tenant. Re-verify the
  page shape on upgrade: the guard fails closed if the input disappears.
- **`Restart-WebAppPool` returns before the new worker serves (overlapped
  recycle, observed 2026-07-09).** Requests fired immediately after the
  recycle can still hit the old worker and its old tenant map for tens of
  seconds. `acu tenant create`'s login-verification loop absorbs this;
  hand-rolled probes right after a recycle must wait or retry.
- **Newly created tenants are invisible until an app-pool recycle (verified
  2026-07-08).** After `ac.exe` added tenant 3 the sign-in page did not list
  it and no tenant value could select it — the app's tenant map loads at
  startup. Recycling the `AcumaticaERP` app pool
  (`Restart-WebAppPool -Name AcumaticaERP`) fixed it: the tenant then appears
  on the sign-in page and routes over REST. `acu tenant create` must perform
  this recycle itself after `ac.exe` returns.
- **First-login password change is a bootstrap step the contract API can't do
  (verified 2026-07-08).** A freshly created clean tenant seeds `admin` /
  **`setup`** with `PasswordChangeOnNextLogin = 1` (in `dbo.Users`). The
  contract login authenticates but refuses to clear the flag: passing
  `newPassword` with `setup` still returns *"You need to change your password.
  Sign in by using your browser to set a new password."* Repeated wrong
  passwords stamp `LockedOutDate` and lock the account. ("Login is not a
  blocker" from 2026-07-07 applied only because those probes hit tenant 2,
  which is past first login.)
- **…but the create path sidesteps it, and the screen flow is a fallback
  (verified 2026-07-08).** `acu tenant create` presets the admin password via
  ac.exe's `-aun`/`-aup`/`-auc` flags (see `ac-exe.md`), so the tenant is
  REST-loginable straight after the recycle with no first-login step at all.
  The `Login.aspx` WebForms dance below is only needed for a tenant that was
  created without those flags — it works from plain httpx, no browser:
    1. `GET /Frames/Login.aspx` — collect the hidden fields (`__VIEWSTATE`,
       `__VIEWSTATEGENERATOR`, …) and the session cookie.
    2. `POST` the same URL with those fields plus
       `ctl00$phUser$cmbCompany=<tenant>`, `ctl00$phUser$txtUser=admin`,
       `ctl00$phUser$txtPass=setup`, `ctl00$phUser$btnLogin=Sign In` — the
       response re-renders the page with `ctl00$phUser$txtNewPassword` and
       `ctl00$phUser$txtConfirmPassword` visible.
    3. `POST` again with old password + both new-password fields filled —
       redirects to `/Main` (signed in, flag cleared). REST login with the
       new password then returns 204.
  Traps: submit `ctl00$phUser$btnLogin` (the `mfLoginButton` on the page is
  the multi-factor flow); the `isReal` login-JS flag is client-side only, a
  plain postback is accepted; and the page always contains hidden error divs
  ("Last update was unsuccessful", "customization failed to apply") — they
  are static markup, not a response to your POST. This is the
  `acu tenant create` route: recycle app pool → screen-flow password change →
  tenant is automation-ready.
- Logout with curl needs an explicit `Content-Length: 0` header or IIS
  answers `411 Length Required`.
- **Always log out.** Sessions count against the license's concurrent API
  user limit; leaked sessions exhaust it (trial instances have a small cap).
- Credentials live outside the repo (env vars `ACU_URL`, `ACU_USER`,
  `ACU_PASSWORD`, `ACU_TENANT`).

## Entity coverage for the Part 1 baseline

Checked against the dumped schema — every entity below supports
`GET`/`PUT`/`PATCH` (PUT is upsert: keyed by the entity's key fields, so
re-runs are naturally idempotent):

| Config object (spec) | Default 25.200.001 entity |
|---|---|
| Chart of accounts | `Account` (+ `Subaccount`, `Ledger`) |
| Customer classes | `CustomerClass` |
| Vendor classes | `VendorClass` |
| Tax zones / categories | `TaxZone`, `TaxCategory`, `Tax` |
| Item classes | `ItemClass` |
| UOMs | `UnitsOfMeasure` |
| Currencies | `Currency` |
| Branches / company structure | `CompaniesStructure` |
| Financial calendar | `FinancialYear`, `FinancialPeriod` |

**Verified gap — payment terms.** The Default endpoint has no Credit Terms
(CS206500) entity (only `ShippingTerm`, which is different). Decision
(2026-07-07): the bootstrap customization package (below) will carry a custom
endpoint that exposes Credit Terms alongside the bootstrap screens — one
package closes both gaps.

## Verified: no tenant works before bootstrap — and the API can't bootstrap alone

Scratch-tenant experiment, 2026-07-07. Attribution note: because tenant 3 was
not yet loaded into the app (the recycle hadn't run), every probe in this
experiment actually ran against **tenant 2 (`Company`)** — which turned out to
be just as unconfigured as the freshly created tenant 3 (`FeaturesSet` empty
for all tenants; explicit `tenant: Company` probe confirmed identical
failures). The instance has never been through initial setup, so bootstrap is
required for *every* tenant, not just new ones. The uoms.yaml proof worked
only because CS203500 has no setup dependency. **Re-run done (2026-07-08):**
after the recycle + screen-flow password change, the probe against the real
tenant 3 returned an identical map — same three classes, `CompaniesStructure`
still dead, CustomizationApi 200 ("no published projects"). The findings
below stand on a genuinely virgin tenant.

Entity access on an unconfigured tenant falls into three classes:

| Class | Observed entities | Error |
|---|---|---|
| Works | `UnitsOfMeasure` (ships with defaults), `FinancialYear` (0 records) | — |
| Setup chain missing | `Account`, `Currency` (Company Branches); `Ledger` (Companies); `FinancialPeriod` (Financial Year); `TaxCategory`, `TaxZone` (Branch); `PaymentMethod` (GL Preferences) | 500 `PXSetupNotEnteredException`, names the missing form |
| Feature-gated | `CustomerClass` (AR201000), `VendorClass` (AP201000), `ItemClass` (IN201000), `Subaccount` (GL203000) | 403 insufficient rights |

Bootstrap findings, all verified against the live instance:

- **`PUT CompaniesStructure` is dead.** 500 `Operation failed`
  (`PXInvalidOperationException` in `EntityService.CreateOrUpdate`) for every
  body variant tried; GET fails too (`BqlDelegate in view Filter`). The
  entity cannot create a company or branch.
- **Login IS a blocker on a genuinely virgin tenant (corrected 2026-07-08).**
  The original "no first-login dance" note was measured against tenant 2,
  which is past first login. A freshly created tenant seeds `admin` / `setup`
  with `PasswordChangeOnNextLogin = 1`, and the contract endpoint cannot clear
  it (see the auth section). First login must go through the screen or a SQL
  reset before REST seeding can run.
- **The CustomizationApi works on a virgin tenant.**
  `POST /CustomizationApi/getPublished` → 200. This is the bootstrap route:
  publish a small package whose custom endpoint exposes Companies (CS101500),
  Enable/Disable Features (CS100000), and Credit Terms (CS206500).
- DB facts (read-only sqlcmd): `EntityEndpoint` is empty — built-in endpoints
  are code-defined; custom endpoints are tenant-scoped rows, so the package
  must be published per tenant. `FeaturesSet` is empty for **all** tenants —
  features were never enabled anywhere, which explains the 403 class.
- Insert datasets are no shortcut — see `ac-exe.md` (T100/U100 are training
  data, not clean baselines).

Consequence — the ordered bootstrap pipeline:

1. `acu tenant create` (ac.exe over SSH)
2. publish the bootstrap package (`/CustomizationApi`)
3. enable features + create company/branch (custom endpoint)
4. seed `baseline/` (Default endpoint)

~~Open risk: whether CS100000 accepts writes through a custom endpoint~~
**Resolved 2026-07-08 — CS100000 does NOT accept writes through a custom
endpoint.** See the custom-endpoint section below. The fallback is now the
route: a `CustomizationPlugin` flipping `FeaturesSet` on publish (ships C#
in the package).

## Custom endpoints under the hood — verified 2026-07-08

Everything below was verified live on a scratch tenant (BootTest, CompanyID
4, deleted after) by installing a custom endpoint as raw DB rows and probing
the API; constants were pulled from the server's own DLLs via PowerShell
reflection (`PX.Api.ContractBased[.Common].dll`).

**A custom endpoint is nothing but tenant-scoped rows in five tables** —
no code, no files:

| Table | Row shape (key columns) |
|---|---|
| `EntityEndpoint` | `GateVersion` ('1.0.0'), `InterfaceName` ('Bootstrap'), `ExtendsVersion/Name` (null unless extending), `SystemContractVersion` = **4** on this build (`SystemContracts.V4` is the only implementation, attribute `IsCurrent`; the DAC default 1 is legacy) |
| `EntityDescription` | `EntityId` (globally unique int), `GateVersion`+`InterfaceName`, `ObjectName` ('Features'), `Active`, `ObjectType` — `T`opLevel / `L`inked / `D`etail / `R`eport, `ScreenID` ('CS100000') |
| `EntityFieldDescription` | `EntityId`, `EntityFieldId`, `FieldName`, `FieldType` — the wrapper names: `StringValue`, `BooleanValue`, `IntValue`, `DecimalValue`, `GuidValue`, `DateTimeValue`, `ShortValue`, `LongValue`, `DoubleValue`, `ByteValue`, `DateOnlyValue`; `PopulateByDefault` = 0 |
| `EntityMapping` | `MappingKey` `E/<entityId>/<fieldId>` → `MappedObject` (graph **view** name) + `MappedField` (DAC field). No entity-level `E/<id>` rows exist — the primary view resolves from the screen. |
| `EntityActionDescription` | `EntityId`, `ActionId`, `ActionName` (contract name), `MappedAction` (graph action member), `Active`; action-parameter mappings key as `E/<eid>/A<aid>/<pid>` |

Facts that matter for tooling:

- **Built-in endpoints materialize into the same tables under CompanyID 1**
  (~2.7k `EntityDescription`, ~34k field rows appeared after an app-pool
  recycle). `EntityId`/`ActionId` are allocated globally
  (`SELECT MAX(...)+1`) — a custom entity reusing a taken id kills the
  whole contract API for that tenant with "An item with the same key has
  already been added". The earlier "EntityEndpoint is empty" observation
  was a pre-materialization artifact.
- **Endpoint metadata is cached per app domain** (`MetadataProvider.*`
  slots). A new `EntityEndpoint` row shows in `GET /entity` immediately,
  but entities/fields/actions need an app-pool recycle to appear.
- Tenant scoping is real: rows with CompanyID 4 are invisible to sessions
  on other tenants (404), exactly as the publish-per-tenant model implies.
- Broken metadata rows don't 404 — they 500/302 **every** `/entity/…`
  request on that tenant, including the built-in Default endpoint. A
  malformed row poisons the tenant's whole contract API.

**The CS100000 verdict (T3).** With a correct custom endpoint mapping
`Features` → FeaturesMaint view `Features`:

- `PUT …/Features {"MultiCompany": {"value": true}}` → **200, but nothing
  persists** — `FeaturesSet` stays empty, and the echoed record omits all
  value fields. The primary view is a keyless BqlDelegate view; the
  contract-API update targets the delegate's transient row and the graph
  save is a silent no-op.
- `GET …/Features` → 500 `CannotOptimizeException` ("There is a BqlDelegate
  in view Features") — same disease as `PUT CompaniesStructure`.
- Exposing the graph's custom `Insert` action (`EntityActionDescription` →
  `MappedAction: Insert`) and invoking it → 500 `PXInvalidOperationException`
  "Operation failed".

So features cannot be enabled through the contract API surface at all, no
matter what endpoint fronts CS100000 — the fallback (C# CustomizationPlugin
writing `FeaturesSet` on publish) ships in the bootstrap package. **The
fallback is verified working (T11, 2026-07-08):** after publish + recycle,
`CustomerClass`/`ItemClass` (the 403 feature-gated class) answer 200 on a
freshly provisioned tenant; `VendorClass` moves to the setup-chain class
(GL Preferences), which baseline seeding owns. See the CustomizationApi
section for the plugin-side landmines.

**Where to read server errors when the API only 302s to `/ui/error`:**
flip `EnableFirstChanceExceptionsLogging` to `True` in web.config (default
False; restarts the app) and read
`App_Data\firstchanceexceptions.log`. The `/ui/error` SPA itself reads
`apiweb/error-page`, which only carries the generic message.

## CustomizationApi — verified quirks (2026-07-08, extended same day)

- **Failures are in-band.** Every `/CustomizationApi/*` call answers 200;
  errors appear only as `log` entries with `"logType": "error"` (a rejected
  import still returns 200 + "The project is not found: …" in the log).
  `getPublished` with nothing published returns only a `log` — no
  `projects` key.
- **The import content field is `projectContentBase64`** — the live binder's
  property (`ImportParamsData`, verified by reflection on
  `PX.Web.Customization.dll`). The widely documented `projectContents`
  binds NOTHING on this build: the server deletes the existing project
  (`isReplaceIfExists`) and then errors "The project is not found" — the
  original mystery import failure. Full binder surface: `ProjectLevel`,
  `ProjectName`, `ProjectDescription`, `ProjectContentBase64`,
  `IsReplaceIfExists`, `Content`.
- **Project names are alphanumeric only.**
  `CstDbStorage.ValidatePackageName` rejects `-` and `_` outright
  ("Invalid project name", import 500s) — `acu-bootstrap` was invalid from
  birth; the package is now `AcuBootstrap`.
- `publishBegin` accepts `projectNames`, `isMergeWithExistingPackages`,
  `isOnlyValidation`, `isOnlyDbUpdates`,
  `isReplayPreviouslyExecutedScripts`, `tenantMode: "Current"`; poll
  `publishEnd` for `{isCompleted, isFailed, log}`. There is also
  `getProject {projectName}` (returns `projectContentBase64`) and
  `delete {projectName}`.
- **`getPublished` is a false idempotence proxy after tenant recreate.**
  Deleting a tenant and recreating it under the same CompanyID keeps the
  stale publication row (the package stays listed) while the project
  content and everything the publish wrote are gone; an
  `isOnlyDbUpdates` replay then fails with "The previously published
  project cannot be found in the database". `acu`'s publish skip therefore
  requires getPublished AND getProject to agree.
- **Package format (verified for code items).** The importable zip carries
  `project.xml` whose root is
  `<Customization level="" description="…" product-version="26.101">` —
  verified against the training packages shipped on the box
  (`HelpAndTraining/T*/PhoneRepairShop.zip`, same build). The project
  *name* comes from the import call, not the XML.
    - **C# source items are `<Graph ClassName="X" FileType="NewFile"
      Source="…escaped source…"/>`** — the source rides in the `Source`
      ATTRIBUTE (`Customization.CstCodeFile`, `Tag = Graph`; shape
      confirmed by invoking its own `Save()` by reflection and by live
      import round-trip). Inline CDATA children and zip-file variants are
      silently dropped on import.
    - **Endpoint items are inline XML, no `.endpoint` file (T12,
      verified by import round-trip 2026-07-08).** The `<EntityEndpoint>`
      project item wraps an `<Endpoint>` child that is the XmlSerializer
      form of `PX.Api.ContractBased.Common.Model.Endpoint` — namespace
      **`http://www.acumatica.com/entity/maintenance/5.31`** (the
      "Unknown root node …data-model:Endpoint" rejection was the wrong
      namespace, not a missing file; the `*.endpoint` globs in
      `PX.Api.ContractBased.Common.dll` belong to the source-control
      folder layout). Shape (attributes throughout):
      `<Endpoint name version systemContractVersion=4>` →
      `<TopLevelEntity name screen>` (also `Detail`/`LinkedEntity`/
      `Report`) → `<Fields><Field name type/></Fields>` +
      `<Mappings><Mapping field><To object="<view>" field="<DAC prop>"/>
      </Mapping></Mappings>` + `<Actions><Action name mappedTo/>`.
      `type` = the wrapper names (`StringValue`, `ShortValue`, …);
      `systemContractVersion` 4 = `SystemContracts.V4`, the build's only
      `IsCurrent` implementation. The server's own getProject
      re-serialization carries no attributes on `<EntityEndpoint>` —
      identity comes from the child's name/version. Live screen chain for
      the bootstrap payload: CS101500 → `OrganizationMaint`, PrimaryView
      `BAccount` (DAC `PX.Objects.CS.DAC.OrganizationBAccount`; fields
      `AcctCD`/`AcctName`/`OrganizationType`/`BaseCuryID` — no
      `OrganizationCD`, no `CountryID` on this build); CS206500 →
      `TermsMaint`, PrimaryView `TermsDef` (DAC `PX.Objects.CS.Terms`;
      `DayDue00` is `Nullable<Int16>` → `ShortValue`).
- **`CustomizationPlugin` is the working features route (T11, verified).**
  `UpdateDatabase` runs on publish (plus a second invocation around site
  start). Writing through `FeaturesMaint` + `Save.Press()` collides with
  the concurrent invocation ("Another process has added the 'FeaturesSet'
  record. Your changes will be lost." — a warning, publish still reports
  success, nothing persists). Writing through
  `PXDatabase.Update/Insert<FeaturesSet>` works; all 205 NOT NULL bit
  columns must be assigned (only ~136 have DB defaults) — fill them
  reflectively. `Status = 0` means Validated.
- **The feature slot outlives the publish restart.** The publish restarts
  the site BEFORE its DB transaction commits, so the restarted domain
  caches the pre-plugin feature set — feature-gated screens keep answering
  403 until one more app-pool recycle. `acu tenant create` recycles
  unconditionally after its publish step.

## Conventions for the seeding layer

- Target the versioned path (`Default/25.200.001`), never an unversioned
  alias — deterministic contract per instance build.
- Field values go wrapped: `{"AccountCD": {"value": "10100"}}`.
- `PUT /<Entity>` with the key fields present updates-or-creates — the
  idempotence primitive. Confirm per entity with a re-run diff.
- `GET /<Entity>` with `$select`/`$filter`/`$expand` is the read side the
  drift-diff and Part 2 validation build on.
