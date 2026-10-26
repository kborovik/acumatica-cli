"""acu - Acumatica configuration as code (GitOps CLI).

Configure Acumatica ERP from YAML in a git data repo. No UI wizards.

\b
MENTAL MODEL
  Data repo = directory with .env (walk-up from cwd). Holds:
    acu-config/{bootstrap,baseline,setup,master}/  seed YAML (apply/diff/survey extract)
    acu-config/views/                              observer views (state)
    acu-scenario/                                  transaction scripts (run)
    .env                                           secrets + where + API pin
    state/ inventory/ findings/ schemas/           command outputs (not seed;
                                                   default --out under data root)
  Two planes (do not mix):
    REST data plane — apply diff run bootstrap survey extract state schema
      needs: base_url, tenant, password (SSH optional)
    SSH control plane — tenant list|create|delete|recycle
      needs: non-empty ACU_SSH (user@host)
  Sole tenant writer = apply (keyed PUT upserts)
  Drift detector     = diff (exit 2 when seed != live)
  Transactions       = run (forward docs, not upserts)

\b
TYPICAL WORKFLOW (SSH box — create your own tenant)
  1. acu config init --host erp.example.com my-erp && cd my-erp
  2. edit .env          # set ACU_PASSWORD, ACU_TENANT
  3. acu config check   # read-only preflight (REST + optional SSH)
  4. acu tenant create --login DEV          # create + bootstrap
  5. acu --tenant DEV apply acu-config      # seed -> tenant
  6. acu --tenant DEV run acu-scenario      # capital -> buy -> build -> sell
  7. acu --tenant DEV diff acu-config       # prove zero drift (exit 2 = drift)
  8. acu --tenant DEV state acu-config/views  # capture trial-balance etc.

\b
HOSTED (tenant already exists, no SSH)
  Set blank ACU_SSH= in .env (scaffold omits the key; without it acu defaults
  SSH to Administrator@<host>). Then:
    acu config check
    acu --tenant DEV bootstrap
    acu --tenant DEV apply acu-config
    acu --tenant DEV diff acu-config
  Offline UI fallback: acu bootstrap --export AcuBootstrap.zip  # SM204505

\b
CONFIG RESOLUTION (per key, highest wins)
  CLI global flag  ->  ACU_* env (.env or process)  ->  default
  Globals only BEFORE the subcommand:
    acu --tenant DEV apply acu-config
  ACU_BASE_URL is where; ACU_API_VERSION pins Default half (else 25.200.001)

\b
COMMAND MAP (pick by intent)
  setup        config init | config show | config check
  tenants      tenant list | create | delete | recycle   (SSH)
  bootstrap    bootstrap [--export PATH]                 (REST or zip)
  seed write   apply [--dry-run] <FILES...>              (sole mutator)
  seed check   diff <FILES...>                           (exit 2 on drift)
  txns         run [--dry-run] <FILES...>
  cold rebuild tenant create then apply then run then diff (no wrap cmd)
  pull seed    survey extract [--only ENTITY]...         (inverse of apply)
  observe      state [--diff|--assert-unchanged] <FILES...>
  offline      survey inventory ARTIFACT | survey reconcile
  reference    schema

\b
EXIT CODES (common)
  0  success
  1  operational / parse / expectation failure
  2  seed drift (diff) or state moved (state --assert-unchanged)

\b
PATHS
  stock trees: acu-config/ (SEED_DIRS + views) and acu-scenario/ only
  apply/diff/run/state require an explicit data path (zero args print that
    command's help, exit non-zero, no HTTP)
  apply/diff  dir with SEED_DIRS children expands those subdirs in order
  run         dir expands *.yaml
  state       view files or dir; --out defaults to state/
  survey extract -> --out default acu-config/ (SEED_DIRS into that root)

Run `acu <command> --help` for flags, examples, and prerequisites.
See package README for seed YAML shape.
"""

import functools
import itertools
import json
import os
from collections import Counter
from collections.abc import Callable
from contextlib import nullcontext
from importlib.metadata import distribution
from pathlib import Path
from typing import Concatenate
from urllib.parse import urlparse

import click
import httpx
from click.shell_completion import get_completion_class

from . import (
    bootstrap,
    extract,
    firstlogin,
    inventory,
    output,
    reconcile,
    run,
    seed,
    state,
)
from .client import AcumaticaClient, SessionPool, format_failure
from .config import (
    Instance,
    default_out,
    find_data_root,
    load_instance,
    read_env_values,
    scaffold,
)
from .tenant import TenantManager, check_identity, find_login

# Wider help + -h for agents/operators scanning docs offline.
_HELP_CTX: dict[str, object] = {
    "help_option_names": ["-h", "--help"],
    "max_content_width": 100,
}


def _version() -> str:
    """Render the CLI version, marking editable installs as dev builds.

    A PEP 610 direct_url.json with dir_info.editable true means the package
    was installed with `pip/uv install -e` from a checkout, so the running
    code can differ from the released wheel; render `<version>+dev (<path>)`
    to keep dev output from masquerading as a release. Wheel installs carry
    no direct_url.json (or editable is absent) and render plain `<version>`.
    """
    dist = distribution("acumatica-cli")
    raw = dist.read_text("direct_url.json")
    if raw is not None:
        direct = json.loads(raw)
        if direct.get("dir_info", {}).get("editable"):
            checkout = direct.get("url", "").removeprefix("file://")
            return f"{dist.version}+dev ({checkout})"
    return dist.version


# flag_value sentinel: a bare --completion means "detect from $SHELL"
_DETECT_SHELL = "auto"


def _completion_script(shell: str) -> str:
    """The click completion script for one supported shell (I.cmd --completion).

    The _DETECT_SHELL sentinel resolves the shell from $SHELL's basename,
    so a bare --completion works in the shell it runs in; an unsupported
    or undetectable shell errors naming the supported set (V9: exit 1).
    Emission is local-only (V23): click renders the script text, nothing
    live is touched - enabling is the user's job (source the output).
    """
    if shell == _DETECT_SHELL:
        shell = Path(os.environ.get("SHELL", "")).name
    completion_cls = get_completion_class(shell)
    if completion_cls is None:
        raise SystemExit(
            f"cannot emit completion for shell {shell!r} (supported: bash, zsh, fish)"
        )
    return completion_cls(cli, {}, "acu", "_ACU_COMPLETE").source()


def _emit_completion(
    ctx: click.Context, _param: click.Parameter, shell: str | None
) -> None:
    """Print the completion script and exit - eager, like --version (V16)."""
    if shell is None or ctx.resilient_parsing:
        return
    output.data(_completion_script(shell))
    ctx.exit()


