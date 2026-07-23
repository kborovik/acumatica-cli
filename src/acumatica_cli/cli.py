"""acu - Acumatica configuration as code."""

import functools
import json
import os
from collections.abc import Callable
from importlib.metadata import distribution
from pathlib import Path
from typing import Concatenate

import click
import httpx
from click.shell_completion import get_completion_class

from . import bootstrap, extract, firstlogin, output, run, seed
from .client import AcumaticaClient
from .config import (
    INIT_FLAVORS,
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


def main() -> None:
    """Entry point: run the CLI, mapping expected failures to one-line errors.

    RuntimeError (SSH/ac.exe, REST, first-login) and httpx transport errors
    print `x message` and exit 1; ACU_DEBUG=1 re-raises for the traceback.
    """
    try:
        cli()
    except (RuntimeError, httpx.HTTPError) as exc:
        if os.environ.get("ACU_DEBUG"):
            raise
        output.error(str(exc))
        raise SystemExit(1) from exc


@cli.group("tenant")
def tenant_group() -> None:
    """Tenant CRUD on the instance (ac.exe CompanyConfig over SSH)."""


@tenant_group.command("list")
@pass_instance
def tenant_list(inst: Instance) -> None:
    """List tenants: CompanyID, sign-in name, internal CD, type."""
    tenants = TenantManager(inst).list()
    output.table(
        f"Tenants on {inst.base_url}",
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
    required=True,
    help="CompanyID (first free is usually 3)",
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
    company_id: int,
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

    Resumable (V4, closes B17): when the login already exists on the
    instance (tenant list probe - live state, never a marker) the ac.exe
    create is skipped and the init + digest-gated publish chain still runs,
    so re-running create is the republish route for existing tenants. --id
    must match the existing CompanyID, else hard error naming both.
    """
    mgr = TenantManager(inst)
    existing = next((t for t in mgr.list() if t.login_name == login_name), None)
    if existing is not None:
        if existing.company_id != company_id:
            raise SystemExit(
                f"tenant {login_name} exists with CompanyID "
                f"{existing.company_id}, not {company_id}; "
                f"pass --id {existing.company_id}"
            )
        output.data(f"skip create: tenant {login_name} exists (id {company_id})")
    else:
        with output.step(
            f"creating tenant {company_id} ({login_name}) on {inst.base_url}"
        ):
            # None = --type omitted = clean tenant (ac.exe's empty CompanyType)
            raw = mgr.create(
                company_id, login_name, parent_id, not hidden, company_type or ""
            )
        output.data(raw.splitlines()[-1] if raw.strip() else "created")
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
@click.option("--id", "company_id", type=int, required=True)
@click.confirmation_option(prompt="Delete this tenant and all its data?")
@pass_instance
def tenant_delete(inst: Instance, company_id: int) -> None:
    """Delete the tenant and all its data, then recycle the app pool."""
    mgr = TenantManager(inst)
    raw = mgr.delete(company_id)
    output.data(raw.splitlines()[-1] if raw.strip() else "done")
    with output.step("recycling app pool (drops the tenant from the running app)"):
        mgr.recycle_app_pool()


@cli.group("config")
def config_group() -> None:
    """Configuration ops.

    init = local write, show = local read, check = live read-only preflight.
    """


@config_group.command("init")
@click.option(
    "--host",
    default=None,
    help="Hostname substituted into the scaffolded .env ACU_BASE_URL/ACU_SSH "
    "values (default: a placeholder)",
)
@click.option(
    "--flavor",
    type=click.Choice(sorted(INIT_FLAVORS), case_sensitive=True),
    default=None,
    help="Template set: omit for finance-minimal (default, offline e2e); "
    "distribution = full virgin-tenant demo seed (master/, scenario/, "
    "bootstrap/project.xml)",
)
@click.argument(
    "directory", required=False, type=click.Path(file_okay=False, path_type=Path)
)
def config_init(host: str | None, flavor: str | None, directory: Path | None) -> None:
    """Scaffold a data repo: .env, .gitignore, bootstrap/, baseline/, setup/.

    Templates ship with the package; every value is a placeholder or a
    verified minimal example - no secrets. ``--flavor distribution`` adds
    Bootstrap contract, expanded COA, master/, scenario/, and README (V28).
    Existing files are never overwritten (reported as skipped). DIRECTORY
    defaults to the current directory and is created if absent. No git
    init, no gpg.
    """
    target = directory or Path.cwd()
    for action, path in scaffold(target, host=host, flavor=flavor):
        suffix = " (exists)" if action == "skip" else ""
        output.data(f"{action} {path}{suffix}")
    # next-step cmds (issue #18): operator rebuild order after scaffold
    output.data("")
    output.data("next:")
    output.data("  1. edit .env (set ACU_PASSWORD, ACU_TENANT; keep ACU_API_VERSION)")
    output.data("  2. acu config check")
    output.data("  3. acu bootstrap          # or: acu tenant create ... (SSH)")
    output.data("  4. acu apply")
    if flavor == "distribution":
        output.data("  5. acu run scenario/")
        output.data("  6. acu diff")
    else:
        output.data("  5. acu diff")


@config_group.command("show")
@pass_instance
def config_show(inst: Instance) -> None:
    """Print the fully resolved configuration as a complete .env document.

    Resolves through the same load_instance path every live command uses,
    so the printed values are exactly what a live command would trust -
    global flag overrides (--url, --ssh, ...) included. The password is
    never emitted in any form (V2): no ACU_PASSWORD key, no value.
    When target.yaml is present, surfaces erp/default_api as comments
    (mismatch noted, still exit 0 — no hard gate). Redirect to a file and
    edit: the output loads back through load_instance unchanged, the
    password supplied out of band.
    """
    output.data("# resolved by `acu config show` - a complete .env")
    output.data("# ACU_PASSWORD comes from .env or the environment, never from here")
    for field, value in inst.model_dump(exclude={"password"}).items():
        output.data(f"ACU_{field.upper()}={value}")
    target = load_target()
    if target is not None:
        output.data(f"# target.yaml: erp={target.erp} default_api={target.default_api}")
        if target.default_api != inst.api_version:
            output.data(
                f"# warn: default_api={target.default_api} != "
                f"ACU_API_VERSION={inst.api_version}"
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
    failed = _probe_target(root, inst, strict=strict)
    try:
        # entering the client is the whole probe: login + landed-tenant
        # verify (V5), and the context manager guarantees logout (V6);
        # endpoints probe (T74) reuses the same session when login succeeds
        with AcumaticaClient(inst) as client:
            output.data(f"ok rest ({inst.base_url}, tenant {inst.tenant})")
            if not _probe_endpoints(client, inst):
                failed = True
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail rest: {exc}")
        failed = True
    if not inst.ssh:
        # V3/I.cmd: ACU_SSH optional — hosted data-plane path skips, never fails
        output.data("skip ssh (ACU_SSH not set)")
    else:
        try:
            TenantManager(inst).ping()
            output.data(f"ok ssh ({inst.ssh})")
        except RuntimeError as exc:
            output.data(f"fail ssh: {exc}")
            failed = True
    if failed:
        raise SystemExit(1)


def _probe_target(root: Path | None, inst: Instance, *, strict: bool) -> bool:
    """Emit the local target probe line; return True when it failed (V27).

    Invalid target hard-exits (any loader). Missing under data root warns
    unless --strict. No data root → skip. Match → ok (erp claimed-only).
    """
    try:
        target = load_target(root)
    except SystemExit as exc:
        output.data(f"fail target: {exc}")
        raise SystemExit(1) from exc
    if root is None:
        output.data("skip target (no data root)")
        return False
    if target is None:
        msg = (
            f"target: no target.yaml under {root} - dataset verified matrix "
            "unknown; add target.yaml (see acu config init) or pass --strict "
            "to require it"
        )
        output.data(f"{'fail' if strict else 'warn'} {msg}")
        return strict
    if target.default_api != inst.api_version:
        output.data(
            f"fail target: dataset default_api={target.default_api} vs "
            f"configured {inst.api_version}"
        )
        return True
    output.data(
        f"ok target (default_api={target.default_api} matches configured; "
        f"erp={target.erp} claimed)"
    )
    # T76: no stable HTTP ERP-build discovery (V12) — keep erp claimed-only;
    # skip rather than invent SSH/sqlcmd (control-plane exclusion, V1)
    output.data(f"skip erp (live probe not available; claimed {target.erp})")
    return False


def _probe_endpoints(client: AcumaticaClient, inst: Instance) -> bool:
    """Emit endpoints probe; return True on pass, False on fail (V12/V27).

    Exact match: a Default entry whose version half equals
    ``Instance.api_version``. Fail-closed when GET /entity is unparseable.
    """
    want = f"Default/{inst.api_version}"
    try:
        endpoints = client.list_endpoints()
    except (RuntimeError, httpx.HTTPError) as exc:
        output.data(f"fail endpoints: {exc}")
        return False
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


def default_seed_dirs() -> tuple[Path, ...]:
    """The init-scaffolded seed dirs that exist at the data-repo root.

    apply and diff default to these when called with no FILES, in fixed
    order (bootstrap, baseline, setup, master); only dirs that exist are
    included (finance-minimal has no master/; distribution does). The data
    repo is the .env dir (V3 walk-up). None existing is an error - an empty
    default would make a bare run a silent no-op. Paths come back relative
    to cwd (the root is always cwd or an ancestor), so a bare run prints
    exactly what naming the dirs would.
    """
    root = data_root()
    dirs = tuple(
        Path(os.path.relpath(d)) for name in SEED_DIRS if (d := root / name).is_dir()
    )
    if not dirs:
        expected = ", ".join(f"{name}/" for name in SEED_DIRS)
        raise SystemExit(f"{root}: none of the seed directories exist ({expected})")
    return dirs


def expand_files(files: tuple[Path, ...]) -> list[Path]:
    """Expand directory arguments into their *.yaml files, sorted.

    features.yaml is skipped: it configures the bootstrap package build
    (a feature-name list, SPEC I.data), not an entity/records seed file.
    """
    paths: list[Path] = []
    for path in files:
        if path.is_dir():
            found = sorted(p for p in path.glob("*.yaml") if p.name != "features.yaml")
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

    FILES are baseline YAML files or directories containing them. Omitted,
    they default to the data repo's existing init-scaffolded directories in
    fixed order: bootstrap/, baseline/, setup/, master/ (when present).
    """
    assert_target_compatible(inst)
    with AcumaticaClient(inst) as client:
        for path in expand_files(files or default_seed_dirs()):
            baseline = seed.load_baseline(path)
            output.data(
                f"{path} -> {inst.tenant} on {inst.base_url} ({baseline.entity})"
            )
            n = seed.apply(client, baseline, dry_run=dry_run)
            output.data(f"  {n} record(s){' (dry run)' if dry_run else ''}")


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

    FILES are baseline YAML files or directories containing them. Omitted,
    they default to the data repo's existing init-scaffolded directories in
    fixed order: bootstrap/, baseline/, setup/, master/ (when present).
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
    last, so a scenario re-runs safely on a warm tenant. Exit 0 when every
    expectation holds, 1 on any step error or expectation miss.
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
    """--only value completion: entity names off the packaged manifest.

    Fires per keystroke, so it stays local-only (V23): the manifest is
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
    help="Limit to matching manifest rows (entity name or file stem); repeatable",
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
    """Extract live tenant state into seed YAML files (the inverse of apply).

    Manifest-driven (the packaged extract manifest carries the verified
    entity set): each entity is read from the live tenant and written as a
    seed file under bootstrap/ or baseline/ that apply and diff consume
    unchanged. Existing files are skipped unless --force; an entity with
    no live records produces no file. A failing row is reported and the
    run continues to the next (a virgin tenant extracts whole). Exit 0
    when every row wrote or skipped clean, 1 when any row failed - drift
    detection stays with diff.
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
