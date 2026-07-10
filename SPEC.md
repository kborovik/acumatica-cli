# SPEC — acu CLI (acumatica-cli)

## §G GOAL

Configure Acumatica ERP purely from source — no UI, no Configuration Wizard. Idempotent ops: `apply` (keyed upserts), `diff` (drift detect, exit 2 on drift), ultimately `acu provision` — one command chains tenant create → bootstrap → apply `baseline/` → diff. Done = clone data repo, decrypt, run one command → byte-identical configured tenant every time.

## §C CONSTRAINTS

- Repo = `acu` CLI only. Data (`baseline/`, `acu.yaml`, `.env.gpg`) lives in separate data repos (sibling `acumatica-baseline`); infra = sibling `acumatica-infra`; blog = sibling `acumatica-blog`. C# customization projects out of scope (exclusion: bootstrap package ships one C# CustomizationPlugin package — §T.11; endpoint item returns when §T.12 lands).
- Python ≥ 3.12; click, httpx, pydantic, rich, pyyaml, python-dotenv; uv build; module `acumatica_cli`; entry `acu`.
- Default suite fully offline: SSH = monkeypatched `subprocess.run`, REST = `httpx.MockTransport`; no live instance needed; `make check` gate offline-only. Opt-in live tier: pytest marker `e2e`, deselected via addopts, runs only via `make e2e`.
- Every cmd except `--dry-run` talks to live instance. Final verification live vs acu-dev1 (`acu-dev1.vm.internal`; needs tailnet + GPG key): `cd ~/github/acumatica-baseline && make decrypt && make diff` or `make e2e` here (data symlinks `acu.yaml`/`.env`/`baseline`/`bootstrap` → sibling data repo).
- Unconfigured tenant fails most entities (500 `PXSetupNotEnteredException` or 403 feature-gated) until bootstrap (features + company/branch); built-in endpoints can't bootstrap; `PUT CompaniesStructure` dead; payment terms have no Default-endpoint entity; financial currency (CM202000) has none either — REST `Currency` = CM201000 currency list only.
- `ac.exe` has no snapshot support — reference data as code is the primary route.

## §I INTERFACES

- cmd: `acu [-t <tenant>] [--host <host>] [--version] <subcommand>` → globals valid only before subcommand; `--host` swaps acu.yaml `host` pre-`Instance` build → `base_url`/`ssh` re-derive; explicit acu.yaml `base_url`/`ssh` still win; `--version` → editable install (PEP 610 `direct_url.json` `dir_info.editable`) renders `<version>+dev (<checkout path>)`, release install plain `<version>`
- cmd: `acu tenant list|create|delete` → tenant CRUD over SSH; create: `--id` ! + `--login` ! + `--type`/`--parent`/`--hidden`/`--no-init` ?; delete: `--id` + confirm prompt, `--yes` skips
- cmd: `acu apply [--dry-run] <files|dirs>` → PUT each record; dir arg expands `*.yaml`; dry-run lines `would PUT …`, summary suffixed `(dry run)`
- cmd: `acu diff <files|dirs>` → GET by `$filter` on key fields, compare normalized; optimization-500 (delegate-view fields) → retry record via key-URL GET `/<entity>/<key1>[/<key2>...]` (YAML `key` order); drift → exit 2
- cmd: `acu provision --id <n> --login <name> [--type] [--parent]` → chains tenant create → bootstrap publish → apply `baseline/` → diff; resumable — skips done steps
- cmd: `acu schema [--out <dir>]` → OpenAPI dump → `schemas/` (gitignored ~3 MB; regenerate, never version)
- cmd: `acu config show` → emit complete valid YAML config doc: header comment (creds live in `.env`, never config) + resolved top-level instance map (defaults merged, URLs constructed); `username`/`password` excluded → output round-trips through `load_instance` (`acu config show > acu.yaml` reloads identical); ! same `load_instance` path as live cmds — no parallel resolution
- cmd: `acu config init [--host <host>] [<dir>]` → scaffold data repo: `acu.yaml` (`host` from flag or placeholder `erp.example.com` + header comment → `acu config show`), `.env` placeholders (`ACU_PASSWORD=` + `ACU_USER=admin`, never real secrets), `baseline/uoms.yaml` (valid 2-record example), `bootstrap/company.yaml` + `bootstrap/credit-terms.yaml` (per-file `endpoint:` override example) + `bootstrap/features.yaml` (built-in six explicit), `.gitignore` (`.env`, `schemas/`); `<dir>` ? cwd, created if absent; per-file skip-if-exists → `skip <file> (exists)`, exit 0, never overwrites; templates = package data via `importlib.resources`; no `git init`, no gpg
- cmd: `acu config check` → read-only preflight, dependency order, one `ok`/`fail` line each: discovery (walk-up + parse + `host`) → secrets (`.env` + `ACU_PASSWORD`) → REST (login + landed-tenant verify + logout) ∥ ssh (trivial remote cmd via `_ssh`); REST + ssh probe independently, discovery/secrets fail stops; no PUTs, no tenant CRUD; exit 0 all pass, 1 any fail (2 stays drift)
- cfg: `acu.yaml` → flat top-level map = the single target instance: `host` ! only required key, rest ? overrides of code defaults (pydantic field defaults on `Instance`; `base_url`/`ssh` computed from `host`, explicit override wins); no `instances` nesting, no `default_instance`; discovery sentinel; empty or non-map file → hard error
- env: `ACU_PASSWORD` ! set; `ACU_USER` ? default `admin`; loaded from dir of found `acu.yaml`; encrypted at rest as `.env.gpg`
- data: `baseline/*.yaml` → `entity` / `key` (string or list) / `records` + `endpoint` ? (per-file contract-endpoint override, e.g. `Bootstrap/1.0.0`); parsed by `seed.py`
- data: `bootstrap/features.yaml` → ? list of `FeaturesSet` property names (DAC bit flags) → plugin `Enabled` set @ package build; absent → built-in six (FinancialModule, FinancialStandard, DistributionModule, Inventory, Branch, MultiCompany)
- api: `/entity/Default/25.200.001/` → cookie-session httpx; values wrapped `{"Field": {"value": ...}}`; PUT = keyed upsert
- ssh: `ac.exe -cm:CompanyConfig` + `sqlcmd` over `ssh` → remote shell PowerShell; `exit $LASTEXITCODE` appended so failures propagate
- pkg: pypi.org `acumatica-cli` → `pip install acumatica-cli`; publish = GitHub Actions `release.yml` on `release: published`, trusted publishing (OIDC), `uv build` sdist+wheel; no stored API token