@click.group(help=__doc__, context_settings=_HELP_CTX)
@click.version_option(version=_version(), prog_name="acu")
@click.option(
    "--completion",
    is_flag=False,
    flag_value=_DETECT_SHELL,
    default=None,
    expose_value=False,
    is_eager=True,
    callback=_emit_completion,
    metavar="[bash|zsh|fish]",
    help="Print shell completion script and exit (bash|zsh|fish). "
    "Bare --completion detects $SHELL. Source the output to enable.",
)
@click.option(
    "--tenant",
    default=None,
    help="Acumatica tenant login name (else ACU_TENANT / .env).",
)
@click.option(
    "--url",
    "base_url",
    default=None,
    help="REST root, e.g. https://erp.example.com/AcumaticaERP (else ACU_BASE_URL).",
)
@click.option(
    "--ssh",
    default=None,
    help="Control-plane SSH user@host (else ACU_SSH). Required for tenant *.",
)
@click.option(
    "--api-version",
    default=None,
    help="Contract API half, e.g. 25.200.001 (else ACU_API_VERSION / 25.200.001).",
)
@click.option(
    "--username",
    "user",
    default=None,
    help="API username (ACU_USER, default: admin).",
)
@click.option(
    "--password",
    default=None,
    help="API password (ACU_PASSWORD). Prefer .env over the flag.",
)
@click.option(
    "--output",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="json: diff/apply/run/state print one JSON document on stdout "
    "(progress on stderr). Other commands stay text.",
)
@click.pass_context
def cli(ctx: click.Context, output_format: str, **flags: str | None) -> None:
    """Stash the global flags; the instance resolves lazily per command.

    Resolution stays out of the group callback so commands that need no
    target (config init) never trigger it; per key a flag beats the
    ACU_* var (.env or process) beats the code default (I.cmd precedence).
    """
    ctx.meta["output"] = output_format
    output.use_json(False)
    ctx.obj = {k: v for k, v in flags.items() if v is not None}


def _json_output(ctx: click.Context) -> bool:
    """True under ``--output json`` (gh #57); moves result lines to stderr."""
    on = ctx.meta.get("output") == "json"
    output.use_json(on)
    return on


def _resolve_instance(ctx: click.Context) -> Instance:
    """Build the target Instance: stashed global flags over ACU_* env (V3 lax)."""
    overrides: dict[str, str] = ctx.obj or {}
    return load_instance(overrides)


def pass_instance[**P, R](f: Callable[Concatenate[Instance, P], R]) -> Callable[P, R]:
    """Like click.pass_obj, resolving config at command time, not group time.

    Every command that talks to an instance takes this decorator; commands
    that must run without a data repo (config init) simply do not.
    """

    @functools.wraps(f)
    def new_func(*args: P.args, **kwargs: P.kwargs) -> R:
        return f(_resolve_instance(click.get_current_context()), *args, **kwargs)

    return new_func


def main() -> None:
    """Entry point: run the CLI, mapping expected failures to one-line errors.

    RuntimeError (SSH/ac.exe, REST, first-login) and httpx transport errors
    print `x message` and exit 1; ACU_DEBUG=1 re-raises for the traceback.
    Transport/network failures use a friendly rewrite (V9), not raw dumps.
    """
    try:
        cli()
    except (RuntimeError, httpx.HTTPError) as exc:
        if os.environ.get("ACU_DEBUG"):
            raise
        output.error(format_failure(exc))
        raise SystemExit(1) from exc


@cli.group("tenant", context_settings=_HELP_CTX)
def tenant_group() -> None:
    """Tenant CRUD over SSH (ac.exe CompanyConfig) — control plane only.

    Requires non-empty ACU_SSH (user@host). Hosted / blank ACU_SSH hard-errors
    before any remote. Not the data-plane path: use bootstrap/apply/diff/run
    for REST work after a tenant exists.

    \b
    Subcommands
      list                 table: CompanyID, Login, CD, Type
      create --login NAME  create (or re-init) + bootstrap → ready for apply
      delete --id N | --login NAME [--yes]
      recycle [--yes]      site-wide app-pool restart (frees API license slots)

    \b
    Examples
      acu tenant list
      acu tenant create --login DEV
      acu tenant create --login LAB --type SalesDemo
      acu tenant delete --login DEV --yes
      acu tenant recycle --yes
    """


@tenant_group.command("list")
@pass_instance
def tenant_list(inst: Instance) -> None:
    """List tenants: CompanyID, sign-in name, internal CD, type, parent.

    SSH control plane. Banner shows hostname only (from base_url), never the
    full URL. Use Login column values as --tenant / --login elsewhere.

    \b
    Example
      acu tenant list
    """
    tenants = TenantManager(inst).list()
    # V9/B27: control-plane identity is the host, never scheme+path
    host = urlparse(inst.base_url).hostname or inst.base_url
    output.table(
        f"Tenants on {host}",
        ("ID", "Login", "CD", "Type", "Parent"),
        (
            (
                str(t.company_id),
                t.login_name,
                t.company_cd,
                t.company_type,
                "" if t.parent_id is None else str(t.parent_id),
            )
            for t in tenants
        ),
    )


@tenant_group.command("create")
@click.option(
    "--id",
    "company_id",
    type=int,
    default=None,
    help="CompanyID (omit = next free max(list)+1; exists-skip adopts live id)",
)
@click.option(
    "--login",
    "login_name",
    required=True,
    help="Tenant name on the sign-in page (required). Becomes REST --tenant.",
)
@click.option(
    "--type",
    "company_type",
    # the V12-verified dataset folders on the box; System
    # is the system-tenant dataset, deliberately not offered
    type=click.Choice(["SalesDemo", "T100", "U100"]),
    default=None,
    help="Data set at creation: SalesDemo|T100|U100 (omit = clean empty tenant).",
)
@click.option(
    "--parent",
    "parent_id",
    type=int,
    default=1,
    show_default=True,
    help="Parent CompanyID for the new company.",
)
@click.option("--hidden", is_flag=True, help="Hide tenant on the sign-in page")
@click.option(
    "--no-init",
    is_flag=True,
    help="Skip recycle + first-login + bootstrap "
    "(tenant invisible to REST until recycle)",
)
@pass_instance
def tenant_create(
    inst: Instance,
    company_id: int | None,
    login_name: str,
    company_type: str | None,
    parent_id: int,
    hidden: bool,
    no_init: bool,
) -> None:
    """Create a tenant and bootstrap it — ready for `acu apply` in one step.

    SSH control plane. Chains: ac.exe CompanyConfig → app-pool recycle →
    REST first-login/password → AcuBootstrap publish (features + endpoint).
    After success, run `acu --tenant <login> apply acu-config`.

    \b
    Identity
      --login required (sign-in / REST tenant name).
      --id optional: omit → next free CompanyID = max(list)+1.
      Login already exists → skip ac.exe create, still run init+bootstrap
      (re-run create to republish bootstrap). --id if given must match live.

    \b
    Flags
      --type SalesDemo|T100|U100  optional dataset (omit = clean tenant)
      --no-init                   create only; no recycle/login/bootstrap

    \b
    Examples
      acu tenant create --login DEV
      acu tenant create --login LAB --type T100
      acu tenant create --login DEV --id 3     # must match if already exists

    \b
    Next: `acu --tenant DEV apply acu-config` then `run acu-scenario`
    / `diff acu-config`.
    """
    # V62: both strings reach remote PowerShell; refuse before any SSH
    check_identity(login_name, "--login")
    check_identity(inst.user, "ACU_USER")
    mgr = TenantManager(inst)
    tenants = mgr.list()
    try:
        existing = find_login(tenants, login_name)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    if existing is not None:
        if company_id is not None and existing.company_id != company_id:
            raise SystemExit(
                f"tenant {login_name} exists with CompanyID "
                f"{existing.company_id}, not {company_id}; "
                f"pass --id {existing.company_id}"
            )
        company_id = existing.company_id
        output.data(f"skip create: tenant {login_name} exists (id {company_id})")
    else:
        if company_id is None:
            # ac.exe never auto-picks; allocate next free from live list
            company_id = max((t.company_id for t in tenants), default=0) + 1
        with output.step(
            f"creating tenant {company_id} ({login_name}) on {inst.base_url}"
        ):
            # None = --type omitted = clean tenant (ac.exe's empty CompanyType)
            raw = mgr.create(
                company_id, login_name, parent_id, not hidden, company_type or ""
            )
        output.data(raw.splitlines()[-1] if raw.strip() else "created")
    # ac.exe only sets CompanyKey from LoginName; CompanyCD stays auto-generated
    # (Company2/Company3/…). Align CD → login so list shows matching columns.
    # Idempotent on re-run / exists-skip.
    with output.step(f"aligning CompanyCD to login ({login_name})"):
        if mgr.set_company_cd(company_id, login_name):
            output.data(f"CompanyCD set to {login_name}")
        else:
            output.data(f"CompanyCD already {login_name}")
    if no_init:
        output.warn("skipping init: tenant is invisible until an app-pool recycle")
        return
    _init_tenant(inst, mgr, login_name)
    _bootstrap_tenant(inst, mgr, login_name)


