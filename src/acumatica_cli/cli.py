"""acu - Acumatica configuration as code.

acu provision --id N --login T   one command: create -> bootstrap -> apply -> diff
acu tenant list|create|delete    tenant CRUD (ac.exe over SSH)
acu apply [--dry-run] FILES...   seed baseline YAML via the REST API
acu diff FILES...                drift check: baseline vs live tenant
acu schema [--out DIR]           dump the endpoint's OpenAPI schema
acu config show                  print the resolved target instance
"""

import os
from pathlib import Path

import click
import httpx
import yaml

from . import bootstrap, firstlogin, output, seed
from .client import AcumaticaClient
from .config import Instance, data_root, load_instance
from .tenant import TenantManager


@click.group(help=__doc__)
@click.version_option(package_name="acumatica-cli")
@click.option(
    "-t", "--tenant", default=None, help="Override the tenant API sessions sign in to"
)
@click.option(
    "--host",
    default=None,
    help="Override the acu.yaml host (base_url/ssh re-derive from it)",
)
@click.pass_context
def cli(ctx: click.Context, tenant: str | None, host: str | None) -> None:
    """Resolve the target instance and stash it in the Click context."""
    inst = load_instance(host=host)
    if tenant is not None:
        inst = inst.model_copy(update={"tenant": tenant})
    ctx.obj = inst


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
@click.pass_obj
def tenant_list(inst: Instance) -> None:
    """List tenants: CompanyID, sign-in name, internal CD, type."""
    tenants = TenantManager(inst).list()
    output.table(
        f"Tenants on {inst.host}",
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
    default="",
    help="Inserted data set: '' = clean, SalesDemo = demo",
)
@click.option("--parent", "parent_id", type=int, default=1, show_default=True)
@click.option("--hidden", is_flag=True, help="Do not show on the sign-in page")
@click.option(
    "--no-init",
    is_flag=True,
    help="Skip app-pool recycle + first-login password change",
)
@click.pass_obj
def tenant_create(
    inst: Instance,
    company_id: int,
    login_name: str,
    company_type: str,
    parent_id: int,
    hidden: bool,
    no_init: bool,
) -> None:
    """Create a tenant and make it automation-ready.

    Chains the verified steps (docs/ac-exe.md, docs/rest-api.md): ac.exe
    CompanyConfig with the admin password preset, an app-pool recycle so the
    running app sees the tenant, then a REST login check (with the sign-in
    screen's first-login password-change flow as fallback).
    """
    mgr = TenantManager(inst)
    with output.step(f"creating tenant {company_id} ({login_name}) on {inst.host}"):
        raw = mgr.create(company_id, login_name, parent_id, not hidden, company_type)
    output.data(raw.splitlines()[-1] if raw.strip() else "created")
    if no_init:
        output.warn("skipping init: tenant is invisible until an app-pool recycle")
        return
    _init_tenant(inst, mgr, login_name)


def _init_tenant(inst: Instance, mgr: TenantManager, login_name: str) -> None:
    """Make a freshly created tenant automation-ready (V5 recycle + login check)."""
    with output.step("recycling app pool (tenant map loads at app start)"):
        mgr.recycle_app_pool()
    with output.step("verifying REST login (screen-flow password change as fallback)"):
        result = firstlogin.initialize_admin_password(inst, tenant=login_name)
    output.success(f"admin {result}; tenant {login_name} is ready")


@tenant_group.command("delete")
@click.option("--id", "company_id", type=int, required=True)
@click.confirmation_option(prompt="Delete this tenant and all its data?")
@click.pass_obj
def tenant_delete(inst: Instance, company_id: int) -> None:
    """Delete the tenant and all its data, then recycle the app pool."""
    mgr = TenantManager(inst)
    raw = mgr.delete(company_id)
    output.data(raw.splitlines()[-1] if raw.strip() else "done")
    with output.step("recycling app pool (drops the tenant from the running app)"):
        mgr.recycle_app_pool()


@cli.group("config")
def config_group() -> None:
    """Local read-only config inspection (never talks to a live instance)."""


@config_group.command("show")
@click.pass_obj
def config_show(inst: Instance) -> None:
    """Print the fully resolved instance as a complete acu.yaml document.

    Resolves through the same load_instance path every live command uses,
    so the printed values are exactly what a live command would trust.
    Credentials never appear - redirect to a file and edit: the output
    loads back through load_instance unchanged.
    """
    doc = inst.model_dump(exclude={"username", "password"})
    output.data("# resolved by `acu config show` - a complete acu.yaml")
    output.data(
        "# credentials come from .env (ACU_USER / ACU_PASSWORD), never from here"
    )
    for line in yaml.safe_dump(doc, sort_keys=False).splitlines():
        output.data(line)


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


