"""acu - Acumatica configuration as code."""

import functools
import json
import os
from collections.abc import Callable
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
from .client import AcumaticaClient
from .config import (
    Instance,
    data_root,
    find_data_root,
    load_instance,
    read_env_values,
    scaffold,
)
from .target import assert_target_compatible, load_target
from .tenant import TenantManager


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


@click.group(help=__doc__)
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
    help="Print the shell completion script and exit; a bare --completion "
    "detects the shell from $SHELL. Enable by sourcing the output.",
)
@click.option(
    "--tenant",
    default=None,
    help="Acumatica tenant name",
)
@click.option(
    "--url",
    "base_url",
    default=None,
    help="REST root URL - https://erp.example.com/AcumaticaERP",
)
@click.option(
    "--ssh",
    default=None,
    help="Control-plane SSH as user@host",
)
@click.option(
    "--api-version",
    default=None,
    help="Contract API version",
)
@click.option(
    "--username",
    "user",
    default=None,
    help="API username (ACU_USER, default: admin)",
)
@click.option(
    "--password",
    default=None,
    help="API password (ACU_PASSWORD)",
)
@click.pass_context
def cli(ctx: click.Context, **flags: str | None) -> None:
    """Stash the global flags; the instance resolves lazily per command.

    Resolution stays out of the group callback so commands that need no
    target (config init) never trigger it; per key a flag beats the
    ACU_* var (.env or process) beats the code default (I.cmd precedence).
    """
    ctx.obj = {k: v for k, v in flags.items() if v is not None}


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


def _transport_target(
    exc: httpx.TransportError, *, target: str | None = None
) -> str | None:
    """Prefer explicit target (e.g. Instance.base_url); else request URL."""
    if target:
        return target
    request = getattr(exc, "request", None)
    if request is not None:
        return str(request.url)
    return None


def _format_transport_error(
    exc: httpx.TransportError, *, target: str | None = None
) -> str:
    """Friendly one-line body for connect/timeout/TLS (V9 / T123).

    Never surface raw httpx/OS dumps (e.g. ``[Errno 61] Connection refused``).
    """
    raw = str(exc).lower()
    if "ssl" in raw or "certificate" in raw or "tls" in raw:
        kind = "TLS error"
    elif isinstance(exc, httpx.TimeoutException):
        kind = "request timed out"
    elif isinstance(exc, httpx.ConnectError):
        kind = "cannot connect"
    elif isinstance(exc, httpx.NetworkError):
        kind = "network error"
    else:
        kind = "transport error"
    where = _transport_target(exc, target=target)
    hint = "check ACU_BASE_URL / network / instance up"
    if where:
        return f"{kind} to {where} ({hint})"
    return f"{kind} ({hint})"


def _format_failure(exc: BaseException, *, target: str | None = None) -> str:
    """Map expected failures to one greppable error line body (V9).

    Transport/network class → friendly rewrite; other RuntimeError / HTTPError
    keep ``str(exc)`` (server ``exceptionMessage`` path unchanged).
    """
    if isinstance(exc, httpx.TransportError):
        return _format_transport_error(exc, target=target)
    return str(exc)


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
        output.error(_format_failure(exc))
        raise SystemExit(1) from exc


@cli.group("tenant")
def tenant_group() -> None:
    """Tenant CRUD on the instance (ac.exe CompanyConfig over SSH)."""


