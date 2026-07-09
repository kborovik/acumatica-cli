# SPEC — acu CLI (acumatica-cli)

## §G GOAL

Configure Acumatica ERP purely from source — no UI, no Configuration Wizard. Idempotent ops: `apply` (keyed upserts), `diff` (drift detect, exit 2 on drift), ultimately `acu provision` — one command chains tenant create → bootstrap → apply `baseline/` → diff. Done = clone data repo, decrypt, run one command → byte-identical configured tenant every time.

## §C CONSTRAINTS

- Repo = `acu` CLI only. Data (`baseline/`, `acu.yaml`, `.env.gpg`) lives in separate data repos (sibling `acumatica-baseline`); infra = sibling `acumatica-infra`; blog = sibling `acumatica-blog`. C# customization projects out of scope (exclusion: bootstrap package ships one C# CustomizationPlugin package — §T.11; endpoint item returns when §T.12 lands).
- Python ≥ 3.12; click, httpx, pydantic, rich, pyyaml, python-dotenv; uv build; module `acumatica_cli`; entry `acu`.
- Default suite fully offline: SSH = monkeypatched `subprocess.run`, REST = `httpx.MockTransport`; no live instance needed; `make check` gate offline-only. Opt-in live tier: pytest marker `e2e`, deselected via addopts, runs only via `make e2e`.
- Every cmd except `--dry-run` talks to live instance. Final verification live vs acu-dev1 (`acu-dev1.vm.internal`; needs tailnet + GPG key): `cd ~/github/acumatica-baseline && make decrypt && make diff` or `make e2e` here (data symlinks `acu.yaml`/`.env`/`baseline`/`bootstrap` → sibling data repo).
- Unconfigured tenant fails most entities (500 `PXSetupNotEnteredException` or 403 feature-gated) until bootstrap (features + company/branch); built-in endpoints can't bootstrap; `PUT CompaniesStructure` dead; payment terms have no Default-endpoint entity.
- `ac.exe` has no snapshot support — reference data as code is the primary route.

## §I INTERFACES

- cmd: `acu [-t <tenant>] [--version] <subcommand>` → globals valid only before subcommand
- cmd: `acu tenant list|create|delete` → tenant CRUD over SSH; create: `--id` ! + `--login` ! + `--type`/`--parent`/`--hidden`/`--no-init` ?; delete: `--id` + confirm prompt, `--yes` skips
- cmd: `acu apply [--dry-run] <files|dirs>` → PUT each record; dir arg expands `*.yaml`; dry-run lines `would PUT …`, summary suffixed `(dry run)`
- cmd: `acu diff <files|dirs>` → GET by `$filter` on key fields, compare normalized; drift → exit 2
- cmd: `acu provision --id <n> --login <name> [--type] [--parent]` → chains tenant create → bootstrap publish → apply `baseline/` → diff; resumable — skips done steps
- cmd: `acu schema [--out <dir>]` → OpenAPI dump → `schemas/` (gitignored ~3 MB; regenerate, never version)
- cmd: `acu config show` → emit complete valid YAML config doc: header comment (creds live in `.env`, never config) + resolved top-level instance map (defaults merged, URLs constructed); `username`/`password` excluded → output round-trips through `load_instance` (`acu config show > acu.yaml` reloads identical); ! same `load_instance` path as live cmds — no parallel resolution
- cfg: `acu.yaml` → flat top-level map = the single target instance: `host` ! only required key, rest ? overrides of code defaults (pydantic field defaults on `Instance`; `base_url`/`ssh` computed from `host`, explicit override wins); no `instances` nesting, no `default_instance`; discovery sentinel; empty or non-map file → hard error
- env: `ACU_PASSWORD` ! set; `ACU_USER` ? default `admin`; loaded from dir of found `acu.yaml`; encrypted at rest as `.env.gpg`
- data: `baseline/*.yaml` → `entity` / `key` (string or list) / `records` + `endpoint` ? (per-file contract-endpoint override, e.g. `Bootstrap/1.0.0`); parsed by `seed.py`
- api: `/entity/Default/25.200.001/` → cookie-session httpx; values wrapped `{"Field": {"value": ...}}`; PUT = keyed upsert
- ssh: `ac.exe -cm:CompanyConfig` + `sqlcmd` over `ssh` → remote shell PowerShell; `exit $LASTEXITCODE` appended so failures propagate

## §V INVARIANTS

