# Contract-based REST API — verified notes

Working notes for the reference-data-as-code layer. **Verified against
acu-dev1 (Acumatica 26.101.0225) on 2026-07-07.**

## Endpoint

Base: `http://acu-dev1.vm.internal/AcumaticaERP`

Newest Default contract endpoint on this build (from `GET /entity`):

```
/AcumaticaERP/entity/Default/25.200.001/
```

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

- **Tenant routing is strict — but only when the tenant map is current
  (verified 2026-07-08).** Send an explicit, valid tenant name; do not omit it
  or pass an empty string. Post-recycle behavior:
    - unknown non-empty name (e.g. `DefinitelyBogus`) → **500
      `PXUndefinedCompanyException`** ("A proper company ID cannot be
      determined"). No silent fallback.
    - valid name (e.g. `Company`, `Scratch`) → routes to *that* tenant and
      enforces *that tenant's* credentials.
    - omitted / `""` → 204 on the **default tenant** (expected Acumatica
      semantics; the one residual silent case — never rely on it).
  History worth knowing: on 2026-07-07, before the post-create app-pool
  recycle, login accepted *any* tenant name (incl. nonsense) and silently
  landed on the default tenant — marker UOM rows written under four names all
  hit CompanyID 2. That was a **stale-tenant-map artifact** (tenant 3 existed
  in the DB but wasn't loaded), not steady-state behavior. Lesson: skipping
  the recycle turns a wrong tenant name into a silent wrong-tenant write. A
  post-login tenant guard in the seeding pipeline is still worthwhile as
  defense-in-depth.
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

Open risk: whether CS100000 accepts writes through a custom endpoint
(community prior art suggests feature flags are settable per-tenant via
customization; a `CustomizationPlugin` flipping `FeaturesSet` on publish is
the fallback, at the cost of shipping C# in the package).

## Conventions for the seeding layer

- Target the versioned path (`Default/25.200.001`), never an unversioned
  alias — deterministic contract per instance build.
- Field values go wrapped: `{"AccountCD": {"value": "10100"}}`.
- `PUT /<Entity>` with the key fields present updates-or-creates — the
  idempotence primitive. Confirm per entity with a re-run diff.
- `GET /<Entity>` with `$select`/`$filter`/`$expand` is the read side the
  drift-diff and Part 2 validation build on.
