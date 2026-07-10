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
  T29 queued to front CM202000 from the Bootstrap endpoint).
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
| 32 | The publish skip gate verifies the project *exists*, not that its content matches this tool version's package — a changed package silently skips on an already-provisioned tenant (B3's marker class, one notch subtler) | open (spec follow-up) | [2026-07-08](2026-07-08.md) |
| 33 | Remote probes cross two PowerShell parsers plus the local shell — `$_` inside a double-quoted `-Command` interpolates away before the inner powershell runs; every reflection probe hand-rolled the utf-16le/base64/`-EncodedCommand` workaround | resolved (`scripts/ps-remote`, T14) | [2026-07-08](2026-07-08.md) |
| 34 | Default-endpoint `Currency` entity is the CM201000 currency *list* only — `UseForAccounting: true` creates no CM202000 financial-currency row, so EUR-denominated GL accounts 422 "Currency cannot be found" even with Multicurrency enabled | open (T29: Bootstrap-endpoint entity) | [2026-07-09](2026-07-09.md) |
| 35 | SalesDemo-extract replay: `AccountGroup` is PM201000 (Projects-gated and absent from the extract) and `ChartOfAccountsOrder` is server-derived — extracted config carrying either fails to apply or diffs dirty | workaround (strip references and derived fields at extract) | [2026-07-09](2026-07-09.md) |

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
  (CS206500) (SPEC T12); company + credit terms now seed from
  `bootstrap/*.yaml` in the data repo with the write path live-verified
  (SPEC T13); the plugin's feature set is data-driven from
  `bootstrap/features.yaml` (SPEC T24).
- `[OPEN]` Baseline expanded in dependency order: currencies → financial
  calendar → chart of accounts/ledger → tax categories/zones →
  customer/vendor/item classes → payment terms.
- `[OPEN]` Drift proof: provision two tenants, diff config, zero difference.
- `[OPEN]` Timing captured (manual baseline vs automated).
- `[OPEN]` Repo clean and runnable; README shows `acu provision` reproducing
  the numbers.

Target config domains (what a configured tenant must carry): chart of
accounts (GL), customer/vendor classes, payment terms, tax zones/categories,
item classes, UOMs, currencies, branches/company structure.