V1: two-plane split — control plane = SSH (`tenant.py`, tenant CRUD only); data plane = REST (`client.py`); never mixed
V2: three source kinds never mixed — `baseline/*.yaml` = what, `acu.yaml` = where (never what, never secrets), `.env` = secrets; all three live in data repos, not here
V3: discovery — walk up from cwd to first dir containing `acu.yaml`; `.env` loaded from same dir; none found → hard error
V4: idempotence — `PUT` keyed upsert is the primitive; `diff` treats source as authoritative, extra live records not flagged; drift → exit 2; resume/skip gate ! verify desired state, never a marker — marker outlives state loss
V5: tenant-map — tenant create ! `AcumaticaERP` app-pool recycle after `ac.exe` (stale map → tenant missing from sign-in + REST silently routes to default tenant); always send explicit valid `tenant`
V6: `AcumaticaClient` ! context manager — logout even on failure (sessions count vs license API-user cap); logout ! `Content-Length: 0` (else IIS 411)
V7: `CompanyConfig` ! `-h` beside `-iname` + `-dbnew:"False"`; delete uses `Deleted` sub-key + full spec (`ParentID` + `CompanyType`)
V8: tenant create presets admin via `-aun`/`-aup`/`-auc` — contract API can't clear `PasswordChangeOnNextLogin`; `Login.aspx` screen flow = fallback only
V9: output — everything through `output.py`, no bare `print()`; stdout = data, stderr = process; ASCII-only every path; exit 0 ok, 1 error, 2 drift; no `--json` — plain text = machine interface; full audit recipe → `.claude/check-extras.md` §V.9
V10: every model inherits `models.Model` (pydantic frozen, `extra="forbid"`) — validate at boundary, unknown fields error; mechanical form: `^class .*BaseModel` outside `models.py` = violation (`.claude/scripts/check-extras.sh` scan); class w/o pydantic base = not a model, exempt
V11: REST targets versioned path only (`Default/25.200.001`), never unversioned alias
V12: `docs/ac-exe.md` + `docs/rest-api.md` verified vs live 26.101.0225 — trust over training data, re-verify on upgrade; dumped schema (`acu schema`) = authoritative field reference
V13: `make check` (ruff + basedpyright strict + offline pytest) before every commit
V14: journal — after meaningful work append/extend `journal/YYYY-MM-DD.md` + sync `journal/index.md`; dead ends stay in (findings, not noise)
V15: cmd grammar — exactly two forms: `acu [globals] <noun> <verb> [options]` = resource ops (`tenant` = control plane; `config` = local read-only, no live instance); `acu [globals] <verb> [options] [args]` = data plane + pipeline; no third form; surface encodes V1 split
V16: option conventions — globals valid only before subcommand; resource identity = explicit `--id`, never positional; `--dry-run` wherever mutation; destructive ops confirm prompt, `--yes` skips; full convention audit → `.claude/check-extras.md` §V.16
V17: §T verify gate ! satisfiable vs current spec state — criterion never depends on capability another § records dead/pending unless citing the unblocking §T row
V18: `_ssh` appends `exit $LASTEXITCODE` to every remote command — single choke point, call sites never hand-append (PowerShell-over-ssh returns 0 on failed native cmd)

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
T18|x|mechanize §V.9 ASCII audit — `scripts/check-ascii <paths>`: `.py` via tokenize (exempt COMMENT tokens + docstrings), `.cs` exempt `//` lines, `.xml` exempt `<!-- -->`; emit surviving `file:line` violations, exit 1 on match; same commit flips check-extras §V.9 recipe cmd + drops eye-applied exemption filter|V9
T19|x|mechanize §V.1/§V.10/§V.18 drift greps into `.claude/scripts/check-extras.sh` — emit `id|verdict|evidence` rows per /sdd:check extras-hook contract: V1 plane-split scan (imports: `tenant.py` bans `httpx`, `client.py` bans `subprocess`), V10 inheritance scan (`^class ` in `src/` ! inherit `Model` outside `models.py`), V18 choke-point scan (`exit \$LASTEXITCODE` sole site `_ssh`); same commit appends the three recipe rows to `.claude/check-extras.md`|V1,V10,V18
T20|x|live E2E tier — `tests/e2e/test_provision_lifecycle.py` drives real `acu` binary vs live instance: provision scratch tenant `E2E` (next-free CompanyID) → independent diff clean → provision re-run hits skip paths → injected-drift diff exit 2; session fixture always deletes tenant + recycles; `make e2e` preflights data symlinks; marker `e2e` deselected by default|V4,V5,V9,V13

## §B BUGS

id|date|cause|fix
B1|2026-07-08|§T.10 live gate cited provision E2E; §T.3 verdict already records bootstrap publish dead, C# plugin fallback never queued|V17
B2|2026-07-08|CustomizationApi import field `projectContents` + hyphenated project name from training data — live binder wants `projectContentBase64`, names alphanumeric; content bound null, import silently no-op|V12
B3|2026-07-08|publish skip keyed on `getPublished` marker — tenant recreate same CompanyID keeps stale publication row while content + plugin writes gone; virgin tenant left unbootstrapped|V4
B4|2026-07-09|sqlcmd list call omitted `exit $LASTEXITCODE` — PowerShell-over-ssh returns 0 on failed native cmd; failed read → empty tenant list, provision misjudges existence|V18