def _init_tenant(inst: Instance, mgr: TenantManager, login_name: str) -> None:
    """Make a freshly created tenant automation-ready (V5 recycle + login check)."""
    with output.step("recycling app pool (tenant map loads at app start)"):
        mgr.recycle_app_pool()
    with output.step("verifying REST login (screen-flow password change as fallback)"):
        result = firstlogin.initialize_admin_password(inst, tenant=login_name)
    output.success(f"admin {result}; tenant {login_name} is ready")


def _bootstrap_features() -> list[str] | None:
    """Feature list from the data repo, or None → package-built-in six.

    features.yaml is package-build config (V2), not a seed file: both the
    live publish path and --export read it the same way.
    """
    root = find_data_root()
    return bootstrap.load_features(root) if root is not None else None


def _publish_bootstrap_package(inst: Instance, features: list[str] | None) -> str:
    """Import + publish AcuBootstrap into inst.tenant (data plane only)."""
    with (
        output.step(f"publishing {bootstrap.PACKAGE_NAME} to {inst.tenant}"),
        AcumaticaClient(inst) as client,
    ):
        return bootstrap.publish(client, features=features)


def _recycle_after_bootstrap(inst: Instance, mgr: TenantManager | None) -> None:
    """Post-publish app-pool recycle when the control plane is available.

    The publish restarts the site BEFORE its DB transaction commits, so the
    restarted domain caches the feature slot pre-plugin (verified live:
    gated screens stay 403 until one more recycle). On the skip path a
    recycle is the cheap way to make a resumed run sound too. Hosted
    (no ACU_SSH): warn and continue — the customer must restart the site
    another way before feature-gated apply can succeed.
    """
    if mgr is None and not inst.ssh:
        output.warn(
            "ACU_SSH not set: skipped app-pool recycle; "
            "feature-gated screens may stay 403 until the site restarts"
        )
        return
    if mgr is None:
        mgr = TenantManager(inst)
    with output.step("recycling app pool (feature set loads at app start)"):
        mgr.recycle_app_pool()
    with output.step("waiting for the site to come back"):
        firstlogin.initialize_admin_password(inst, tenant=inst.tenant)


def _bootstrap_tenant(inst: Instance, mgr: TenantManager, login_name: str) -> None:
    """Publish the bootstrap package into the fresh tenant (data plane).

    Idempotent on content, not existence (V4): publish() skips only when the
    published package carries the digest of the package built now. The
    session targets the new tenant explicitly (V5), never a config default.
    Tenant create always has SSH (TenantManager already constructed), so
    the post-publish recycle always runs on this path.
    """
    inst = inst.model_copy(update={"tenant": login_name})
    features = _bootstrap_features()
    result = _publish_bootstrap_package(inst, features)
    output.success(f"{bootstrap.PACKAGE_NAME} {result}")
    _recycle_after_bootstrap(inst, mgr)


@cli.command("bootstrap")
@click.option(
    "--export",
    "export_path",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help="Write package zip to PATH (offline; no REST/SSH) for SM204505 UI import",
)
@click.pass_context
def bootstrap_cmd(ctx: click.Context, export_path: Path | None) -> None:
    """Publish AcuBootstrap into the session tenant (or export the package zip).

    Makes a virgin tenant configurable (features on, Bootstrap endpoint up).
    Pure REST — never requires ACU_SSH. Prefer this on hosted boxes; on SSH
    boxes `acu tenant create` already chains bootstrap for you.

    When ACU_SSH is set, an app-pool recycle follows publish so feature-gated
    screens load. When unset, a warning notes the site may need another
    restart before apply. Requires --tenant / ACU_TENANT unless --export.

    \b
    Examples
      acu --tenant DEV bootstrap
      acu bootstrap --export AcuBootstrap.zip   # offline; import on SM204505

    \b
    Next: `acu --tenant DEV apply acu-config`.
    """
    features = _bootstrap_features()
    if export_path is not None:
        if not export_path.parent.exists():
            raise SystemExit(f"{export_path.parent}: directory does not exist")
        export_path.write_bytes(bootstrap.package_zip(features))
        output.success(f"wrote {export_path}")
        return
    inst = _resolve_instance(ctx)
    if not inst.tenant:
        raise SystemExit(
            "tenant not set (pass --tenant, "
            "or put ACU_TENANT in .env or the environment)"
        )
    result = _publish_bootstrap_package(inst, features)
    output.success(f"{bootstrap.PACKAGE_NAME} {result}")
    _recycle_after_bootstrap(inst, mgr=None)