## §V INVARIANTS

V1: two-plane split — control plane = SSH (`tenant.py`, tenant CRUD only); data plane = REST (`client.py`); never mixed
V2: three source kinds never mixed — `baseline/*.yaml` = what, `acu.yaml` = where (never what, never secrets), `.env` = secrets; all three live in data repos, not here; package-embedded config = what — bootstrap feature set sources from data-repo `bootstrap/features.yaml`, never hardcoded in plugin source
V3: discovery — walk up from cwd to first dir containing `acu.yaml`; `.env` loaded from same dir; none found → hard error
V4: idempotence — `PUT` keyed upsert is the primitive; `diff` treats source as authoritative, extra live records not flagged; drift → exit 2; resume/skip gate ! verify desired state, never a marker — marker outlives state loss; published-package skip ! content parity (embedded content digest), never existence alone — stale content silently starves config; diff read-back ! survive delegate-view entities — list-GET optimization-500 → key-URL single-record GET fallback (closes §B.9)
V5: tenant-map — tenant create ! `AcumaticaERP` app-pool recycle after `ac.exe` (stale map → tenant missing from sign-in + REST silently routes to default tenant); always send explicit valid `tenant`; stale map reroutes named tenants too — data-plane session ! post-login landed-tenant verify, refuse on mismatch (probe discovery → §T.21)
V6: `AcumaticaClient` ! context manager — logout even on failure (sessions count vs license API-user cap); logout ! `Content-Length: 0` (else IIS 411)
V7: `CompanyConfig` ! `-h` beside `-iname` + `-dbnew:"False"`; delete uses `Deleted` sub-key + full spec (`ParentID` + `CompanyType`)
V8: tenant create presets admin via `-aun`/`-aup`/`-auc` — contract API can't clear `PasswordChangeOnNextLogin`; `Login.aspx` screen flow = fallback only
V9: output — everything through `output.py`, no bare `print()`; stdout = data, stderr = process; ASCII-only every path; exit 0 ok, 1 error, 2 drift; no `--json` — plain text = machine interface; full audit recipe → `.spec/check-extras.md` §V.9
V10: every model inherits `models.Model` (pydantic frozen, `extra="forbid"`) — validate at boundary, unknown fields error; mechanical form: `^class .*BaseModel` outside `models.py` = violation (`.spec/scripts/check-extras.sh` scan); class w/o pydantic base = not a model, exempt
V11: REST targets versioned path only (`Default/25.200.001`), never unversioned alias
V12: `docs/ac-exe.md` + `docs/rest-api.md` verified vs live 26.101.0225 — trust over training data, re-verify on upgrade; dumped schema (`acu schema`) = authoritative field reference
V13: `make check` (ruff + basedpyright strict + offline pytest) before every commit
V14: journal — after meaningful work append/extend `journal/YYYY-MM-DD.md` + sync `journal/index.md`; dead ends stay in (findings, not noise)
V15: cmd grammar — exactly two forms: `acu [globals] <noun> <verb> [options]` = resource ops (`tenant` = control plane; `config` = configuration ops: `init` local write, `show` local read, `check` live read-only preflight); `acu [globals] <verb> [options] [args]` = data plane + pipeline; no third form; surface encodes V1 split
V16: option conventions — globals valid only before subcommand; resource identity = explicit `--id`, never positional; `--dry-run` wherever mutation; destructive ops confirm prompt, `--yes` skips; full convention audit → `.spec/check-extras.md` §V.16
V17: §T verify gate ! satisfiable vs current spec state — criterion never depends on capability another § records dead/pending unless citing the unblocking §T row
V18: `_ssh` appends `exit $LASTEXITCODE` to every remote command — single choke point, call sites never hand-append (PowerShell-over-ssh returns 0 on failed native cmd)
V19: release pipeline — `make release <part>` sole release path (bump + commit + tag + gh release); release published → `release.yml` builds (`uv build`) + publishes to pypi.org via trusted publishing (OIDC); no PyPI API token in repo or GitHub secrets; tag `v<version>` == pyproject `version`
V20: seed endpoint routing ! unambiguous — baseline `entity` named in shipped Bootstrap template ! explicit per-file `endpoint:`; absent → hard error naming both endpoints, never silent Default-endpoint PUT (§B.8 class — Bootstrap `Currency` vs Default CM201000 list; symptom returns behind clean apply)
V21: endpoint contract identity = name+version — entity or field shape change in `bootstrap_project.xml` ! version bump; version held → older build's digest gate republishes prior contract under same identity (silent downgrade, no version signal in seed failures)
V22: baseline reference closure — record field referencing another entity ! that entity exist @ PUT time: tenant-native or created by earlier-sorting baseline file (dir expansion alphabetical = sole ordering mechanism; filename prefixes encode order); extract-derived files ! strip fields referencing entities outside the baseline set (AccountGroup class) + server-derived fields — PUT-tolerated, server keeps own derivation, sourced value = permanent drift (ChartOfAccountsOrder/CashAccount class, Translation* sibling)