@cli.command("provision")
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
    default="",
    help="Inserted data set: '' = clean, SalesDemo = demo",
)
@click.option("--parent", "parent_id", type=int, default=1, show_default=True)
@click.pass_obj
def provision_cmd(
    inst: Instance,
    company_id: int,
    login_name: str,
    company_type: str,
    parent_id: int,
) -> None:
    """One command to a configured tenant: create -> bootstrap -> apply -> diff.

    Chains the verified pipeline (docs/rest-api.md) idempotently: tenant
    create over SSH (skipped when the login name already exists), bootstrap
    package publish (skipped when already published), bootstrap/ YAML through
    the custom endpoint, baseline/ through the Default endpoint, then a drift
    check over everything applied - exit 2 on drift.
    """
    root = data_root()
    baseline_dir = root / "baseline"
    if not baseline_dir.is_dir():
        raise SystemExit(f"{baseline_dir}: not a directory - nothing to provision")
    seed_dirs = [d for d in (root / "bootstrap", baseline_dir) if d.is_dir()]
    # every session below signs in to the provisioned tenant, never a default
    inst = inst.model_copy(update={"tenant": login_name})

    mgr = TenantManager(inst)
    if any(t.login_name == login_name for t in mgr.list()):
        output.info(f"tenant {login_name} exists on {inst.host} - skipping create")
    else:
        with output.step(f"creating tenant {company_id} ({login_name}) on {inst.host}"):
            raw = mgr.create(company_id, login_name, parent_id, True, company_type)
        output.data(raw.splitlines()[-1] if raw.strip() else "created")
        _init_tenant(inst, mgr, login_name)

    # feature set = data (V2): bootstrap/features.yaml, absent -> built-in six
    features = bootstrap.load_features(root)
    with (
        output.step(f"publishing {bootstrap.PACKAGE_NAME} to {inst.tenant}"),
        AcumaticaClient(inst) as client,
    ):
        result = bootstrap.publish(client, features=features)
    output.success(f"{bootstrap.PACKAGE_NAME} {result}")
    # unconditional: the publish restarts the site BEFORE its DB transaction
    # commits, so the restarted domain caches the feature slot pre-plugin
    # (verified live: gated screens stay 403 until one more recycle); on the
    # skip path a recycle is the cheap way to make a resumed run sound too
    with output.step("recycling app pool (feature set loads at app start)"):
        mgr.recycle_app_pool()
    with output.step("waiting for the site to come back"):
        firstlogin.initialize_admin_password(inst, tenant=login_name)

    # fresh session: publishing restarts the app domain, so don't trust the
    # cookie that watched it happen
    drifts: list[str] = []
    seed_paths = expand_files(tuple(seed_dirs))
    with AcumaticaClient(inst) as client:
        for path in seed_paths:
            baseline = seed.load_baseline(path)
            output.data(f"{path} -> {inst.host}/{inst.tenant} ({baseline.entity})")
            n = seed.apply(client, baseline)
            output.data(f"  {n} record(s)")
        # diff everything applied, bootstrap YAML included - a PUT that
        # answers 200 can persist nothing (T3), the drift check is the proof
        for path in seed_paths:
            drifts += seed.diff(client, seed.load_baseline(path))
    _exit_on_drift(inst, drifts, len(seed_paths))


@cli.command("apply")
@click.argument(
    "files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path)
)
@click.option("--dry-run", is_flag=True, help="Show what would be PUT without writing")
@click.pass_obj
def apply_cmd(inst: Instance, files: tuple[Path, ...], dry_run: bool) -> None:
    """Seed baseline YAML into the tenant (idempotent PUT upserts).

    FILES are baseline YAML files or directories containing them.
    """
    with AcumaticaClient(inst) as client:
        for path in expand_files(files):
            baseline = seed.load_baseline(path)
            output.data(f"{path} -> {inst.host}/{inst.tenant} ({baseline.entity})")
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
@click.pass_obj
def schema_cmd(inst: Instance, out_dir: Path | None) -> None:
    """Dump the endpoint's OpenAPI schema (swagger.json) into schemas/.

    The schema is the authoritative field-level reference for the exact
    build - regenerate rather than version (the file is ~3 MB).
    """
    if out_dir is None:
        out_dir = data_root() / "schemas"
    out_file = out_dir / f"swagger-{inst.endpoint.replace('/', '-')}.json"
    with (
        output.step(f"dumping OpenAPI schema from {inst.host} ({inst.endpoint})"),
        AcumaticaClient(inst) as client,
    ):
        raw = client.swagger()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(raw)
    output.data(f"{out_file} ({len(raw)} bytes)")


@cli.command("diff")
@click.argument(
    "files", nargs=-1, required=True, type=click.Path(exists=True, path_type=Path)
)
@click.pass_obj
def diff_cmd(inst: Instance, files: tuple[Path, ...]) -> None:
    """Compare baseline YAML against the live tenant; exit 2 on drift.

    FILES are baseline YAML files or directories containing them.
    """
    paths = expand_files(files)
    drifts: list[str] = []
    with AcumaticaClient(inst) as client:
        for path in paths:
            baseline = seed.load_baseline(path)
            drifts += seed.diff(client, baseline)
    _exit_on_drift(inst, drifts, len(paths))


def _exit_on_drift(inst: Instance, drifts: list[str], files: int) -> None:
    """Report drift lines and exit 2 (the load-bearing diff contract, V9)."""
    if drifts:
        output.error(f"DRIFT on {inst.host}/{inst.tenant}:")
        for line in drifts:
            output.data(f"  {line}")
        raise SystemExit(2)
    output.success(f"no drift on {inst.host}/{inst.tenant} ({files} file(s))")
