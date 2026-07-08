"""acu — Acumatica configuration as code.

acu tenant list|create|delete    tenant CRUD (ac.exe over SSH)
acu apply [--dry-run] FILES...   seed baseline YAML via the REST API
acu diff FILES...                drift check: baseline vs live tenant
acu schema [-o DIR]              dump the endpoint's OpenAPI schema
"""

import os
from pathlib import Path

import click
import httpx

from . import firstlogin, output, seed
from .client import AcumaticaClient
from .config import Instance, data_root, load_instance
from .tenant import TenantManager


@click.group(help=__doc__)
@click.version_option(package_name="acumatica-cli")
@click.option(
    "-i",
    "--instance",
    default=None,
    help="Target from acu.toml [instances.<name>] (default: its default_instance)",
)
@click.option(
    "-t", "--tenant", default=None, help="Override the tenant API sessions sign in to"
)
@click.pass_context
def cli(ctx: click.Context, instance: str | None, tenant: str | None) -> None:
    """Resolve the target instance and stash it in the Click context."""
    inst = load_instance(instance)
    if tenant is not None:
        inst = inst.model_copy(update={"tenant": tenant})
    ctx.obj = inst


def main() -> None:
    """Entry point: run the CLI, mapping expected failures to one-line errors.

    RuntimeError (SSH/ac.exe, REST, first-login) and httpx transport errors
    print `✗ message` and exit 1; ACU_DEBUG=1 re-raises for the traceback.
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
        f"Tenants on {inst.name}",
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
@click.option("--login", "login_name", required=True, help="Name on the sign-in page")
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
    with output.step(f"creating tenant {company_id} ({login_name}) on {inst.name}"):
        raw = mgr.create(company_id, login_name, parent_id, not hidden, company_type)
    output.data(raw.splitlines()[-1] if raw.strip() else "created")
    if no_init:
        output.warn("skipping init: tenant is invisible until an app-pool recycle")
        return
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


def expand_files(files: tuple[Path, ...]) -> list[Path]:
    """Expand directory arguments into their *.yaml files, sorted."""
    paths: list[Path] = []
    for path in files:
        if path.is_dir():
            found = sorted(path.glob("*.yaml"))
            if not found:
                raise SystemExit(f"{path}: no *.yaml files in directory")
            paths += found
        else:
            paths.append(path)
    return paths


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
            output.data(f"{path} -> {inst.name}/{inst.tenant} ({baseline.entity})")
            n = seed.apply(client, baseline, dry_run=dry_run)
            output.data(f"  {n} record(s){' (dry run)' if dry_run else ''}")


@cli.command("schema")
@click.option(
    "-o",
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
    build — regenerate rather than version (the file is ~3 MB).
    """
    if out_dir is None:
        out_dir = data_root() / "schemas"
    out_file = out_dir / f"swagger-{inst.endpoint.replace('/', '-')}.json"
    with (
        output.step(f"dumping OpenAPI schema from {inst.name} ({inst.endpoint})"),
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
    """Compare baseline YAML against the live tenant; exit 1 on drift.

    FILES are baseline YAML files or directories containing them.
    """
    paths = expand_files(files)
    drifts: list[str] = []
    with AcumaticaClient(inst) as client:
        for path in paths:
            baseline = seed.load_baseline(path)
            drifts += seed.diff(client, baseline)
    if drifts:
        output.error(f"DRIFT on {inst.name}/{inst.tenant}:")
        for line in drifts:
            output.data(f"  {line}")
        raise SystemExit(1)
    output.success(f"no drift on {inst.name}/{inst.tenant} ({len(paths)} file(s))")