@tenant_group.command("list")
@pass_instance
def tenant_list(inst: Instance) -> None:
    """List tenants: CompanyID, sign-in name, internal CD, type."""
    tenants = TenantManager(inst).list()
    # V9/B27: control-plane identity is the host, never scheme+path
    host = urlparse(inst.base_url).hostname or inst.base_url
    output.table(
        f"Tenants on {host}",
        ("ID", "Login", "CD", "Type"),
        (
            (str(t.company_id), t.login_name, t.company_cd, t.company_type)
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
    help="Acumatica tenant name as shown on the sign-in page",
)
@click.option(
    "--type",
    "company_type",
    # the V12-verified dataset folders on the box (docs/ac-exe.md); System
    # is the system-tenant dataset, deliberately not offered
    type=click.Choice(["SalesDemo", "T100", "U100"]),
    default=None,
    help="Data set inserted at creation (omit for a clean tenant); "
    "T100/U100 are the Acumatica University training sets",
)
@click.option("--parent", "parent_id", type=int, default=1, show_default=True)
@click.option("--hidden", is_flag=True, help="Do not show on the sign-in page")
@click.option(
    "--no-init",
    is_flag=True,
    help="Skip app-pool recycle, first-login password change, and bootstrap",
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
    """Create a tenant and bootstrap it - ready for `acu apply` in one step.

    Chains the verified steps (docs/ac-exe.md, docs/rest-api.md): ac.exe
    CompanyConfig with the admin password preset, an app-pool recycle so the
    running app sees the tenant, a REST login check (with the sign-in
    screen's first-login password-change flow as fallback), then the
    bootstrap package publish that makes the virgin tenant configurable
    (features on, Bootstrap endpoint up). --no-init skips everything after
    the create: an unrecycled tenant is invisible to REST, so the bootstrap
    chain cannot run either.

    --login is required; --id is optional (V16 login-only). Omit --id →
    next free CompanyID = max(live list)+1 (ac.exe never auto-picks). When
    the login already exists: omit --id → adopt the live CompanyID; pass
    --id → must match, else hard error naming both.

    Resumable (V4, closes B17): when the login already exists on the
    instance (tenant list probe - live state, never a marker) the ac.exe
    create is skipped and the init + digest-gated publish chain still runs,
    so re-running create is the republish route for existing tenants.

    After create (or on exists-skip), CompanyCD is set equal to --login.
    ac.exe only writes LoginName into CompanyKey and auto-generates CD
    (Company2, Company3, …); the align step matches what operators do on
    the Companies screen so ``acu tenant list`` shows Login = CD.
    """
    mgr = TenantManager(inst)
    tenants = mgr.list()
    existing = next((t for t in tenants if t.login_name == login_name), None)
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
    # Idempotent on re-run / exists-skip (docs/ac-exe.md).
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
    help="Write the package zip to PATH (offline; no REST, no SSH) "
    "for Customization Projects UI import",
)
@click.pass_context
def bootstrap_cmd(ctx: click.Context, export_path: Path | None) -> None:
    """Publish AcuBootstrap into the session tenant (or export the package zip).

    Hosted / no-SSH path: CustomizationApi publish is pure REST, so this
    command never requires ACU_SSH. When SSH is set, an app-pool recycle
    follows publish so feature-gated screens load; when unset, a warning
    notes that the site may need another restart before apply.

    --export writes the same feature-spliced package that publish would
    import, with no HTTP and no password — the SM204505 UI-import fallback.
    """
    features = _bootstrap_features()
    if export_path is not None:
        if not export_path.parent.exists():
            raise SystemExit(f"{export_path.parent}: directory does not exist")
        export_path.write_bytes(bootstrap.package_zip(features))
        output.success(f"wrote {export_path}")
        return
    inst = _resolve_instance(ctx)
    assert_target_compatible(inst)
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
    help="CompanyID (from `acu tenant list`)",
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

    Pass exactly one of --id or --login (login = sign-in name from
    `acu tenant list`). Confirm prompt; --yes skips.
    """
    if (company_id is None) == (login_name is None):
        raise SystemExit("pass exactly one of --id or --login")
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

    Control plane only (SSH): ``Restart-WebAppPool`` for the instance pool.
    Reloads the tenant map (V5) and drops every in-flight session so concurrent
    API-user license slots free up. Hosted / empty ACU_SSH hard-errors before
    any remote (same as other tenant cmds).
    """
    mgr = TenantManager(inst)
    with output.step("recycling app pool (tenant map + free API-user sessions)"):
        mgr.recycle_app_pool()
    output.success("app pool recycled")


@cli.group("config")
def config_group() -> None:
    """Configuration ops.

    init = local write, show = local read, check = live read-only preflight.
    """


@config_group.command("init")
@click.option(
    "--host",
    default=None,
    help="Hostname substituted into the scaffolded .env ACU_BASE_URL "
    "(default: a placeholder); ACU_SSH is omitted and defaults from that host",
)
@click.argument(
    "directory", required=False, type=click.Path(file_okay=False, path_type=Path)
)
def config_init(host: str | None, directory: Path | None) -> None:
    """Scaffold a data repo: .env, target.yaml, config/ seed, scenario/.

    Templates ship with the package; every value is a placeholder or a
    verified example - no secrets. Single full seed under ``config/``
    (bootstrap/baseline/setup/master) + lifecycle ``scenario/`` +
    ``config/views/`` + README. Bootstrap contract stays package SoT
    (never scaffolds ``project.xml`` — V2/V21/V28). No ``--flavor``
    (V28/T108). Existing files are never overwritten (reported as
    skipped). DIRECTORY defaults to the current directory and is created
    if absent. No git init, no gpg.
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
    output.data("  4. acu apply config/")
    output.data("  5. acu run scenario/")
    output.data("  6. acu diff config/")
    output.data("  7. acu state")


@config_group.command("show")
@pass_instance
def config_show(inst: Instance) -> None:
    """Print the fully resolved configuration as a complete .env document.

    Resolves through the same load_instance path every live command uses,
    so the printed values are exactly what a live command would trust -
    global flag overrides (--url, --ssh, ...) included. The password is
    never emitted in any form (V2): no ACU_PASSWORD key, no value.
    ``ACU_API_VERSION`` is never emitted (V27/T125 — api pin is not env;
    source is target.yaml ``default_api`` or ``--api-version``). When
    target.yaml is present, surfaces erp/default_api as comments and notes
    the api_version source (still exit 0 — no hard gate). Redirect to a
    file and edit: the output loads back through load_instance unchanged,
    the password supplied out of band.
    """
    output.data("# resolved by `acu config show` - a complete .env")
    output.data("# ACU_PASSWORD comes from .env or the environment, never from here")
    # V27: api pin is not an env key — never emit ACU_API_VERSION
    for field, value in inst.model_dump(exclude={"password", "api_version"}).items():
        output.data(f"ACU_{field.upper()}={value}")
    target = load_target()
    if target is not None:
        output.data(f"# target.yaml: erp={target.erp} default_api={target.default_api}")
        if inst.api_version == target.default_api:
            output.data(
                f"# api_version={inst.api_version} (from target.yaml default_api)"
            )
        else:
            output.data(
                f"# api_version={inst.api_version} (from --api-version; "
                f"target default_api={target.default_api})"
            )
    else:
        output.data(
            f"# api_version={inst.api_version} "
            f"(code default or --api-version; no target.yaml)"
        )


@config_group.command("check")
@click.option(
    "--strict",
    is_flag=True,
    help="Promote warn classes (missing target.yaml) to fail",
)
@click.pass_context
def config_check(ctx: click.Context, strict: bool) -> None:
    """Read-only preflight of the resolved target, one ok/fail/warn/skip line.

    Dependency order: discovery (.env walk-up + parse + ACU_BASE_URL), then
    secrets (ACU_PASSWORD resolved), then local target.yaml probe, then REST
    (login, landed-tenant verify, logout) and ssh probed independently -
    ssh set → trivial remote; ssh unset → skip (ACU_SSH optional, V3 hosted
    path), never fail. A discovery or secrets failure stops; a REST failure
    still probes ssh when set and vice versa. Discovery is lax (V3): no
    .env passes when --url covers base_url, and flags-only runs (no .env
    anywhere) are valid. Writes nothing: no PUTs, no tenant CRUD. Exit 0
    when no fail line (warns allowed); --strict promotes missing target to
    fail; exit 1 on any failure.
    """
    overrides: dict[str, str] = ctx.obj or {}
    # discovery (V3): lax walk-up + parse; base_url (the primary identity
    # key) must be resolvable from the flag, the process environment, or
    # the found .env - the same sources load_instance merges
    root = find_data_root()
    try:
        env_values = read_env_values(root / ".env") if root is not None else {}
        if not (
            overrides.get("base_url")
            or os.environ.get("ACU_BASE_URL")
            or env_values.get("base_url")
        ):
            source = f"{root / '.env'}:" if root is not None else "no .env found and"
            raise SystemExit(f"{source} missing required key ACU_BASE_URL (or --url)")
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
    failed, claimed_erp = _probe_target(root, inst, strict=strict)
    if not _probe_rest(inst, claimed_erp):
        failed = True
    if not _probe_ssh(inst):
        failed = True
    if failed:
        raise SystemExit(1)


def _probe_rest(inst: Instance, claimed_erp: str | None) -> bool:
    """REST login + endpoints + optional ERP build; True = all passed.

    Entering the client is the probe: login + landed-tenant verify (V5);
    context manager guarantees logout (V6). One GET /entity feeds endpoints
    (T74/T90) and optional ERP build (T92).
    """
    try:
        with AcumaticaClient(inst) as client:
            output.data(f"ok rest ({inst.base_url}, tenant {inst.tenant})")
            return _probe_entity_root(client, inst, claimed_erp)
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail rest: {_format_failure(exc, target=inst.base_url)}")
        return False


def _probe_entity_root(
    client: AcumaticaClient, inst: Instance, claimed_erp: str | None
) -> bool:
    """Endpoints (+ ERP when claimed); True = all passed."""
    ok = True
    try:
        endpoints, live_build = client.entity_root()
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail endpoints: {_format_failure(exc, target=inst.base_url)}")
        endpoints, live_build = [], None
        ok = False
    else:
        if not _probe_endpoints(endpoints, inst):
            ok = False
    if claimed_erp is not None and not _probe_erp(claimed_erp, live_build):
        ok = False
    return ok


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


def _probe_target(
    root: Path | None, inst: Instance, *, strict: bool
) -> tuple[bool, str | None]:
    """Emit local target probe; return ``(failed, claimed_erp?)`` (V27).

    Invalid target hard-exits (any loader). Missing under data root warns
    unless --strict. No data root → skip. Match → ok + claimed ``erp`` for
    the live ERP probe after REST (T92).
    """
    try:
        target = load_target(root)
    except SystemExit as exc:
        output.data(f"fail target: {exc}")
        raise SystemExit(1) from exc
    if root is None:
        output.data("skip target (no data root)")
        return False, None
    if target is None:
        msg = (
            f"target: no target.yaml under {root} - dataset verified matrix "
            "unknown; add target.yaml (see acu config init) or pass --strict "
            "to require it"
        )
        output.data(f"{'fail' if strict else 'warn'} {msg}")
        return strict, None
    # V27/T125: source-merge — api_version already from default_api unless
    # --api-version; surface source, never dual-source mismatch fail
    if inst.api_version == target.default_api:
        output.data(
            f"ok target (api_version from default_api={target.default_api}; "
            f"erp={target.erp} claimed)"
        )
    else:
        output.data(
            f"ok target (api_version={inst.api_version} from --api-version; "
            f"default_api={target.default_api}; erp={target.erp} claimed)"
        )
    return False, target.erp


def _probe_endpoints(endpoints: list[tuple[str, str]], inst: Instance) -> bool:
    """Emit endpoints probe; return True on pass, False on fail (V12/V27).

    Exact match: a Default entry whose version half equals
    ``Instance.api_version``. Caller already fail-closed on unparseable GET.
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


def _major_minor(version: str) -> str:
    """First two dotted segments (T76/T92 major.minor match)."""
    parts = version.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}.{parts[1]}"
    return version