@tenant_group.command("delete")
@click.option(
    "--id",
    "company_id",
    type=int,
    default=None,
    help="CompanyID from `acu tenant list`",
)
@click.option(
    "--login",
    "login_name",
    default=None,
    help="Sign-in / REST tenant name (alternative to --id)",
)
@click.confirmation_option(prompt="Delete this tenant and all its data?")
@pass_instance
def tenant_delete(
    inst: Instance, company_id: int | None, login_name: str | None
) -> None:
    """Delete the tenant and all its data, then recycle the app pool.

    SSH control plane. Pass exactly one of --id or --login. Confirm prompt;
    --yes skips. Irreversible — all company data is removed.

    \b
    Examples
      acu tenant delete --login DEV --yes
      acu tenant delete --id 3 --yes
    """
    if (company_id is None) == (login_name is None):
        raise SystemExit("pass exactly one of --id or --login")
    if login_name is not None:
        check_identity(login_name, "--login")  # V62: before any SSH
    mgr = TenantManager(inst)
    # V9 long single-op: ac.exe delete matches create's step path (TTY spinner /
    # piped stderr process line); recycle stays its own step after.
    label = f"id {company_id}" if company_id is not None else str(login_name)
    try:
        with output.step(f"deleting tenant {label} on {inst.base_url}"):
            raw = mgr.delete(company_id, login_name=login_name)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
    output.data(raw.splitlines()[-1] if raw.strip() else "done")
    with output.step("recycling app pool (drops the tenant from the running app)"):
        mgr.recycle_app_pool()


@tenant_group.command("recycle")
@click.confirmation_option(
    prompt=(
        "Recycle the site app pool? This kills all sessions and frees API-user slots."
    )
)
@pass_instance
def tenant_recycle(inst: Instance) -> None:
    """Recycle the IIS app pool for the instance (site-wide; no tenant --id).

    SSH control plane: Restart-WebAppPool. Reloads the tenant map and drops
    every in-flight session so concurrent API-user license slots free up.
    Use after stuck sessions / "license" API errors. Hosted / empty ACU_SSH
    hard-errors. Confirm prompt; --yes skips.

    \b
    Example
      acu tenant recycle --yes
    """
    mgr = TenantManager(inst)
    with output.step("recycling app pool (tenant map + free API-user sessions)"):
        mgr.recycle_app_pool()
    output.success("app pool recycled")


@cli.group("config", context_settings=_HELP_CTX)
def config_group() -> None:
    """Data-repo setup and preflight (local + read-only live).

    \b
    Subcommands
      init [DIR]     scaffold full data repo (local write, no network)
      show           print resolved config as a complete .env (password redacted)
      check          read-only live preflight (REST + optional SSH)

    Start here on a new machine: init → edit .env → config check →
    tenant create / bootstrap.
    """


@config_group.command("init")
@click.option(
    "--host",
    default=None,
    help="Hostname for ACU_BASE_URL (default: erp.example.com). "
    "ACU_SSH omitted from .env and defaults from that host.",
)
@click.argument(
    "directory", required=False, type=click.Path(file_okay=False, path_type=Path)
)
def config_init(host: str | None, directory: Path | None) -> None:
    """Scaffold a data repo: .env, acu-config/ seed, acu-scenario/.

    Local only (no network, no secrets written). Creates
    acu-config/{bootstrap,baseline,setup,master}/, acu-scenario/,
    acu-config/views/, .env with ACU_BASE_URL + ACU_API_VERSION, README.
    Never scaffolds a customization tree. Existing files are never
    overwritten (reported as skipped). DIRECTORY defaults to cwd and is
    created if absent. No git init, no gpg. Never scaffolds project.xml
    (bootstrap contract is package-owned). Never scaffolds matrix.yaml.

    \b
    Examples
      acu config init --host erp.example.com my-erp
      cd my-erp && $EDITOR .env    # ACU_PASSWORD, ACU_TENANT
      acu config check

    Prints a numbered next: list after scaffold.
    """
    target = directory or Path.cwd()
    for action, path in scaffold(target, host=host):
        suffix = " (exists)" if action == "skip" else ""
        output.data(f"{action} {path}{suffix}")
    # next-step cmds: operator rebuild order after scaffold (V28)
    output.data("")
    output.data("next:")
    output.data("  1. edit .env (set ACU_PASSWORD, ACU_TENANT)")
    output.data("  2. acu config check")
    output.data("  3. acu bootstrap          # or: acu tenant create ... (SSH)")
    output.data("  4. acu apply acu-config")
    output.data("  5. acu run acu-scenario")
    output.data("  6. acu diff acu-config")
    output.data("  7. acu state acu-config/views")


@config_group.command("show")
@pass_instance
def config_show(inst: Instance) -> None:
    """Print the fully resolved configuration as a complete .env document.

    Same resolution path live commands use (flags → env → default).
    Password is never emitted (no ACU_PASSWORD key). ACU_API_VERSION is
    emitted with the resolved pin. Safe to redirect and edit; supply
    password out of band.

    \b
    Examples
      acu config show
      acu --tenant DEV --api-version 24.200.001 config show
    """
    output.data("# resolved by `acu config show` - a complete .env")
    output.data("# ACU_PASSWORD comes from .env or the environment, never from here")
    for field, value in inst.model_dump(exclude={"password"}).items():
        output.data(f"ACU_{field.upper()}={value}")


@config_group.command("check")
@click.pass_context
def config_check(ctx: click.Context) -> None:
    """Read-only preflight of the resolved target (ok/fail/warn/skip lines).

    Proves connectivity before apply/run. Writes nothing (no PUT, no tenant
    CRUD).

    \b
    Probe order
      discovery  .env walk-up + base_url resolve
      secrets    ACU_PASSWORD present
      rest       login + landed tenant + Default/<api> listed
      ssh        ping if ACU_SSH set; skip if unset (hosted OK)

    \b
    Exit  0 = no fail lines (warns OK) · 1 = any fail
    Discovery/secrets failure stops early; REST and SSH are independent.

    \b
    Examples
      acu config check
      acu --url https://erp/AcumaticaERP --password SECRET config check
    """
    overrides: dict[str, str] = ctx.obj or {}
    # discovery (V3): lax walk-up + parse; base_url must resolve from flag,
    # process env, or found .env (V27)
    root = find_data_root()
    try:
        env_values = read_env_values(root / ".env") if root is not None else {}
        if not (
            overrides.get("base_url")
            or os.environ.get("ACU_BASE_URL")
            or env_values.get("base_url")
        ):
            source = f"{root / '.env'}:" if root is not None else "no .env found and"
            raise SystemExit(
                f"{source} missing required base_url (pass --url or ACU_BASE_URL)"
            )
    except SystemExit as exc:
        output.data(f"fail discovery: {exc}")
        raise SystemExit(1) from exc
    found = root / ".env" if root is not None else "no .env - flags only"
    output.data(f"ok discovery ({found})")

    # secrets: same sources as load_instance - the flag, then the process
    # environment, then the found .env; the value is never printed (V2)
    if overrides.get("password"):
        output.data("ok secrets (--password)")
    elif os.environ.get("ACU_PASSWORD") or env_values.get("password"):
        output.data("ok secrets (ACU_PASSWORD set)")
    else:
        output.data(
            "fail secrets: password not set (pass --password, "
            "or put ACU_PASSWORD in .env or the environment)"
        )
        raise SystemExit(1)

    # both live probes run through the exact objects live commands use, so
    # a pass here proves the real code path, not a parallel one
    inst = _resolve_instance(ctx)
    failed = False
    if not _probe_rest(inst):
        failed = True
    if not _probe_ssh(inst):
        failed = True
    if failed:
        raise SystemExit(1)


