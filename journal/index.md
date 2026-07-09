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
  `Bootstrap/1.0.0` answering, bootstrap YAML seeding unblocked.
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
| 11 | With a stale tenant map, REST login accepts *any* tenant name and silently lands on the default tenant — config-as-code writes to the wrong tenant | resolved (recycle + always send explicit valid `tenant`) | [2026-07-07](2026-07-07.md), [2026-07-08](2026-07-08.md) |
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

## Status

Mechanisms:

1. **Tenant provisioning as code** (`acu tenant list|create|delete`, ac.exe
   over SSH) — `[DONE]` — create chains create → recycle → login-ready admin
   in one command; delete round-trips it.
2. **Snapshot-based baseline** — `[DEAD END]` — no CLI snapshot support;
   baseline lives entirely in reference-data-as-code.
3. **Reference data as code** (`acu apply` / `acu diff`, YAML → REST upserts)
   — `[IN PROGRESS]` — proven end-to-end on `baseline/uoms.yaml`; the
   bootstrap customization package (features + company/branch + payment
   terms via CustomizationApi) is the gating task.
4. **One-command provisioning** (`acu provision`: create → bootstrap →
   apply → diff) — `[DONE (code)]` — command lands with offline tests; the
   bootstrap step is gated on the package publish working end to end.

Remaining milestones:

- `[DONE]` Bootstrap package published via CustomizationApi — the C#
  `CustomizationPlugin` enables features on publish (SPEC T11) and the
  `Bootstrap/1.0.0` endpoint exposes company (CS101500) + credit terms
  (CS206500) (SPEC T12); both live-verified, provision E2E green on a
  virgin tenant. Remaining: seed company + credit terms YAML through it.
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