def _probe_erp(claimed: str, live: str | None) -> bool:
    """Emit ERP probe; return True on pass/skip, False on fail (T92).

    Live id comes from 26.x ``GET /entity`` wrapper
    ``version.acumaticaBuildVersion``. Bare array → skip (no HTTP surface).
    Compare major.minor only — patch builds may drift within a claimed line.
    """
    if live is None:
        output.data(f"skip erp (live probe not available; claimed {claimed})")
        return True
    if _major_minor(live) == _major_minor(claimed):
        output.data(f"ok erp ({live} matches claimed {claimed})")
        return True
    output.data(
        f"fail erp: live {live} vs claimed {claimed} "
        f"(major.minor {_major_minor(live)} vs {_major_minor(claimed)})"
    )
    return False


SEED_DIRS = ("bootstrap", "baseline", "setup", "master")


def _seed_child_dirs(parent: Path) -> list[Path]:
    """SEED_DIRS children of ``parent`` that exist, fixed order (V22/V30)."""
    return [parent / name for name in SEED_DIRS if (parent / name).is_dir()]


def default_seed_dirs() -> tuple[Path, ...]:
    """Default apply/diff dirs: prefer ``config/`` SEED_DIRS, else root (V30).

    When the data-repo ``config/`` has any SEED_DIRS child, only those
    ``config/<name>/`` paths are returned (dual layout never merges with
    root). Else root ``bootstrap/``…``master/`` for present names
    (legacy root layout). The data repo is the .env dir (V3 walk-up). None
    existing is an error - an empty default would make a bare run a silent
    no-op. Paths come back relative to cwd so a bare run prints exactly
    what naming the dirs would.
    """
    root = data_root()
    config = root / "config"
    if config.is_dir() and _seed_child_dirs(config):
        parents = _seed_child_dirs(config)
    else:
        parents = _seed_child_dirs(root)
    dirs = tuple(Path(os.path.relpath(d)) for d in parents)
    if not dirs:
        expected = ", ".join(f"{name}/" for name in SEED_DIRS)
        raise SystemExit(
            f"{root}: none of the seed directories exist (config/<name>/ or {expected})"
        )
    return dirs