def _probe_rest(inst: Instance) -> bool:
    """REST login + endpoints; True = all passed.

    Entering the client is the probe: login + landed-tenant verify (V5);
    context manager guarantees logout (V6). One GET /entity feeds endpoints
    (T74/T90).
    """
    try:
        with AcumaticaClient(inst) as client:
            output.data(f"ok rest ({inst.base_url}, tenant {inst.tenant})")
            return _probe_entity_root(client, inst)
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail rest: {format_failure(exc, target=inst.base_url)}")
        return False


def _probe_entity_root(client: AcumaticaClient, inst: Instance) -> bool:
    """Endpoints probe; True = passed."""
    try:
        endpoints, _live_build = client.entity_root()
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail endpoints: {format_failure(exc, target=inst.base_url)}")
        return False
    return _probe_endpoints(endpoints, inst)


def _probe_ssh(inst: Instance) -> bool:
    """SSH ping or skip when unset; True = pass/skip, False = fail."""
    if not inst.ssh:
        # V3/I.cmd: ACU_SSH optional — hosted data-plane path skips, never fails
        output.data("skip ssh (ACU_SSH not set)")
        return True
    try:
        TenantManager(inst).ping()
        output.data(f"ok ssh ({inst.ssh})")
        return True
    except RuntimeError as exc:
        output.data(f"fail ssh: {exc}")
        return False


def _probe_endpoints(endpoints: list[tuple[str, str]], inst: Instance) -> bool:
    """Emit endpoints probe; return True on pass, False on fail (V12/V27).

    Exact match: a Default entry whose version half equals
    `Instance.api_version`. Caller already fail-closed on unparseable GET.
    """
    want = f"Default/{inst.api_version}"
    defaults = [v for name, v in endpoints if name == "Default"]
    if inst.api_version in defaults:
        output.data(f"ok endpoints ({want} present)")
        return True
    present = ", ".join(f"Default/{v}" for v in defaults) or "(none)"
    output.data(
        f"fail endpoints: configured {want} not listed; "
        f"present Default versions: {present}"
    )
    return False


SEED_DIRS = ("bootstrap", "baseline", "setup", "master")


def _seed_child_dirs(parent: Path) -> list[Path]:
    """SEED_DIRS children of `parent` that exist, fixed order (V22/V30)."""
    return [parent / name for name in SEED_DIRS if (parent / name).is_dir()]


def _leaf_yaml(directory: Path) -> list[Path]:
    """Sorted `*.yaml` in a leaf seed dir; skip `features.yaml` (I.data)."""
    return sorted(
        p for p in directory.glob("*.yaml") if p.is_file() and p.name != "features.yaml"
    )


def _require_data_paths(
    ctx: click.Context, files: tuple[Path, ...]
) -> tuple[Path, ...]:
    """V59: zero data-path args print this command's help; no HTTP, no PUT."""
    if files:
        return files
    click.echo(ctx.get_help(), color=ctx.color)
    ctx.exit(1)


def expand_files(files: tuple[Path, ...]) -> list[Path]:
    """Expand directory arguments into seed `*.yaml` files (V22/V30).

    A dir with any SEED_DIRS child (umbrella e.g. `acu-config/`) expands those
    nested subdirs in fixed SEED_DIRS order, then leaf `*.yaml` per subdir.
    A leaf dir expands its own `*.yaml` only. Typed path is used as given
    (never cwd-fill). `features.yaml` is skipped: it configures the
    bootstrap package build, not an entity/records seed.
    """
    paths: list[Path] = []
    for path in files:
        if path.is_dir():
            children = _seed_child_dirs(path)
            if children:
                found: list[Path] = []
                for child in children:
                    found += _leaf_yaml(child)
            else:
                found = _leaf_yaml(path)
            if not found:
                raise SystemExit(f"{path}: no seed *.yaml files in directory")
            paths += found
        else:
            paths.append(path)
    return paths


def collapse_same_basename(paths: list[Path]) -> list[Path]:
    """Later same-basename wins (V44 overlay compose for `run`).

    `acu run acu-scenario overlays/default-<half>/acu-scenario` keeps trunk
    order and replaces overlay-matching names. apply/diff still concatenate.
    """
    by_name: dict[str, Path] = {}
    order: list[str] = []
    for path in paths:
        if path.name not in by_name:
            order.append(path.name)
        by_name[path.name] = path
    return [by_name[name] for name in order]


@cli.command("apply")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List what would be PUT; offline (no login, no .env needed)",
)
@click.option(
    "--plan",
    is_flag=True,
    help="Read live; label each record create / update / unchanged; no writes",
)
@click.option(
    "--changed-only",
    is_flag=True,
    help="Read live first; skip the PUT of records with no drift",
)
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Parallel sessions; each takes one API-user license slot",
)
@click.pass_context
def apply_cmd(
    ctx: click.Context,
    files: tuple[Path, ...],
    dry_run: bool,
    plan: bool,
    changed_only: bool,
    concurrency: int,
) -> None:
    """Push seed YAML into the tenant (idempotent PUT upserts).

    Sole tenant writer. REST data plane. FILES = seed YAML files or dirs
    (required). A dir with seed children (e.g. acu-config/) expands nested
    trees in fixed order: bootstrap → baseline → setup → master. Zero
    args print this help, exit non-zero, no PUT (V59).

    Every file is parsed and validated before login; any invalid file
    exits 1 naming all of them, with nothing sent.
    Per-record failure isolation: one failed PUT reports and continues;
    exit 1 with multi-error summary if any failed (never silent partial).
    A refused connect or failed re-login stops the run: exit 1 naming the
    stopped-at record and every file not applied.
    Exit 2 is reserved for `diff` drift.

    --plan reads live state with the `diff` compare and labels each
    record create / update (fields) / unchanged, and each action invoke /
    skip; it writes nothing. --changed-only does the same read, then PUTs
    only create / update records. Both end with a count summary. Default
    PUTs every record (idempotent re-PUT).

    \b
    Prerequisites
      Tenant exists and is bootstrapped (tenant create or acu bootstrap).
      ACU_PASSWORD + base_url + tenant resolved (config check first).

    \b
    Examples
      acu --tenant DEV apply acu-config
      acu --tenant DEV apply --dry-run acu-config/master/
      acu --tenant DEV apply --plan acu-config
      acu --tenant DEV apply --changed-only acu-config
      acu --tenant DEV apply acu-config/baseline/20-accounts.yaml

    Related: `diff` (drift) · `survey extract` (inverse pull) · `run` (txns).
    """
    files = _require_data_paths(ctx, files)
    if sum((dry_run, plan, changed_only)) > 1:
        raise click.UsageError("--dry-run, --plan, --changed-only are exclusive")
    mode = (
        seed.ApplyMode.PLAN
        if plan
        else seed.ApplyMode.CHANGED
        if changed_only
        else seed.ApplyMode.ALL
    )
    as_json = _json_output(ctx)
    counts: Counter[str] = Counter()
    total_ok = 0
    all_errors: list[str] = []
    report: list[dict[str, object]] = []
    not_applied: list[Path] = []
    paths = expand_files(files)
    baselines = seed.load_all(paths)  # V45: every file valid before login
    stopped = False
    # gh #63: dry-run is offline - no instance resolution, no session
    inst = None if dry_run else _resolve_instance(ctx)
    # V66: all N sessions log in before the first write
    session = (
        nullcontext()
        if inst is None
        else SessionPool(inst, concurrency, factory=AcumaticaClient)
    )
    with session as pool:
        client = None if pool is None else pool.clients[0]
        for i, (path, baseline) in enumerate(zip(paths, baselines, strict=True)):
            target = "dry run" if inst is None else f"{inst.tenant} on {inst.base_url}"
            output.data(f"{path} -> {target} ({baseline.entity})")
            try:
                n, errors = seed.apply(client, baseline, dry_run, mode, counts)
            except seed.ApplyStopped as stop:
                # report now: a dead host can also fail the logout on exit
                stopped = True
                not_applied = paths[i:]
                all_errors.extend(f"{path}: {e}" for e in stop.errors)
                _apply_stopped(path, stop, paths[i + 1 :], total_ok + stop.ok)
                for err in all_errors:
                    output.error(err)
                break
            total_ok += n
            all_errors.extend(f"{path}: {e}" for e in errors)
            report.append(
                {
                    "file": str(path),
                    "entity": baseline.entity,
                    "ok": n,
                    "errors": errors,
                }
            )
            output.data(f"  {n} record(s){' (dry run)' if dry_run else ''}")
    if as_json:
        output.document(
            {
                "command": "apply",
                "tenant": None if inst is None else inst.tenant,
                "mode": "dry_run" if dry_run else mode.value,
                "ok": total_ok,
                "failed": len(all_errors),
                "counts": dict(counts),
                "errors": all_errors,
                "stopped": stopped,
                "not_applied": [str(p) for p in not_applied],
                "files": report,
            }
        )
    if stopped:
        raise SystemExit(1)
    if mode is not seed.ApplyMode.ALL:
        output.data(_plan_summary(mode, counts, len(all_errors)))
    if all_errors:
        output.error(
            f"apply: {len(all_errors)} error(s), {total_ok} record(s) applied"
            f"{' (dry run)' if dry_run else ''}"
        )
        for err in all_errors:
            output.error(err)
        raise SystemExit(1)


