# SPEC — acu CLI (acumatica-cli)

## §G GOAL

Configure Acumatica ERP purely from source — no UI, no Configuration Wizard. Idempotent ops: `apply` (keyed upserts), `diff` (drift detect, exit 1 on drift), ultimately `acu provision` — one command chains tenant create → bootstrap → apply `baseline/` → diff. Done = clone data repo, decrypt, run one command → byte-identical configured tenant every time.

## §C CONSTRAINTS

- Repo = `acu` CLI only. Data (`baseline/`, `acu.toml`, `.env.gpg`) lives in separate data repos (sibling `acumatica-baseline`); infra = sibling `acumatica-infra`; blog = sibling `acumatica-blog`. C# customization projects out of scope (exclusion ?: bootstrap package §T.2 ships one custom endpoint package).
- Python ≥ 3.12; click, httpx, pydantic, rich, pyyaml, python-dotenv; uv build; module `acumatica_cli`; entry `acu`.
- Tests fully offline: SSH = monkeypatched `subprocess.run`, REST = `httpx.MockTransport`; no live instance needed.
- Every cmd except `--dry-run` talks to live instance. Final verification live vs acu-dev1 (`acu-dev1.vm.internal`; needs tailnet + GPG key): `cd ~/github/acumatica-baseline && make decrypt && make diff`.
- Unconfigured tenant fails most entities (500 `PXSetupNotEnteredException` or 403 feature-gated) until bootstrap (features + company/branch); built-in endpoints can't bootstrap; `PUT CompaniesStructure` dead; payment terms have no Default-endpoint entity.
- `ac.exe` has no snapshot support — reference data as code is the primary route.

## §I INTERFACES

- cmd: `acu [-i <instance>] [-t <tenant>] <subcommand>` → global instance/tenant selection; `--version`
- cmd: `acu tenant list|create|delete` → tenant CRUD over SSH; create: `--id` ! + `--login` ! + `--type`/`--parent`/`--hidden` ?; delete: `--id` + confirm prompt
- cmd: `acu apply [--dry-run] <files|dirs>` → PUT each record; dir arg expands `*.yaml`; dry-run lines `would PUT …`, summary suffixed `(dry run)`
- cmd: `acu diff <files|dirs>` → GET by `$filter` on key fields, compare normalized; drift → exit 1
- cmd: `acu schema` → OpenAPI dump → `schemas/` (gitignored ~3 MB; regenerate, never version)
- cfg: `acu.toml` → `[instances.<name>]` tables (URLs, SSH target, DB name) + `default_instance`; discovery sentinel
- env: `ACU_PASSWORD` ! set; `ACU_USER` ? default `admin`; loaded from dir of found `acu.toml`; encrypted at rest as `.env.gpg`
- data: `baseline/*.yaml` → `entity` / `key` (string or list) / `records`; parsed by `seed.py`
- api: `/entity/Default/25.200.001/` → cookie-session httpx; values wrapped `{"Field": {"value": ...}}`; PUT = keyed upsert
- ssh: `ac.exe -cm:CompanyConfig` + `sqlcmd` over `ssh` → remote shell PowerShell; `exit $LASTEXITCODE` appended so failures propagate

## §V INVARIANTS

V1: two-plane split — control plane = SSH (`tenant.py`, tenant CRUD only); data plane = REST (`client.py`); never mixed
V2: three source kinds never mixed — `baseline/*.yaml` = what, `acu.toml` = where (never what, never secrets), `.env` = secrets; all three live in data repos, not here
V3: discovery — walk up from cwd to first dir containing `acu.toml`; `.env` loaded from same dir; none found → hard error
V4: idempotence — `PUT` keyed upsert is the primitive; `diff` treats source as authoritative, extra live records not flagged; drift → exit 1
V5: tenant-map — tenant create ! `AcumaticaERP` app-pool recycle after `ac.exe` (stale map → tenant missing from sign-in + REST silently routes to default tenant); always send explicit valid `tenant`
V6: `AcumaticaClient` ! context manager — logout even on failure (sessions count vs license API-user cap); logout ! `Content-Length: 0` (else IIS 411)
V7: `CompanyConfig` ! `-h` beside `-iname` + `-dbnew:"False"`; delete uses `Deleted` sub-key + full spec (`ParentID` + `CompanyType`)
V8: tenant create presets admin via `-aun`/`-aup`/`-auc` — contract API can't clear `PasswordChangeOnNextLogin`; `Login.aspx` screen flow = fallback only
V9: output — everything through `output.py`, no bare `print()` (ruff T20); stdout = data, stderr = process; exit 0 ok, 1 error or drift; expected failure = one `✗` line, no traceback (`ACU_DEBUG=1` re-raises)
V10: every model inherits `models.Model` (pydantic frozen, `extra="forbid"`) — validate at boundary, unknown fields error
V11: REST targets versioned path only (`Default/25.200.001`), never unversioned alias
V12: `docs/ac-exe.md` + `docs/rest-api.md` verified vs live 26.101.0225 — trust over training data, re-verify on upgrade; dumped schema (`acu schema`) = authoritative field reference
V13: `make check` (ruff + basedpyright strict + offline pytest) before every commit
V14: journal — after meaningful work append/extend `journal/YYYY-MM-DD.md` + sync `journal/index.md`; dead ends stay in (findings, not noise)

## §T TASKS

id|status|task|cites
T1|.|build `acu provision` — chain tenant create → bootstrap → apply `baseline/` → diff|V4,V5
T2|.|bootstrap package — publish via `/CustomizationApi`; custom endpoint exposes CS100000 features + CS101500 company/branch + CS206500 credit terms|I.api
T3|.|verify CS100000 accepts writes via custom endpoint ? — fallback: `CustomizationPlugin` flips `FeaturesSet` on publish (ships C#)|T2
T4|.|post-login tenant guard in seeding pipeline — defense-in-depth vs wrong-tenant writes|V5

## §B BUGS

id|date|cause|fix