def _leaf_yaml(directory: Path) -> list[Path]:
    """Sorted ``*.yaml`` in a leaf seed dir; skip ``features.yaml`` (I.data)."""
    return sorted(p for p in directory.glob("*.yaml") if p.name != "features.yaml")


def expand_files(files: tuple[Path, ...]) -> list[Path]:
    """Expand directory arguments into seed ``*.yaml`` files (V22/V30).

    A dir with any SEED_DIRS child (umbrella e.g. ``config/``) expands those
    nested subdirs in fixed SEED_DIRS order, then leaf ``*.yaml`` per subdir.
    A leaf dir expands its own ``*.yaml`` only. ``features.yaml`` is skipped:
    it configures the bootstrap package build, not an entity/records seed.
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


@cli.command("apply")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option("--dry-run", is_flag=True, help="Show what would be PUT without writing")
@pass_instance
def apply_cmd(inst: Instance, files: tuple[Path, ...], dry_run: bool) -> None:
    """Seed baseline YAML into the tenant (idempotent PUT upserts).

    FILES are baseline YAML files or directories containing them. A dir with
    SEED_DIRS children (e.g. config/) expands nested trees in fixed order.
    Omitted, defaults prefer config/<name>/ when present, else root SEED_DIRS
    (V30).

    Per-record failure isolation (V45): one failed PUT reports and continues;
    later records and files still run. Exit 1 with a multi-error summary when
    any record failed; never silent partial. Exit 2 stays with ``diff``.
    """
    assert_target_compatible(inst)
    total_ok = 0
    all_errors: list[str] = []
    with AcumaticaClient(inst) as client:
        for path in expand_files(files or default_seed_dirs()):
            baseline = seed.load_baseline(path)
            output.data(
                f"{path} -> {inst.tenant} on {inst.base_url} ({baseline.entity})"
            )
            n, errors = seed.apply(client, baseline, dry_run=dry_run)
            total_ok += n
            all_errors.extend(f"{path}: {e}" for e in errors)
            output.data(f"  {n} record(s){' (dry run)' if dry_run else ''}")
    if all_errors:
        output.error(
            f"apply: {len(all_errors)} error(s), {total_ok} record(s) applied"
            f"{' (dry run)' if dry_run else ''}"
        )
        for err in all_errors:
            output.error(err)
        raise SystemExit(1)


@cli.command("schema")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (default: <data repo>/schemas)",
)
@pass_instance
def schema_cmd(inst: Instance, out_dir: Path | None) -> None:
    """Dump the endpoint's OpenAPI schema (swagger.json) into schemas/.

    The schema is the authoritative field-level reference for the exact
    build - regenerate rather than version (the file is ~3 MB).
    """
    assert_target_compatible(inst)
    if out_dir is None:
        out_dir = data_root() / "schemas"
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
@pass_instance
def diff_cmd(inst: Instance, files: tuple[Path, ...]) -> None:
    """Compare baseline YAML against the live tenant; exit 2 on drift.

    FILES are baseline YAML files or directories containing them. A dir with
    SEED_DIRS children (e.g. config/) expands nested trees in fixed order.
    Omitted, defaults prefer config/<name>/ when present, else root SEED_DIRS
    (V30).
    """
    assert_target_compatible(inst)
    paths = expand_files(files or default_seed_dirs())
    drifts: list[str] = []
    with AcumaticaClient(inst) as client:
        for path in paths:
            baseline = seed.load_baseline(path)
            # same file banner as apply so a long multi-file diff shows
            # progress instead of silence until the final status line
            output.data(
                f"{path} -> {inst.tenant} on {inst.base_url} ({baseline.entity})"
            )
            file_drifts = seed.diff(client, baseline)
            drifts += file_drifts
            n = 1 if isinstance(baseline, seed.ActionFile) else len(baseline.records)
            if file_drifts:
                output.data(f"  {n} record(s), {len(file_drifts)} drift(s)")
            else:
                output.data(f"  {n} record(s) ok")
    _exit_on_drift(inst, drifts, len(paths))


@cli.command("run")
@click.argument(
    "files", nargs=-1, required=False, type=click.Path(exists=True, path_type=Path)
)
@click.option("--dry-run", is_flag=True, help="Parse and list steps without any HTTP")
@pass_instance
def run_cmd(inst: Instance, files: tuple[Path, ...], dry_run: bool) -> None:
    """Execute transaction scenario YAML against the live tenant.

    FILES are scenario YAML files or directories containing them. Omitted,
    they default to the data repo's scenario/ directory. Transactions are
    executed forward (the server assigns document numbers), never upserted;
    delta expectations snapshot before the first step and compare after the
    last, so a scenario re-runs safely on a warm tenant. Built-in
    ``${current_period}`` expands to host-local MMyyyy on steps, expect
    params, and once.present params (views for ``acu state`` stay pinned).
    Exit 0 when every expectation holds, 1 on any step error or expectation
    miss.
    """
    assert_target_compatible(inst)
    if not files:
        default = data_root() / "scenario"
        if not default.is_dir():
            raise SystemExit(f"{default}: scenario directory does not exist")
        files = (Path(os.path.relpath(default)),)
    paths = expand_files(files)
    scenarios = [run.load_scenario(path) for path in paths]
    ok = True
    if dry_run:
        for scenario in scenarios:
            run.run(None, scenario, dry_run=True)
    else:
        with AcumaticaClient(inst) as client:
            for scenario in scenarios:
                ok = run.run(client, scenario) and ok
    if not ok:
        raise SystemExit(1)
    output.success(f"{len(scenarios)} scenario(s) passed on {inst.tenant}")


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


@cli.command("extract")
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (default: current directory)",
)
@click.option(
    "--only",
    multiple=True,
    shell_complete=_complete_only,
    help="Limit to matching catalog rows (entity name or file stem); repeatable",
)
@click.option("--force", is_flag=True, help="Overwrite existing files")
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
    """Extract live tenant state into seed YAML under config/ (inverse of apply).

    Catalog-driven (packaged ``seed_catalog.yaml`` is the verified entity
    registry): each row is read from the live tenant and written under
    ``config/{bootstrap,baseline,setup,master}/`` (hard-cut; never root
    SEED_DIRS; no ``--layout``). Apply and diff consume those files
    unchanged. Features synthesize to ``config/bootstrap/features.yaml``.
    Existing files are skipped unless --force; an entity with no live
    records produces no file. A failing row is reported and the run
    continues (a virgin tenant extracts whole). Exit 0 when every row
    wrote or skipped clean, 1 when any row failed - drift stays with diff.
    """
    assert_target_compatible(inst)
    with AcumaticaClient(inst) as client:
        failed = extract.run(
            client,
            out_dir or Path("."),
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


@cli.command("inventory")
@click.argument(
    "artifact",
    type=click.Path(exists=True, path_type=Path),
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output directory (default: inventory/)",
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
    """Parse a snapshot artifact into inventory/ (offline dual-reader).

    ARTIFACT is an SM203520 Settings XML ZIP (manifest.xml + table XML) or
    an ac.exe export xml folder. Both normalize to one IR (V37). Binary
    .adb is rejected. No REST, no SSH, no password. Writes summary.yaml
    and tables/<Table>.yaml under --out (default inventory/). Existing
    files are skipped unless --force. When data-repo target.yaml is
    present and the artifact reports a build, erp must match or the run
    fails. Exit 0 clean, 1 parse/format/version fail; never 2 (drift is
    not this command). Not extract (REST seed into config/), not state
    (derived balances) — never writes config/ or state/ (V35).
    """
    # V9 long single-op: artifact parse (ZIP/folder IR) via step; banner +
    # per-table write/skip emit stay multi-unit stdout after.
    with output.step(f"parsing snapshot artifact {artifact}"):
        art = inventory.parse_artifact(artifact)
    target = load_target()
    if target is not None:
        inventory.assert_erp_matches(art, target.erp)
    dest = out_dir if out_dir is not None else Path(inventory.DEFAULT_OUT)
    output.data(
        f"{artifact} -> {dest} ({len(art.tables)} table(s)"
        f"{f', erp={art.erp}' if art.erp else ''})"
    )
    inventory.emit(art, dest, force=force, dry_run=dry_run)


@cli.command("reconcile")
@click.option(
    "--inventory",
    "inventory_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Inventory tree directory (default: inventory/)",
)
@click.option(
    "--config",
    "config_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Optional config/ seed tree (default: config/ when present)",
)
@click.option(
    "--out",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Findings output directory (default: findings/)",
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
    """Compare inventory/ to optional config/ and write findings/ (offline).

    Dual-reader cross-check (V35/V36): reads an inventory tree from
    `acu inventory` plus an optional prior-extract config/ seed tree;
    emits FindingsBundle under --out (default findings/). Never writes
    config/ (not extract promote) and never captures state/ — unmapped
    tables, REST gaps, rest-vs-snapshot field deltas, and Usr* custom
    columns land in findings files only. No REST, no SSH, no password.
    Exit 0 clean, 1 IO/parse fail; never 2 (conflicts are findings, not
    drift). Optional snapshot_map.yaml maps DAC tables to catalog
    entities (absent → identity match on entity name) and may declare
    keys:/fields: seed→inv aliases, resolvers:/resolves: for FK int→CD,
    and enums: label→code (package defaults cover Sub/UOM aliases,
    ReasonCode/VendorClass/PostingClass/CashAccount Account+Sub,
    ReasonCode.Usage + Account Type/Active bools, etc.). Compare always
    pad-trims strings.
    """
    inv = (
        inventory_dir
        if inventory_dir is not None
        else Path(reconcile.DEFAULT_INVENTORY)
    )
    if config_dir is not None:
        cfg: Path | None = config_dir
    else:
        default_cfg = Path(reconcile.DEFAULT_CONFIG)
        cfg = default_cfg if default_cfg.is_dir() else None
    dest = out_dir if out_dir is not None else Path(reconcile.DEFAULT_OUT)
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
    help="Observation output directory (default: state/)",
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
@pass_instance
def state_cmd(
    inst: Instance,
    files: tuple[Path, ...],
    out_dir: Path | None,
    do_diff: bool,
    assert_unchanged: bool,
    dry_run: bool,
) -> None:
    """Capture live derived state into committed observation files.

    FILES are view YAML files or directories. Omitted, they default to the
    data repo's config/views/ directory (hard-cut; no config/snapshot/
    fallback). Default write target is state/ (--out). Bare capture writes
    observations (change is fine). --diff compares live to disk without
    writing. --assert-unchanged is the warm-run idempotence gate (exit 2
    when moved). Never writes seed trees or endpoint: symbols (V32). Exit 0
    ok, 1 op fail, 2 only under --assert-unchanged when state moved.
    """
    assert_target_compatible(inst)
    if not files:
        default = data_root() / "config" / "views"
        if not default.is_dir():
            raise SystemExit(f"{default}: views directory does not exist")
        files = (Path(os.path.relpath(default)),)
    paths = state.expand_view_files(files)
    views = [state.load_view(path) for path in paths]
    dest = out_dir if out_dir is not None else Path("state")
    if dry_run:
        code = state.run_views(None, views, out_dir=dest, mode="dry")
    else:
        mode = "assert" if assert_unchanged else "diff" if do_diff else "write"
        with AcumaticaClient(inst) as client:
            code = state.run_views(client, views, out_dir=dest, mode=mode)
    if code:
        raise SystemExit(code)
    if dry_run:
        return
    if assert_unchanged:
        output.success(f"{len(views)} view(s) unchanged on {inst.tenant}")
    elif do_diff:
        output.success(f"{len(views)} view(s) compared on {inst.tenant}")
    else:
        output.success(f"{len(views)} view(s) written under {dest} on {inst.tenant}")