def _plan_summary(mode: seed.ApplyMode, counts: Counter[str], failed: int) -> str:
    """Tally line (gh #55): ``plan: 2 create, ...`` / ``apply: 2 created, ...``."""
    done = mode is seed.ApplyMode.CHANGED
    parts = [
        f"{counts[kind]} {kind}{'d' if done else ''}"
        for kind in ("create", "update", "invoke")
    ]
    head = "apply" if done else "plan"
    return (
        f"{head}: {', '.join(parts)}, {counts['unchanged']} unchanged, {failed} failed"
    )


def _apply_stopped(
    path: Path, stop: seed.ApplyStopped, rest: list[Path], applied: int
) -> None:
    """V45 hard-stop report: stopped-at file/record + not-applied remainder."""
    output.error(f"apply: stopped at {path}: {stop}")
    output.error(f"apply: {applied} record(s) applied before the stop")
    output.error(f"not applied: {path} (from {stop.record})")
    for later in rest:
        output.error(f"not applied: {later}")


@cli.command("schema")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (default: <data root>/schemas; no .env: ./schemas)",
)
@pass_instance
def schema_cmd(inst: Instance, out_dir: Path | None) -> None:
    """Dump the endpoint OpenAPI schema (swagger.json) into schemas/.

    Authoritative field-level reference for the exact Default/<api> build
    (~3 MB). Regenerate rather than version-control. REST; needs password.

    \b
    Example
      acu schema
      acu --api-version 25.200.001 schema --out ./schemas
    """
    if out_dir is None:
        out_dir = default_out("schemas")
    out_file = out_dir / f"swagger-Default-{inst.api_version}.json"
    with (
        output.step(
            f"dumping OpenAPI schema from {inst.base_url} (Default/{inst.api_version})"
        ),
        AcumaticaClient(inst) as client,
    ):
        raw = client.swagger()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(raw)
    output.data(f"{out_file} ({len(raw)} bytes)")


@cli.command("diff")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.pass_context
def diff_cmd(ctx: click.Context, files: tuple[Path, ...]) -> None:
    """Compare seed YAML against the live tenant; exit 2 on drift.

    Read-only REST. Same FILES rules as apply (umbrella expansion on the
    given dir). Data path required (V59): zero args print this help, exit
    non-zero, no HTTP. Use after apply to prove the tenant matches the
    repo, or in CI as the drift gate. Prints DRIFT lines then exits 2
    when seed ≠ live.

    \b
    Exit  0 = no drift · 1 = op failure · 2 = drift detected

    \b
    Examples
      acu --tenant DEV diff acu-config
      acu --tenant DEV diff acu-config/master/90-roles.yaml

    Related: `apply` (fix) · `survey extract` (pull) · `state` (balances).
    """
    files = _require_data_paths(ctx, files)
    as_json = _json_output(ctx)
    inst = _resolve_instance(ctx)
    paths = expand_files(files)
    drifts: list[str] = []
    report: list[dict[str, object]] = []
    baselines = seed.load_all(paths)
    with AcumaticaClient(inst) as client:
        for path, baseline in zip(paths, baselines, strict=True):
            # same file banner as apply so a long multi-file diff shows
            # progress instead of silence until the final status line
            output.data(
                f"{path} -> {inst.tenant} on {inst.base_url} ({baseline.entity})"
            )
            file_drifts = seed.diff(client, baseline)
            drifts += file_drifts
            n = 1 if isinstance(baseline, seed.ActionFile) else len(baseline.records)
            if as_json:
                report.append(_diff_file_doc(path, baseline.entity, n, file_drifts))
            if file_drifts:
                output.data(f"  {n} record(s), {len(file_drifts)} drift(s)")
            else:
                output.data(f"  {n} record(s) ok")
    if as_json:
        output.document(
            {
                "command": "diff",
                "tenant": inst.tenant,
                "base_url": inst.base_url,
                "drift": len(drifts),
                "files": report,
            }
        )
    _exit_on_drift(inst, drifts, len(paths))


def _diff_file_doc(
    path: Path, entity: str, records: int, drifts: list[seed.Drift]
) -> dict[str, object]:
    """One file of the ``diff --output json`` document (gh #57 schema)."""
    return {
        "file": str(path),
        "entity": entity,
        "records": records,
        "drifts": [
            {
                "file": str(path),
                "entity": entity,
                "keys": d.keys,
                "field": d.field,
                "kind": d.kind,
                "source": d.source,
                "live": d.live,
                "text": str(d),
            }
            for d in drifts
        ],
    }


