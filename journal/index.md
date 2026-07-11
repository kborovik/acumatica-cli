# Development journal — Acumatica config-as-code

The engineering record of building `acu`: every problem, friction, dead end,
and correction hit while trying to configure Acumatica ERP purely from source
code. Dead ends and errors stay in — they are findings, not noise, and the
raw material for the blog series (article drafts live in the sibling
`acumatica-blog` repo).

**Conventions:** one file per day (`YYYY-MM-DD.md`), entries within a day in
chronological order. After any meaningful work: add or extend the day's entry
(progress, errors, solutions), then update the entry list, friction catalog,
and status below.

## Entries

Newest first.

- [2026-07-10](2026-07-10.md) — T29 fronts financial currency (CM202000)
  from the Bootstrap endpoint: live archaeology maps the screen's two
  views (general info on the `CurrencyList` primary including
  `IsFinancial`; the GL Accounts tab on `CM.Currency`, whose ten
  gain/loss account pairs are all required-at-persist with no default),
  and the full pipeline verifies on a fresh scratch tenant — provision,
  accounts, `PUT Currency [EUR]`, and the B8 acceptance object (an
  EUR-denominated account) applying and diffing clean; the verify's diff
  leg immediately surfaces B9 — the contract API's *list* GET is an
  optimized export that 500s on any delegate-view field, so the entity
  that just persisted was unreadable by `diff` — fixed as T30 with an
  operator-chosen fallback: on exactly the optimization-500, diff
  retries the record via a key-URL single-record GET (which skips the
  optimizer; a missing record there answers 500
  `NoEntitySatisfiesTheConditionException`, not 404); also recorded:
  `Translation*` currency accounts are feature-gated — write-tolerated
  but read-invisible while financial-statement translation is off, so
  seed YAML must not claim them; an evening code review of `src/` fixes
  two findings (the template comment contradicted the `Translation*`
  caveat, which lived only in a commit message; `PACKAGE_DESCRIPTION`
  hand-duplicated the XML root description, now parsed from the
  template) and specs two latent gaps preventively — V20/T32 (Bootstrap
  `Currency` name collides with the Default endpoint's, so files naming
  a Bootstrap-template entity must carry an explicit `endpoint:`) and
  V21/T33 (T29 changed the contract shape under a held version 1.0.0;
  bump to 1.1.0 — contract identity is name plus version); T31 graduates
  the scratch into the data repo's `baseline/` — 15 subaccounts, the
  161-account chart of accounts, and CAD/EUR/SGD financial currencies
  with real gain/loss accounts — through two backpropped failures:
  B10/V22 (apply order was an alphabetical accident — currencies applied
  before the subaccounts they reference; numbered filename prefixes now
  encode order, `10-`/`20-`/`30-`, `uoms` parked at `90-`) and B11/V22∆
  (server-derived `ChartOfAccountsOrder`/`CashAccount` are PUT-tolerated
  but the server keeps its own derivation, so extract-sourced values
  drift forever — stripped, alongside the `AccountGroup` refs the raw
  extract carried); third fresh provision exits 0 with no drift over six
  files and an independent re-diff confirms; T32 enforces V20 —
  `load_baseline` hard-errors on a file naming a Bootstrap-template
  entity without an explicit `endpoint:` (the ambiguous set parsed from
  `bootstrap_project.xml`, never hand-listed), closing the
  silent-wrong-endpoint route back to B8; T33 lands the bump itself —
  endpoint now `Bootstrap/1.1.0`, seed-side strings all derived so only
  the XML attribute, templates, data-repo refs, and pinned tests moved;
  the T25 digest gate forced the live republish through a resumable
  provision and an independent diff read clean through 1.1.0 — the §T
  backlog is empty; T34 adds `GLPreferences` to the endpoint and bumps
  to 1.2.0 — the live site map corrected the screen ID before any code
  (GL preferences is GL102000 on this build, the spec's GL105000 was a
  training-data artifact; V12 amend), reflection settled the field set
  at exactly two required accounts on `GLSetupRecord`, the data repo
  gained `40-ledger.yaml` + `50-gl-preferences.yaml`, and a fresh
  provision applied all eight files with both diffs clean and
  `JournalTransaction` answering 200 where unbootstrapped tenants threw
  `PXSetupNotEnteredException`; the "GL batch posts" verify leg then hit
  a multi-surface wall and backpropped as B12 + T36 — no org-ledger
  link (the Default `Ledger.Companies` detail is write-tolerated but
  silently dropped) and no financial calendar (`FinYearSetup` → master
  year → company periods, each its own gap), so T34's verify narrowed
  per V17 and T36 owns the GL posting closure; T38 lands the action-file
  mechanism offline — `setup/*.yaml` declares a contract action plus a
  `done_when` live-state probe, `load_baseline` dispatches on the
  `action:` key, `client.invoke` handles 204-done vs 202-poll-`Location`
  (a 204's `Location` is bogus and never followed — the T36 trap encoded
  in code and pinned by test), apply skips and diff drifts off the same
  probe (V4, one probe both directions), and `provision` gains the
  `setup/` phase after `baseline/`; twelve MockTransport tests pin the
  mechanism, and T36's in-flight 1.3.0 template sat stashed across the
  gate so the path-scoped commit stayed clean of it; T35 graduates the
  verified minimal company into the `acu config init` template set —
  eleven files scaffold (zero subaccount, ten-account generic chart with
  retained earnings + net income, ACTUAL ledger, GL preferences through
  `Bootstrap/1.3.0`, UOMs renamed `90-uoms.yaml`; numbered prefixes
  teach the V22 apply order, and a dry-run test pins the order itself) —
  and the live verify catches the template set unclosed over its own
  feature requirements: the subaccounts template 403s on GL203000
  because the features template shipped the built-in six without
  SubAccount (B15; the closure folded into V22 as the feature sibling of
  reference closure), after which the resumed provision republished
  through the digest gate and applied all seven seed files with a clean
  diff, re-init answered eleven skips, and the scratch tenant was torn
  down; T37 closes the last GL-posting link — the GL201100 "Open
  Periods" redirect that B13 declared unfollowable is bypassed by
  driving GL503000 itself: Bootstrap 1.4.0 adds `ManagePeriods` (the
  `FinPeriodStatusProcess` filter plus a contract action on the
  runtime-registered `ProcessAll`; the filter `Action` takes the stored
  word `Open`) and `CompanyPeriod` (GL201100's per-period
  `OrgFinPeriods` view, single-view on purpose so the done_when
  conjunction of FinancialYear and Status dodges B14), drops the dead
  CompanyCalendar `OpenPeriods` action, and the live verify runs the
  whole chain on a fresh scratch tenant — provision zero manual steps,
  `invoke ProcessAll`, diff clean over twelve files, and a GL batch PUT
  + release answering **Posted**, the criterion B12/B13 kept pushing
  forward; re-run skipped all three actions and the tenant tore down
  clean; T39 graduates the `setup/` files into the `config init`
  template set, and the build's planning pass catches the scope short
  before any code — the row pinned 14 files but the verify leg (a
  scaffolded GL batch posts) needs the org-ledger link B12 recorded,
  which only lived in the data repo (B16; V22's self-closing clause
  generalizes from features to every recorded dependency-chain link the
  set's own verify chain needs); fifteen files now scaffold —
  `60-ledger-company` keyed on `LedgerCD` alone per B14 plus the three
  setup templates with `OrganizationID: COMPANY` matching the company
  template's `AcctCD`, year values shipped as editable placeholders —
  with the dry-run round-trip pinning the full apply order across all
  three dirs and a reference-closure test (B15's sibling) pinning every
  `OrganizationID` to the company `AcctCD`.
- [2026-07-09](2026-07-09.md) — T17 closes the SPEC backlog: `exit
  $LASTEXITCODE` centralized in `_ssh` (single choke point per V18, the
  B4 recurrence class), call-site hand-appends stripped, suffix pinned
  exactly-once by a new regression test; T18 mechanizes the §V.9 ASCII
  audit into `scripts/check-ascii` (tokenize/ast exemptions for `.py`
  comments and docstrings, `//` lines in `.cs`, `<!-- -->` in `.xml`;
  violations report as `file:line: U+XXXX` so the audit's own output
  stays ASCII), check-extras recipe cmd flipped to the script; T19 does
  the same for the V1/V10/V18 drift greps — `.claude/scripts/check-extras.sh`
  emits `id|verdict|evidence` rows per the /sdd:check extras-hook contract
  (plane-split imports, direct-BaseModel subclassing outside `models.py`,
  `exit $LASTEXITCODE` sole-site-in-`_ssh`), VIOLATE branches pinned via a
  synthetic repo tree in tests; `make e2e` lands the opt-in live test tier
  (T20) — a pytest `e2e` marker deselected by default drives the real `acu`
  binary through the full provision lifecycle on a scratch tenant (provision
  → clean diff → idempotent re-run → injected-drift exit 2 → teardown always
  deletes), first run 4 passed in 269s with the tenant list clean after;
  the work also surfaced and fixed a missing `bootstrap` symlink that made
  provision-from-this-repo silently skip bootstrap YAML seeding; T21 closes
  B5's hole with a landed-tenant guard in `AcumaticaClient.__enter__` — live
  archaeology corrected the "strict tenant routing" belief (a single-tenant
  instance accepts ANY tenant name with 204; strictness only exists
  multi-tenant with a fresh map), eliminated the tenant-blind surfaces
  (title bar speaks `CompanyCD`, not login name; the `screenLink`
  `CompanyID` param vanishes on a single-tenant box), and landed on an
  authenticated `GET /Frames/Login.aspx` whose hidden `txtSingleCompany`
  input names the landed tenant's login name in every observed state; the
  guard refuses on mismatch (logging out first), fails closed if the page
  shape changes, and the e2e tier pins `diff` against a nonexistent tenant
  to exit 1; T22 adds the global `--host` flag — the override lands on the
  raw config dict inside `load_instance` *before* the Instance is built so
  derived `base_url`/`ssh` re-derive from the new host (a post-hoc
  `model_copy` would leave them stale), while explicit acu.yaml
  `base_url`/`ssh` keep their precedence; T23 puts the package on PyPI —
  `release.yml` fires on `release: published`, builds with `uv build`, and
  publishes via OIDC trusted publishing (no API token; a workflow step pins
  tag == pyproject version per V19), with the verification release (v0.2.2)
  first tripping over the PyPI registration form's Repository-name field
  (bare repo name, not `owner/repo` — the form accepts the slash silently
  and the publish fails `invalid-publisher`); `pip install
  acumatica-cli==0.2.2` verified from pypi.org; ac.exe archaeology
  (decompiled 26.101 with ilspycmd) recovers two undocumented surfaces,
  both written up in `docs/ac-exe.md` — the `-f` configuration-file XML
  format (`<Root>` + one element per long option name with a `Value`
  attribute; installer parameters only, no ERP data; genuine dump produced
  by calling the wizard's own `SaveToFile` via PowerShell reflection) and
  a hidden bare-word verb family (`export`/`import`/`database`/…):
  `ac.exe export xml 'mssql://…?companyid=N' <folder>` live-verified,
  dumps one tenant as per-table XML in exactly the shipped
  `Database\Data\` dataset format (SalesDemo *is* such a dump), making
  dump → version → edit → import mechanically possible but raw-table
  (ID webs, no business logic, version-coupled) — REST seeding stays
  primary, export earns a note as a whole-tenant diff/DR candidate;
  T24 makes the bootstrap feature set data-driven — the C# plugin ships
  an `/*ACU_FEATURES*/` sentinel that `package_zip()` fills from the data
  repo's `bootstrap/features.yaml` (absent → built-in six as a Python
  code default), a plugin-side guard logs names matching no `FeaturesSet`
  property, and `expand_files` skips `features.yaml` in directory sweeps
  (package-build config, not seed data); live verify on a fresh tenant
  kills both B6 symptoms (`Multicurrency=1 SubAccount=1` in FeaturesSet,
  15/15 subaccounts and 161/166 accounts apply) but exposes B6's
  half-wrong attribution: the 5 EUR-denominated accounts still 422
  because the Default endpoint's `Currency` entity is the CM201000
  currency list only — `UseForAccounting` creates no CM202000
  financial-currency row (backpropped as B8, §C gap list extended,
  T29 queued to front CM202000 from the Bootstrap endpoint); T25 makes
  the publish skip gate content-aware (friction #32 / B7): the package
  digest (sha256 of project.xml bytes) rides in the import's
  `projectDescription` and reads back from the root `description`
  attribute of getProject's re-serialized project.xml — the API's one
  round-trip channel, settled by live probe (getPublished rows carry
  only names) — so a digest mismatch (changed features.yaml, older
  tool's package) now reimports + republishes instead of silently
  skipping; T26 adds `acu config init` — a seven-file data-repo
  scaffold shipped as package data (skip-if-exists, placeholder
  secrets, no git/gpg), with instance resolution moved from the Click
  group callback to a lazy `pass_instance` decorator so init runs
  where discovery finds no `acu.yaml`; a near-miss recorded en route:
  the repo's unanchored `.gitignore` symlink patterns silently
  excluded five templates from the commit while the suite passed from
  the working tree (patterns now root-anchored, wheel contents
  verified by hand); T27 adds `acu config check` — a four-probe
  read-only preflight (discovery → secrets → REST ∥ ssh, one ok/fail
  line each, exit 0/1) where the REST probe is just entering
  `AcumaticaClient` (login + landed-tenant verify + logout) and the ssh
  probe is a new `TenantManager.ping()` through the `_ssh` choke point;
  discovery/secrets failures stop, the two live planes report
  independently so one run names every broken layer; T28 makes
  `--version` read its own PEP 610 `direct_url.json` so an editable
  checkout prints `<version>+dev (<path>)` while a wheel stays plain.
- [2026-07-08](2026-07-08.md) — recycle unblocks tenant visibility (stale-map
  corrections); first-login password wall found and defeated (screen-flow,
  then `-aup` preset); `acu tenant create` chains create → recycle →
  login-ready; two more ac.exe landmines (`-h`, `Deleted` sub-key);
  domain-style tenant names verified; CLI output standards defined
  (`docs/cli.md`, rich, human + LLM-agent audiences via TTY detection);
  offline test suite established (45 tests: MockTransport for REST,
  monkeypatched subprocess for SSH; ac.exe landmines pinned as regressions);
  pydantic adopted as the model standard (frozen, `extra="forbid"`) and
  `instances/*.yaml` folded into a single `acu.toml` with `default_instance`;
  repo split — data (`baseline/`, `acu.toml`, `.env.gpg`) extracted to the
  sibling `acumatica-baseline`, `acu.toml` became the cwd-walk-up discovery
  sentinel (`data_root()`), and the tool goes on PATH via
  `uv tool install --editable`; `scripts/dump-swagger.sh` converted to
  `acu schema` (kills the duplicate curl session logic and env-var config
  channel; `output.data()` gained `soft_wrap` so piped result lines never
  hard-wrap); repo renamed `acumatica-devops` → `acumatica-cli` (package
  `acumatica-cli`, module `acumatica_cli`, README rewritten, baseline repo's
  install path updated); CLAUDE.md + docs distilled into a root `SPEC.md`
  (goal / constraints / interfaces / invariants / tasks), CLAUDE.md reduced
  to a pointer stub; initial commit pushed to a new public GitHub repo
  (<https://github.com/kborovik/acumatica-cli>); `/sdd:build --all` landed
  `acu provision` (create → bootstrap → apply → diff, idempotent), the
  bootstrap-package machinery (`acu bootstrap`, CustomizationApi client),
  and the explicit-tenant session guard; the CS100000 verification came
  back **negative** after a live custom-endpoint archaeology session
  (endpoints = tenant-scoped DB rows in five tables, metadata cached per
  app domain, CustomizationApi errors are in-band 200s, project.xml root
  is `<Customization>`) — PUT Features persists nothing, so the C#
  CustomizationPlugin fallback is the route; evening `/sdd:build --all`
  landed the CLI-standard + config backlog (ASCII-only output incl. TTY,
  `docs/cli.md` deleted into SPEC, drift = exit 2, `acu bootstrap` and
  `schema -o` dropped, layered `Instance` defaults — `host` is the only
  required `acu.toml` key, `acu config show` prints the resolved target),
  the data repo's config shrank to two lines and both planes verified live;
  T10's "passes provision" gate backpropped into the spec (B1/V17/T11:
  verify gates must be satisfiable against current spec state — provision
  E2E waits on the C# plugin task); late evening: T11 landed and provision
  went E2E-green on a virgin tenant — the import mystery dissolved into
  `projectContentBase64` (the documented `projectContents` binds nothing)
  plus alphanumeric-only project names, code items serialize as
  `<Graph Source="…">` (discovered by invoking `CstCodeFile.Save()` by
  reflection), the plugin writes FeaturesSet via `PXDatabase` (graph save
  collides with the publish pipeline), the feature slot needs one
  post-publish recycle, and tenant recreate leaves a stale publication row
  (B2/B3, V4 desired-state rule, T12 queued for the `.endpoint` format);
  night: T12 dissolved the `.endpoint` premise — packages carry endpoints
  as inline XmlSerializer XML (namespace `entity/maintenance/5.31`; the
  old `entity/data-model` guess was the "Unknown root node" rejection, and
  the `*.endpoint` globs are source-control folder layout), the payload's
  view/field names were re-read off the live box (CS101500 → `BAccount`
  view, `AcctCD`/`AcctName`; CS206500 → `TermsDef`; no `CountryID` on this
  build), and the Company + CreditTerms endpoint shipped in the bootstrap
  package — provision E2E green on a scratch tenant with
  `Bootstrap/1.0.0` answering, bootstrap YAML seeding unblocked;
  small hours: T13 landed — `bootstrap/company.yaml` + `credit-terms.yaml`
  seed CS101500/CS206500 from the data repo and the write path is
  live-verified through three rounds of 422 archaeology (the graph inserts
  an Address row needing `CountryID`; OrganizationType/BaseCuryID must map
  to `OrganizationView` per the screen's own bindings — the primary-view
  projection echoes the PUT and persists nothing; list fields take external
  labels, and a wrong-but-valid DAC code is silently misread), `seed._norm`
  compares numbers by value, provision's drift check now covers the
  bootstrap YAML it applies, virgin-tenant provision exit 0 with no drift
  over 3 files; evening: config file migrated TOML → YAML (T15) —
  `acu.yaml` is the discovery sentinel and `acu config show` emits a
  complete valid `acu.yaml` (credentials excluded) that round-trips through
  `load_instance`, making `acu config show > acu.yaml` the config-editing
  workflow; verified live (round trip identical + `tenant list` green);
  then flattened to single-instance (T16) — dev1/tst1/prd1 multi-host was
  weighed and rejected (multiple admin passwords in one `.env`), so
  `instances.<name>`/`default_instance`/`-i` and `Instance.name` are gone,
  `acu.yaml` is a flat `host` + overrides map, messages label the target
  by host, legacy nested files rejected by `extra="forbid"`; T14 closed
  the backlog — the hand-rolled reflection-probe pipeline became
  `scripts/ps-remote` (utf-16le/base64/`-EncodedCommand`; host defaults
  via `acu config show`, pinned offline with `ssh`/`acu` PATH stubs).
- [2026-07-07](2026-07-07.md) — skeleton verified end-to-end
  (`apply`/`diff` on UOMs); snapshot plan confirmed dead; no API-only
  bootstrap path — CustomizationApi chosen as the route; the silent
  wrong-tenant foot-gun discovered.

## Friction catalog

Every Acumatica problem hit so far, one line each. Status: **resolved**
(fixed in code/config), **workaround** (route around it), **dead end**
(abandoned approach), **open**.

| # | Friction | Status | Entry |
|---|----------|--------|-------|
| 1 | `ac.exe` has no snapshot save/restore in current builds; snapshots are UI-only (SM203520) and screen automation is unreliable | dead end | [2026-07-07](2026-07-07.md) |
| 2 | `ac.exe -cm:CompanyConfig` defaults `-dbnew` to `True` (creates a new DB) — must pass `-dbnew:"False"` on existing DBs | resolved | [2026-07-07](2026-07-07.md) |
| 3 | Official docs mislabel `CompanyType` as a boolean; working values are dataset names (`''` = clean, `SalesDemo` = demo) | resolved | [2026-07-07](2026-07-07.md) |
| 4 | `-iname:"<instance>"` is required but omitted from the docs' CompanyConfig example (`Site with name '' doesn't exist`) | resolved | [2026-07-07](2026-07-07.md) |
| 5 | `-h:"<instance path>"` also required beside `-iname` — without it create dies mid-run with `ArgumentNullException` and rolls back | resolved | [2026-07-08](2026-07-08.md) |
| 6 | Delete sub-key is `Deleted`, not the documented `Delete`; `Delete=Yes` is *silently ignored* and the run overwrites the tenant as an insert target | resolved | [2026-07-08](2026-07-08.md) |
| 7 | Delete also needs the full spec (`ParentID` + `CompanyType`) or the preflight misreads the tenant as the system company and aborts | resolved | [2026-07-08](2026-07-08.md) |
| 8 | `LoginName` lands in `dbo.Company.CompanyKey` (the sign-in name), not `CompanyCD` — easy to misread as "didn't take" | resolved | [2026-07-08](2026-07-08.md) |
| 9 | Tenant names: `;` and `=` are delimiters inside the `-company:"…"` string — names containing them corrupt the ac.exe invocation | open (needs input validation) | [2026-07-08](2026-07-08.md) |
| 10 | New tenants are invisible to the running app until an `AcumaticaERP` app-pool recycle — the tenant map loads at startup | resolved | [2026-07-08](2026-07-08.md) |
| 11 | REST login silently lands on the wrong tenant: a stale tenant map reroutes named logins to the default tenant, and a single-tenant instance accepts *any* tenant name outright (204, no validation — steady state, not an artifact) | resolved (landed-tenant guard in the client: `txtSingleCompany` probe, refuse on mismatch; plus recycle discipline) | [2026-07-07](2026-07-07.md), [2026-07-08](2026-07-08.md), [2026-07-09](2026-07-09.md) |
| 12 | Fresh tenants seed `admin`/`setup` with a must-change flag the contract REST API cannot clear; retry loops lock the account | resolved (`-aup` preset at create; `Login.aspx` screen-flow fallback) | [2026-07-08](2026-07-08.md) |
| 13 | `Login.aspx` automation traps: four submit buttons (`mfLoginButton` matches first), `txtConfirmPassword` must be posted too, alarming hidden divs are static template noise | resolved | [2026-07-08](2026-07-08.md) |
| 14 | Unconfigured tenants fail on most entities — 500 `PXSetupNotEnteredException` (Companies → Branches → GL Preferences → Financial Year chain) or 403 on feature-gated screens | open (bootstrap package) | [2026-07-07](2026-07-07.md) |
| 15 | No API-only bootstrap path: `PUT CompaniesStructure` fails on every variant; features (CS100000) unreachable via built-in endpoints | workaround (CustomizationApi route) | [2026-07-07](2026-07-07.md) |
| 16 | Payment terms have no entity in the Default 25.200.001 endpoint | workaround (custom endpoint in bootstrap package) | [2026-07-07](2026-07-07.md) |
| 17 | REST logout returns `411 Length Required` without an explicit `Content-Length: 0` | resolved | [2026-07-07](2026-07-07.md) |
| 18 | API sessions count against the license's concurrent-user cap — leaked sessions exhaust a trial instance | resolved (client is a context manager; logout always runs) | [2026-07-07](2026-07-07.md) |
| 19 | CustomizationApi failures are in-band: every call answers 200, errors live only in `log[].logType == "error"` — status-code checking is blind | resolved (`_checked_log`) | [2026-07-08](2026-07-08.md) |
| 20 | project.xml root is `<Customization level description product-version>`, not `<Project>`; `EntityEndpoint` items are inline XmlSerializer XML in namespace `entity/maintenance/5.31` — the `entity/data-model` guess drew "Unknown root node", and the `.endpoint` globs are source-control folder layout, not package format | resolved (T12; import round-trip verified) | [2026-07-08](2026-07-08.md) |
| 21 | CS100000 rejects the whole contract-API surface: PUT 200-but-no-persist (keyless BqlDelegate view), GET `CannotOptimizeException`, `Insert` action invoke `PXInvalidOperationException` | dead end (C# CustomizationPlugin fallback) | [2026-07-08](2026-07-08.md) |
| 22 | Custom-endpoint DB rows: EntityIds are global — a colliding id kills the tenant's whole contract API; endpoint metadata is cached per app domain (recycle to refresh); one malformed row 302s every `/entity` request on the tenant | resolved (documented row formats) | [2026-07-08](2026-07-08.md) |
| 23 | CustomizationApi import: documented `projectContents` field binds nothing on 26.101 — server deletes the project then errors "The project is not found"; live binder wants `projectContentBase64` | resolved | [2026-07-08](2026-07-08.md) |
| 24 | Customization project names are alphanumeric-only (`ValidatePackageName` rejects `-`/`_` with a bare "Invalid project name" 500) | resolved (`AcuBootstrap`) | [2026-07-08](2026-07-08.md) |
| 25 | Code items serialize as `<Graph ClassName FileType Source="…">` with source in the ATTRIBUTE; CDATA children and zip-file variants import clean and are silently dropped | resolved | [2026-07-08](2026-07-08.md) |
| 26 | `FeaturesMaint` + `Save.Press()` inside a CustomizationPlugin persists nothing — concurrent plugin invocations collide ("Another process has added the 'FeaturesSet' record", logged as a warning) | resolved (`PXDatabase` writes, all 205 NOT NULL bits assigned) | [2026-07-08](2026-07-08.md) |
| 27 | The publish restarts the site before its DB transaction commits — the new domain caches the pre-plugin feature set and gated screens stay 403 until one more recycle | resolved (provision recycles after publish) | [2026-07-08](2026-07-08.md) |
| 28 | Tenant delete + recreate under the same CompanyID keeps the stale publication row: `getPublished` lists the package while its content and effects are gone; `isOnlyDbUpdates` replay fails "previously published project cannot be found" | resolved (skip gate = getPublished AND getProject) | [2026-07-08](2026-07-08.md) |
| 29 | Custom-endpoint mappings must follow the screen's own bindings, not the primary view's DAC props — the BAccount projections of OrganizationType/BaseCuryID echo the PUT back and persist nothing while the graph inserts an empty GL `Company` row (422); the auto-inserted Address row demands `CountryID` | resolved (map to `OrganizationView`/`AddressDummy` per CS101500.aspx) | [2026-07-08](2026-07-08.md) |
| 30 | Contract-API list fields take external labels, not DAC codes — `VisibleTo: A` is rejected with the allowed list, but `DueType: D` is *silently misread* as "Day of Next Month"; a deliberately bogus value elicits the full allowed-label list from the 422 | resolved (labels in YAML, allowed lists in file comments) | [2026-07-08](2026-07-08.md) |
| 31 | DecimalValue fields come back as floats — YAML `0` vs live `0.0` flagged spurious drift when compared as strings | resolved (`seed._norm` compares numbers by value) | [2026-07-08](2026-07-08.md) |
| 32 | The publish skip gate verifies the project *exists*, not that its content matches this tool version's package — a changed package silently skips on an already-provisioned tenant (B3's marker class, one notch subtler) | resolved (T25: content digest in the package description, mismatch republishes) | [2026-07-08](2026-07-08.md), [2026-07-09](2026-07-09.md) |
| 33 | Remote probes cross two PowerShell parsers plus the local shell — `$_` inside a double-quoted `-Command` interpolates away before the inner powershell runs; every reflection probe hand-rolled the utf-16le/base64/`-EncodedCommand` workaround | resolved (`scripts/ps-remote`, T14) | [2026-07-08](2026-07-08.md) |
| 34 | Default-endpoint `Currency` entity is the CM201000 currency *list* only — `UseForAccounting: true` creates no CM202000 financial-currency row, so EUR-denominated GL accounts 422 "Currency cannot be found" even with Multicurrency enabled | resolved (T29: `Currency` entity on `Bootstrap/1.0.0` fronts CM202000; EUR-denominated account applies and diffs clean) | [2026-07-09](2026-07-09.md), [2026-07-10](2026-07-10.md) |
| 36 | Contract-API *list* GET is an optimized export that 500s ("Optimization cannot be performed") when any field in scope maps to a BQL-delegate view — the Bootstrap Currency GL-account fields all do, so a PUT that persisted could not be read back by `diff` | resolved (B9/T30: on exactly that 500, diff retries the record via key-URL single-record GET, which skips the optimizer) | [2026-07-10](2026-07-10.md) |
| 37 | Feature-gated fields are write-tolerated but read-invisible — `Translation*` account pairs on the Bootstrap Currency entity accept a PUT while financial-statement translation is off, but never come back on GET, so YAML claiming them diffs "not returned by endpoint" forever | workaround (seed data omits feature-gated fields its tenant's feature set hides) | [2026-07-10](2026-07-10.md) |
| 35 | SalesDemo-extract replay: `AccountGroup` is PM201000 (Projects-gated and absent from the extract) and `ChartOfAccountsOrder` + `CashAccount` are server-derived (PUT-tolerated, server keeps its own derivation) — extracted config carrying any of them fails to apply or diffs dirty forever | resolved (B11/V22: strip rule covers references outside the baseline set and server-derived fields; generator strips all four) | [2026-07-09](2026-07-09.md), [2026-07-10](2026-07-10.md) |
| 38 | Baseline apply order is whatever alphabetical filename sort says — `30-currencies.yaml` gain/loss pairs 422 until the SUB row from the subaccounts file exists, so semantic filenames applied currencies first | resolved (B10/V22: numbered filename prefixes encode the dependency order; dir expansion stays alphabetical) | [2026-07-10](2026-07-10.md) |
| 39 | Training-data screen IDs lie: the plan named GL preferences as GL105000, but the live site map has no such row — General Ledger Preferences is GL102000 on this build (GL105020/30 are budget restriction screens) | resolved (T34: site-map SQL probe + GL102000.aspx bindings; spec amended before any code) | [2026-07-10](2026-07-10.md) |
| 40 | Default-endpoint `Ledger` entity's `Companies` detail is write-tolerated but silently dropped — PUT answers 200 echoing the record while the org-ledger link table stays empty, so the batch header cannot default `LedgerID` (the B3/B7 silent-no-op shape wearing a detail list) | resolved (T36: Bootstrap `LedgerCompany` entity writes through GL201500's `OrganizationLedgerLinkWithOrganizationSelect` view — the link seeds as `baseline/60-ledger-company.yaml`) | [2026-07-10](2026-07-10.md) |
| 41 | GL posting on a fresh tenant needs a setup chain no single entity closes: `FinYearSetup` singleton → master calendar year → company periods, plus the org-ledger link; the Default `FinancialYear` entity inserts bare `{}` but pins the year start to the creation date, and an explicit start date fails the "configure all the Financial Periods" validation both ways the API offers | resolved (T36: `setup/` action files — `GeneratePeriods` (AutoFill) realizes the FinYearSetup singleton with an explicit January 1 start, `GenerateCalendar` (GenerateYears) generates the master year, and company periods derive once the organization exists; `done_when` probes verify each) | [2026-07-10](2026-07-10.md) |
| 42 | The PowerShell reflection probe that settled `GLSetupMaint` stack-overflows on the ledger graphs (`GeneralLedgerMaint`) — same script shape, different type graph | workaround (aspx grep for `TypeName`/`PrimaryView`/`DataField` is the sturdier binding instrument; constructor-free static reflection — `Assembly.GetType` + field enumeration — safely settles declaring types and DAC homes on the same graphs) | [2026-07-10](2026-07-10.md) |
| 43 | Contract-API list GET goes blind on an AND `$filter` spanning fields of different views — each predicate alone matches the row, the conjunction answers 200 `[]`, and the key-URL form 500s with a non-B9 exception, so a composite-key seed file on a multi-view entity phantom-drifts "missing on tenant" behind a PUT that persisted | resolved (B14/V4 clause: multi-view entity seed files key on primary-view fields only; secondary-view fields stay record fields and diff field by field) | [2026-07-10](2026-07-10.md) |
| 44 | A template/data-repo set can require features its own features.yaml never enables — the scaffolded subaccounts template 403s on the feature-gated GL203000 because the features template shipped the built-in six without SubAccount, while the verified data repo passed only because its list had grown separately | resolved (B15/V22 feature-closure clause: the shipped set must enable every feature its baseline files require; template features.yaml gains SubAccount, a test asserts the closure) | [2026-07-10](2026-07-10.md) |
| 45 | Processing screens are not entity-action drivable the obvious way: the GL201100 "Open Periods" action is a `PXRedirectHelper` redirect to GL503000 the contract API cannot follow (B13), and the processing graph declares no `PXAction` to map — the process buttons are runtime-registered by `PXFilteredProcessing` | resolved (T37: a contract entity over the GL503000 *filter view* plus an action mapped to the runtime `ProcessAll` drives the processing directly; the filter `Action` field takes stored words like `Open`) | [2026-07-10](2026-07-10.md) |
| 46 | A shipped file set can satisfy feature closure and still starve its own verify chain — T39 scoped the template set at 14 files while the GL-posting chain it promises needs the org-ledger link that only the data repo carried; a scaffolded tenant would open periods fine and 422 at the batch PUT (B12's known link, outside the scoped set) | resolved (B16/V22: the self-closing clause covers every recorded dependency-chain link the set's own verify chain needs; `60-ledger-company` template ships, a reference-closure test pins `OrganizationID` to the company `AcctCD`) | [2026-07-10](2026-07-10.md) |

## Status

Mechanisms:

1. **Tenant provisioning as code** (`acu tenant list|create|delete`, ac.exe
   over SSH) — `[DONE]` — create chains create → recycle → login-ready admin
   in one command; delete round-trips it.
2. **Snapshot-based baseline** — `[DEAD END]` — no CLI snapshot support;
   baseline lives entirely in reference-data-as-code.
3. **Reference data as code** (`acu apply` / `acu diff`, YAML → REST upserts)
   — `[DONE]` — proven end-to-end on both planes: `baseline/*.yaml` through
   the Default endpoint and `bootstrap/*.yaml` (company + credit terms)
   through the custom `Bootstrap/1.0.0` endpoint, write path live-verified.
4. **One-command provisioning** (`acu provision`: create → bootstrap →
   apply → diff) — `[DONE]` — virgin-tenant E2E green live: create →
   publish (features via plugin) → apply (company + credit terms + UOMs) →
   drift check over everything applied, exit 0.

Remaining milestones:

- `[DONE]` Bootstrap package published via CustomizationApi — the C#
  `CustomizationPlugin` enables features on publish (SPEC T11) and the
  `Bootstrap/1.0.0` endpoint exposes company (CS101500) + credit terms
  (CS206500) (SPEC T12) + financial currency (CM202000) (SPEC T29);
  company + credit terms now seed from
  `bootstrap/*.yaml` in the data repo with the write path live-verified
  (SPEC T13); the plugin's feature set is data-driven from
  `bootstrap/features.yaml` (SPEC T24).
- `[OPEN]` Baseline expanded in dependency order — subaccounts, chart of
  accounts, financial currencies (SPEC T31), the actual ledger + GL
  preferences (SPEC T34; Finance screens open on a fresh tenant), the
  GL setup chain (SPEC T36 via the T38 action files: org-ledger link,
  financial year, master + company calendar), and GL period activation
  (SPEC T37: `ManagePeriods` drives GL503000 directly — Bootstrap
  1.4.0, GL batch posts on a fresh tenant end to end) landed, numbered
  prefixes encode apply order; still open: tax categories/zones →
  customer/vendor/item classes (payment terms seed as bootstrap credit
  terms).
- `[OPEN]` Drift proof: provision two tenants, diff config, zero difference.
- `[OPEN]` Timing captured (manual baseline vs automated).
- `[OPEN]` Repo clean and runnable; README shows `acu provision` reproducing
  the numbers.

Target config domains (what a configured tenant must carry): chart of
accounts (GL), customer/vendor classes, payment terms, tax zones/categories,
item classes, UOMs, currencies, branches/company structure.