## §T TASKS

id|status|task|cites
T1|x|build `acu provision` — chain tenant create → bootstrap → apply `baseline/` → diff|V4,V5
T2|x|bootstrap package — publish via `/CustomizationApi`; custom endpoint exposes CS100000 features + CS101500 company/branch + CS206500 credit terms|I.api
T3|x|verify CS100000 accepts writes via custom endpoint ? — fallback: `CustomizationPlugin` flips `FeaturesSet` on publish (ships C#)|T2
T4|x|post-login tenant guard in seeding pipeline — defense-in-depth vs wrong-tenant writes|V5
T5|x|ASCII sweep — replace non-ASCII glyphs in all output-reaching strings|V9
T6|x|drop `docs/cli.md` — contract folded into §I/§V; drop ref from CLAUDE.md|V9
T7|x|drift exit code 1 → 2 in `diff` + `provision`; ripple: `acumatica-baseline` `make diff` + any consumer treating exit 1 as drift|V4,V9
T8|x|drop `acu bootstrap` cmd — `bootstrap.publish()` module stays; resumable `provision` = recovery route|I.cmd
T9|x|drop `schema -o` short flag — `--out` only|V16
T10|x|layered `Instance` defaults per `designs/config-layered-defaults.md` — `host` sole required config key, rest code defaults; add `acu config show`|V11,V12,I.cfg
T11|x|C# CustomizationPlugin flips FeaturesSet on publish — ships in bootstrap package; unblocks provision E2E|T2,T3
T12|x|discover `.endpoint` package-file serialization — restore custom endpoint to bootstrap package; unblocks bootstrap YAML seeding|T2,T11
T13|x|seed company + credit terms through `Bootstrap/1.0.0` — author `baseline/` YAML in data repo, verify live|T12,I.data
T14|x|`scripts/ps-remote <file.ps1> [host]` — mechanize the live-box PowerShell reflection probe|V12
T15|x|config TOML → YAML: sentinel `acu.toml` → `acu.yaml`, loader `tomllib` → `yaml.safe_load`; migrate data repo|V2,V3,I.cfg
T16|x|flatten config to single instance — drop `instances.<name>` nesting, `default_instance`, `-i/--instance` global, `Instance.name` field|V16,I.cfg,T15
T17|x|centralize `exit $LASTEXITCODE` in `_ssh`; strip hand-appended suffixes at call sites — sweep grep `self\._ssh\(`|V18
T18|x|mechanize §V.9 ASCII audit — `.spec/scripts/check-ascii <paths>`: `.py` via tokenize (exempt COMMENT tokens + docstrings), `.cs` exempt `//` lines, `.xml` exempt `<!-- -->`; emit surviving `file:line` violations, exit 1 on match; same commit flips check-extras §V.9 recipe cmd + drops eye-applied exemption filter|V9
T19|x|mechanize §V.1/§V.10/§V.18 drift greps into `.spec/scripts/check-extras.sh` — emit `id|verdict|evidence` rows per /sdd:check extras-hook contract: V1 plane-split scan (imports: `tenant.py` bans `httpx`, `client.py` bans `subprocess`), V10 inheritance scan (`^class ` in `src/` ! inherit `Model` outside `models.py`), V18 choke-point scan (`exit \$LASTEXITCODE` sole site `_ssh`); same commit appends the three recipe rows to `.spec/check-extras.md`|V1,V10,V18
T20|x|live E2E tier — `tests/e2e/test_provision_lifecycle.py` drives real `acu` binary vs live instance: provision scratch tenant `E2E` (next-free CompanyID) → independent diff clean → provision re-run hits skip paths → injected-drift diff exit 2; session fixture always deletes tenant + recycles; `make e2e` preflights data symlinks; marker `e2e` deselected by default|V4,V5,V9,V13
T21|x|post-login tenant guard — discover verified landed-tenant probe (live archaeology: login response? entity exposing `CompanyKey`?), then `AcumaticaClient.__enter__` refuses session on mismatch; e2e regression: `diff` vs nonexistent tenant ! exit 1|V5,V12
T22|x|global `--host` flag — swap acu.yaml `host` pre-`Instance` build (post-hoc `model_copy` leaves derived `base_url`/`ssh` stale); explicit `base_url`/`ssh` override wins; acu.yaml stays required, `-t` override idiom; tests: re-derive on override + explicit-`base_url` precedence|V16,I.cmd,I.cfg
T23|x|PyPI auto-publish — register trusted publisher on pypi.org (repo kborovik/acumatica-cli, workflow `release.yml`); add `.github/workflows/release.yml`: trigger `release: published`, `uv build`, `pypa/gh-action-pypi-publish` OIDC; verify: next `make release patch` → `pip install acumatica-cli==<version>` from pypi.org succeeds|V19,I.pkg
T24|x|data-driven bootstrap features — `package_zip()` injects `Enabled` list into `bootstrap_plugin.cs` from data-repo `bootstrap/features.yaml`; file absent → built-in six; plugin logs names matching no `FeaturesSet` property (silent-typo guard); author `features.yaml` in data repo (six + `Multicurrency` + `SubAccount`) + verify live: SalesDemo-extract replay onto fresh tenant passes Subaccount + Account applies|V2,T11
T25|x|content-aware publish gate — `publish()` embeds deterministic content digest (project.xml bytes) in package description; skip ! published + project exists + digest match; digest mismatch → reimport + republish; offline tests pin mismatch→republish path|V4,T24
T26|x|`acu config init` — scaffold data repo per §I.cmd row: 7-file template set via `importlib.resources`, per-file skip-if-exists; build after T24 (features.yaml template copies verified format); verify: empty dir → `init --host h` → `config show` succeeds + `apply --dry-run bootstrap/ baseline/` parses all templates; re-run → all `skip`, zero mutations, exit 0|V2,V3,V9,V15,T24
T27|x|`acu config check` — four-probe read-only preflight per §I.cmd row; verify: healthy instance → 4x `ok` exit 0; wrong `ACU_PASSWORD` → REST `fail` while ssh still reports, exit 1; live state unchanged either way|V3,V5,V6,V9,V15,V18
T28|x|dev-version marker — `--version` reads own dist `direct_url.json` (PEP 610); `dir_info.editable` true → `<version>+dev (<checkout path>)`, else plain `<version>`; no build-backend change, `uv version --bump` release flow intact; offline tests: editable metadata → `+dev` suffix, wheel/no-`direct_url.json` → plain|V19,I.cmd
T29|x|extend Bootstrap endpoint w/ financial-currency entity (CM202000) — verify: PUT EUR via `Bootstrap/1.0.0` on fresh tenant → EUR-denominated account applies|T12,V12,I.data
T30|x|diff key-URL fallback per §I.cmd row — `seed.diff` catches optimization-500, retries record via single-record key-URL GET (B9); offline tests: fallback round-trip + non-optimization 500 still raises; live: `acu diff` clean over T29 currencies scratch YAML|V4,V12
T31|x|graduate T29 scratch into data-repo `baseline/` — author `10-subaccounts.yaml` + `20-accounts.yaml` (real chart-of-accounts values, not scratch 8710/8720; `AccountGroup` + `RevaluationRateType` + server-derived `ChartOfAccountsOrder`/`CashAccount` stripped — fresh tenant lacks PM account groups; server keeps own derivation) + `30-currencies.yaml` (`endpoint: Bootstrap/1.0.0` override; Translation* pairs omitted while translation feature off — write-tolerated, read-invisible); numbered prefixes encode apply order (subaccounts < accounts < currencies < currency-denominated accounts; dir expansion sorts alphabetical); verify: provision fresh scratch tenant zero manual steps → clean diff over everything applied, currencies included|V2,V4,V22,I.data,T29,T30
T32|x|enforce V20 — `seed.py` parses packaged `bootstrap_project.xml` entity names; baseline file w/ `entity` in that set + no `endpoint:` → hard error; offline tests: ambiguous file bails, explicit override passes|V20,V9,I.data
T33|.|bump Bootstrap endpoint 1.0.0 → 1.1.0 (T29 changed contract shape under held version) — patch `bootstrap_project.xml` + `seed.py` docstring + `templates/bootstrap/*.yaml` + test fixtures + data-repo `endpoint:` refs + §I.data example; verify: republish, `acu diff` clean via `Bootstrap/1.1.0`|V21,T29,I.data

## §B BUGS

id|date|cause|fix
B1|2026-07-08|§T.10 live gate cited provision E2E; §T.3 verdict already records bootstrap publish dead, C# plugin fallback never queued|V17
B2|2026-07-08|CustomizationApi import field `projectContents` + hyphenated project name from training data — live binder wants `projectContentBase64`, names alphanumeric; content bound null, import silently no-op|V12
B3|2026-07-08|publish skip keyed on `getPublished` marker — tenant recreate same CompanyID keeps stale publication row while content + plugin writes gone; virgin tenant left unbootstrapped|V4
B4|2026-07-09|sqlcmd list call omitted `exit $LASTEXITCODE` — PowerShell-over-ssh returns 0 on failed native cmd; failed read → empty tenant list, provision misjudges existence|V18
B5|2026-07-09|manual tenant delete w/o recycle → stale map; named-tenant REST login silently rerouted to default — `diff` false-green; client guard refuses empty tenant only|V5
B6|2026-07-09|bootstrap plugin hardcodes six-feature `Enabled` set in C# — feature flags = config "what" living in tool source; SalesDemo config replay onto bootstrapped tenant: Subaccount PUT 403 (SubAccount off), Account PUT 500 (Multicurrency off)|V2
B7|2026-07-09|publish skip gate = project existence, not content parity — changed package content silently skips republish (B3 class, one notch subtler)|V4
B8|2026-07-09|REST `Currency` entity = CM201000 currency list only — `UseForAccounting` write creates no CM.Currency row; EUR-denominated Account PUT 422 persists w/ Multicurrency on (B6 Account-500 attribution half-right: feature bit AND missing financial-currency row)|-
B9|2026-07-10|contract-API list GET = optimized export — 500s when any field in scope maps to a BQL-delegate view (Bootstrap Currency GL fields -> CuryRecords); PUT persists but `diff` cannot read back|V4
B10|2026-07-10|T31 file naming left apply order to alphabetical accident — currencies.yaml 422 on SUB refs from later-sorting subaccounts.yaml; extract-derived accounts.yaml kept AccountGroup refs absent on fresh tenant|V22
B11|2026-07-10|extract-derived accounts YAML carried server-derived fields (ChartOfAccountsOrder, CashAccount) — PUT-tolerated, server keeps own derivation, diff dirty forever|V22