def _not_run_lines(scenarios: list[run.Scenario]) -> list[str]:
    """One line per not-run file; a multi-document tail folds to a range.

    A generated load stopped at document 12 of 1000 lists
    ``<file>#13-#1000 (988 documents)``, not 988 lines (gh #73).
    """
    lines: list[str] = []
    for _, group in itertools.groupby(scenarios, key=lambda s: s.path):
        docs = list(group)
        if len(docs) == 1:
            lines.append(docs[0].label)
        else:
            lines.append(f"{docs[0].label}-#{docs[-1].doc} ({len(docs)} documents)")
    return lines


@cli.command("run")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option("--dry-run", is_flag=True, help="Parse and list steps without any HTTP")
@click.option(
    "--concurrency",
    type=click.IntRange(min=1),
    default=1,
    show_default=True,
    help="Parallel sessions; each takes one API-user license slot",
)
@click.pass_context
def run_cmd(
    ctx: click.Context, files: tuple[Path, ...], dry_run: bool, concurrency: int
) -> None:
    """Execute transaction scenario YAML against the live tenant.

    REST data plane. Not seed upserts — documents are posted forward (server
    assigns numbers). FILES required (V59): zero args print this help, exit
    non-zero, no HTTP. A dir expands `*.yaml`. Delta expectations snapshot
    before the first step and compare after the last, so re-runs are safe
    on a warm tenant. Built-in `${current_period}` → host-local MMyyyy on
    steps/expect/once (state views stay period-pinned separately).

    A file may hold many scenarios split by `---` (a generated load, one
    YAML document per ERP document, reported as `<file>#<n>`). Guard each
    with `once: true` + `present: {get: {entity, filter}}` on its external
    reference: a re-run skips documents already on the tenant.

    A step error prints `x <file> [<step id>]: <error>` and stops: later
    scenarios are listed as not run (they build on earlier ones). An
    expectation miss marks that scenario failed and the run continues.
    Summary: passed / failed / skipped (once) / not run.

    \b
    Exit  0 = all expectations hold · 1 = step error or expectation miss

    \b
    Prerequisites
      Seed applied (apply acu-config) so masters/parties/items exist.

    \b
    Examples
      acu --tenant DEV run acu-scenario
      acu --tenant DEV run --dry-run acu-scenario/10-seed-capital.yaml
      acu --tenant DEV run acu-scenario overlays/default-24.200.001/acu-scenario

    Related: `apply` · `diff` · `state`.
    """
    files = _require_data_paths(ctx, files)
    paths = collapse_same_basename(expand_files(files))
    scenarios = [scenario for path in paths for scenario in run.load_scenarios(path)]
    if dry_run:
        # gh #63: offline - no instance resolution, no HTTP
        for scenario in scenarios:
            run.run(None, scenario, dry_run=True)
        output.success(f"{len(scenarios)} scenario(s) parsed (dry run)")
        return
    as_json = _json_output(ctx)
    inst = _resolve_instance(ctx)
    counts = dict.fromkeys(run.Outcome, 0)
    not_run: list[run.Scenario] = []
    report: list[dict[str, object]] = []
    # V66: all N sessions log in before the first step
    with SessionPool(inst, concurrency, factory=AcumaticaClient) as pool:
        client = pool.clients[0]
        for i, scenario in enumerate(scenarios):
            misses: list[str] = []
            entry: dict[str, object] = {"file": str(scenario.path), "misses": misses}
            if scenario.doc is not None:
                entry["doc"] = scenario.doc
            report.append(entry)
            try:
                entry["outcome"] = outcome = run.run(client, scenario, misses=misses)
                counts[outcome] += 1
            except run.StepError as err:
                # gh #58: lifecycle scenarios chain, so stop and list the rest
                output.error(str(err))
                entry |= {"outcome": run.Outcome.FAILED, "error": str(err)}
                counts[run.Outcome.FAILED] += 1
                not_run = scenarios[i + 1 :]
                break
    for line in _not_run_lines(not_run):
        output.error(f"not run: {line}")
    if as_json:
        output.document(
            {
                "command": "run",
                "tenant": inst.tenant,
                "counts": {str(k): v for k, v in counts.items()}
                | {"not_run": len(not_run)},
                "scenarios": report,
                "not_run": [s.label for s in not_run],
            }
        )
    summary = (
        f"{counts[run.Outcome.PASSED]} passed, {counts[run.Outcome.FAILED]} failed, "
        f"{counts[run.Outcome.SKIPPED]} skipped, {len(not_run)} not run "
        f"on {inst.tenant}"
    )
    if counts[run.Outcome.FAILED]:
        output.error(f"run: {summary}")
        raise SystemExit(1)
    output.success(f"run: {summary}")


def _complete_only(
    _ctx: click.Context, _param: click.Parameter, incomplete: str
) -> list[str]:
    """--only value completion: entity names off the packaged seed catalog.

    Fires per keystroke, so it stays local-only (V23): the catalog is
    package data - never REST, never SSH, never a live instance.
    """
    return [
        spec.entity
        for spec in extract.load_manifest().entities
        if spec.entity.startswith(incomplete)
    ]


@cli.group("survey", context_settings=_HELP_CTX)
def survey_group() -> None:
    """Existing-tenant dual-reader (REST extract + offline inventory/reconcile).

    Noun owns extract|inventory|reconcile. Never L1 those verbs. No aliases.

    \b
    Subcommands
      extract [--only ENTITY]...   live GET -> acu-config/ SEED_DIRS (REST)
      inventory ARTIFACT           snapshot ZIP/folder -> inventory/ (offline)
      reconcile                    inventory/ + optional --config DIR
                                   -> findings/ (offline)

    \b
    Examples
      acu --tenant DEV survey extract
      acu survey inventory ./export-xml/
      acu survey reconcile
    """


@survey_group.command("extract")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Seed-tree root (default: <data root>/acu-config/); writes SEED_DIRS into it",
)
@click.option(
    "--only",
    multiple=True,
    shell_complete=_complete_only,
    help="Limit to catalog entity name or file stem; repeatable",
)
@click.option("--force", is_flag=True, help="Overwrite existing seed files")
@click.option(
    "--dry-run", is_flag=True, help="Show what would be written without writing"
)
@pass_instance
def extract_cmd(
    inst: Instance,
    out_dir: Path | None,
    only: tuple[str, ...],
    force: bool,
    dry_run: bool,
) -> None:
    """Extract live tenant into seed YAML under acu-config/ (inverse of apply).

    REST reader. Packaged seed_catalog.yaml is the entity registry. --out
    default acu-config/ is the seed-tree root: writes SEED_DIRS directly
    into it (no nested acu-config/acu-config). Features →
    bootstrap/features.yaml under --out. Existing files skip unless
    --force; empty live sets produce no file. Row failures continue;
    exit 1 if any failed. Drift detection stays with `diff`.

    Not inventory (offline snapshot tables) and not state (derived balances).

    \b
    Exit  0 = all rows wrote/skipped clean · 1 = any row failed

    \b
    Examples
      acu --tenant DEV survey extract
      acu --tenant DEV survey extract --only Account --only Vendor
      acu --tenant DEV survey extract --force --dry-run

    Related: `apply` · `diff` · `survey inventory` · `state`.
    """
    with AcumaticaClient(inst) as client:
        failed = extract.run(
            client,
            out_dir or default_out("acu-config"),
            only=frozenset(only),
            force=force,
            dry_run=dry_run,
        )
    if failed:
        raise SystemExit(1)


def _exit_on_drift(inst: Instance, drifts: list[str], files: int) -> None:
    """Report drift lines and exit 2 (the load-bearing diff contract, V9)."""
    if drifts:
        output.error(f"DRIFT on {inst.tenant} ({inst.base_url}):")
        for line in drifts:
            output.data(f"  {line}")
        raise SystemExit(2)
    output.success(f"no drift on {inst.tenant} ({inst.base_url}, {files} file(s))")


@survey_group.command("inventory")
@click.argument(
    "artifact",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (default: <data root>/inventory/)",
)
@click.option("--force", is_flag=True, help="Overwrite existing files")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without writing",
)
def inventory_cmd(
    artifact: Path,
    out_dir: Path | None,
    force: bool,
    dry_run: bool,
) -> None:
    """Parse a snapshot artifact into inventory/ (offline, no REST/SSH).

    ARTIFACT = SM203520 Settings XML ZIP (manifest.xml + table XML) or an
    ac.exe `export xml` folder. Binary .adb is rejected. Writes
    summary.yaml + tables/<Table>.yaml under --out (default inventory/).
    Existing files skip unless --force.

    Not extract (REST → config/ seed). Not state (balances). Never writes
    config/ or state/. Feed inventory/ into `acu survey reconcile`.

    \b
    Exit  0 = clean · 1 = parse/format/version fail · never 2

    \b
    Examples
      acu survey inventory ./export-xml/
      acu survey inventory Settings.zip --out inventory/ --force
    """
    # V9 long single-op: artifact parse (ZIP/folder IR) via step; banner +
    # per-table write/skip emit stay multi-unit stdout after.
    with output.step(f"parsing snapshot artifact {artifact}"):
        art = inventory.parse_artifact(artifact)
    dest = out_dir if out_dir is not None else default_out(inventory.DEFAULT_OUT)
    output.data(
        f"{artifact} -> {dest} ({len(art.tables)} table(s)"
        f"{f', erp={art.erp}' if art.erp else ''})"
    )
    inventory.emit(art, dest, force=force, dry_run=dry_run)


@survey_group.command("reconcile")
@click.option(
    "--inventory",
    "inventory_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Inventory tree from `acu survey inventory` (default: <data root>/inventory/)",
)
@click.option(
    "--config",
    "config_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional seed tree (no default; omit = inventory-only)",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Findings output directory (default: <data root>/findings/)",
)
@click.option("--force", is_flag=True, help="Overwrite existing findings files")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show what would be written without writing",
)
def reconcile_cmd(
    inventory_dir: Path | None,
    config_dir: Path | None,
    out_dir: Path | None,
    force: bool,
    dry_run: bool,
) -> None:
    """Compare inventory/ to optional --config seed; write findings/ (offline).

    Cross-check snapshot tables vs seed. No REST, no SSH, no password.
    Omit --config for inventory-only (never assume acu-config/ or config/).
    Never writes acu-config/ (not extract promote) and never captures state/.
    Gaps, field deltas, unmapped tables, Usr* columns → findings/ only.
    Optional snapshot_map.yaml maps DAC tables → catalog entities (aliases,
    FK resolvers, enums); package defaults cover common LAB maps. Conflicts
    are findings, not exit-2 drift.

    \b
    Exit  0 = ran clean · 1 = IO/parse fail · never 2

    \b
    Examples
      acu survey inventory export.zip && acu survey reconcile
      acu survey reconcile --inventory inv/ --config acu-config/ --out findings/
    """
    inv = (
        inventory_dir
        if inventory_dir is not None
        else default_out(reconcile.DEFAULT_INVENTORY)
    )
    cfg = config_dir
    dest = out_dir if out_dir is not None else default_out(reconcile.DEFAULT_OUT)
    cfg_label = str(cfg) if cfg is not None else "(none)"
    output.data(f"{inv} + {cfg_label} -> {dest}")
    reconcile.run(
        inventory_dir=inv,
        config_dir=cfg,
        out_dir=dest,
        force=force,
        dry_run=dry_run,
    )


@cli.command("state")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Observation output directory (default: <data root>/state/)",
)
@click.option(
    "--diff",
    "do_diff",
    is_flag=True,
    help="Compare live vs disk; write nothing (exit 0 either way)",
)
@click.option(
    "--assert-unchanged",
    is_flag=True,
    help="Like --diff, but exit 2 when state moved (idempotence gate)",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Resolve views and validate sources without HTTP",
)
@click.pass_context
def state_cmd(
    ctx: click.Context,
    files: tuple[Path, ...],
    out_dir: Path | None,
    do_diff: bool,
    assert_unchanged: bool,
    dry_run: bool,
) -> None:
    """Capture live derived state (balances/totals) into state/ files.

    REST observer. Not seed (config/) and not inventory (snapshot tables).
    FILES = view YAML (required). Zero args print this help, exit non-zero,
    no HTTP (V59). Writes observations to state/ (--out). Bare capture
    always writes (change OK). --diff compares live vs disk without
    writing. --assert-unchanged is the warm-run idempotence gate (exit 2
    when moved).

    \b
    Exit  0 = ok · 1 = op fail · 2 = only with --assert-unchanged when moved

    \b
    Examples
      acu --tenant DEV state acu-config/views/
      acu --tenant DEV state --diff acu-config/views/
      acu --tenant DEV state --assert-unchanged acu-config/views/
      acu --tenant DEV state acu-config/views/10-trial-balance.yaml

    Related: `run` (moves balances) · `diff` (seed drift) · `survey extract`.
    """
    files = _require_data_paths(ctx, files)
    paths = state.expand_view_files(files)
    views = [state.load_view(path) for path in paths]
    dest = out_dir if out_dir is not None else default_out("state")
    if dry_run:
        # gh #63: offline - no instance resolution, no HTTP
        code = state.run_views(None, views, out_dir=dest, mode="dry")
        if code:
            raise SystemExit(code)
        return
    as_json = _json_output(ctx)
    inst = _resolve_instance(ctx)
    mode = "assert" if assert_unchanged else "diff" if do_diff else "write"
    report: list[dict[str, object]] = []
    with AcumaticaClient(inst) as client:
        code = state.run_views(client, views, out_dir=dest, mode=mode, report=report)
    if as_json:
        moved = sum(1 for v in report if v.get("status") == "changed")
        output.document(
            {
                "command": "state",
                "tenant": inst.tenant,
                "mode": mode,
                "exit": code,
                "changed": moved,
                "views": report,
            }
        )
    if code:
        raise SystemExit(code)
    if assert_unchanged:
        output.success(f"{len(views)} view(s) unchanged on {inst.tenant}")
    elif do_diff:
        output.success(f"{len(views)} view(s) compared on {inst.tenant}")
    else:
        output.success(f"{len(views)} view(s) written under {dest} on {inst.tenant}")
