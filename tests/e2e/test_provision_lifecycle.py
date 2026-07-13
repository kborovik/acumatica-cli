"""Live tenant lifecycle against the real instance (SPEC G, `make e2e`).

The lifecycle is the SPEC G pipeline verbatim, self-contained (T63): the
conftest scaffolds a synthetic single-org company from the packaged
`acu config init` templates into a tmp data repo, then `acu tenant
create` (which chains the bootstrap publish, T45) -> `acu apply` ->
`acu diff` clean, all running from that repo. Bare apply/diff exercise
the default-dirs path (I.cmd): the scaffolded bootstrap/ baseline/
setup/ are the data-repo root dirs the walk-up finds.

Opt-in tier: every test carries the `e2e` marker, which the default
suite deselects (`make check` stays offline, V13). Run via `make e2e`
from the repo root; the only repo-root file involved is the decrypted
.env (the instance address, copied into the scaffold) - no data
symlinks, no dataset tenants.

The tests drive the installed `acu` binary through subprocess - not
CliRunner - so the exit-code and plain-text contract (V9) is exercised
exactly as a script or agent sees it. They are sequential and stateful
by design: pytest runs them in file order, and each step builds on the
tenant state the previous one proved. The session-scoped fixture below
brackets the run: it clears any leftover scratch tenant on the way in
and always deletes it on the way out, so nothing persists on the
instance.
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, NamedTuple

import pytest
import yaml

from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2E"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    """The disposable tenant slot the lifecycle runs against."""

    login: str
    company_id: int


@pytest.fixture(scope="session")
def scratch_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    """Bracket the session with a clean scratch-tenant slot.

    Setup clears a leftover tenant from a crashed run and picks the next
    free CompanyID; teardown always deletes the tenant (TenantManager.delete
    has no confirmation prompt, unlike the CLI command).
    """
    delete_tenant(SCRATCH_LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    delete_tenant(SCRATCH_LOGIN)


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    """Stdout plus stderr - status lines (success/error) go to stderr."""
    return proc.stdout + proc.stderr


def test_tenant_create_bootstraps_at_birth(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """T45: create chains the bootstrap publish - tenant + bootstrap one step."""
    proc = acu(
        "tenant",
        "create",
        "--id",
        str(scratch_tenant.company_id),
        "--login",
        scratch_tenant.login,
    )
    assert proc.returncode == 0, _combined(proc)
    assert f"tenant {scratch_tenant.login} is ready" in _combined(proc)
    assert "AcuBootstrap published" in _combined(proc)


def test_apply_configures_the_fresh_tenant(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """Bare apply sweeps the default dirs (T44): bootstrap/, baseline/, setup/."""
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, _combined(proc)


def test_diff_is_clean_on_configured_tenant(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """Independent read-back over everything applied (SPEC G byte-identical).

    A clean setup/ diff is the live proof of the whole GL setup chain: each
    action file's done_when probe answers non-empty, so the FinYearSetup
    row and the 2026 company periods exist on the tenant.
    """
    proc = acu("--tenant", scratch_tenant.login, "diff")
    assert proc.returncode == 0, _combined(proc)
    assert "no drift" in _combined(proc)


def test_apply_is_idempotent(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, _combined(proc)
    # every setup/ action re-verifies through its done_when probe and
    # skips - the T36 re-run leg: no second invoke, zero mutations
    assert "skip GeneratePeriods (already done)" in _combined(proc)
    assert "skip GenerateCalendar (already done)" in _combined(proc)


def test_diff_detects_injected_drift(
    acu: RunAcu, scratch_tenant: ScratchTenant, data_repo: Path, tmp_path: Path
) -> None:
    source = sorted((data_repo / "baseline").glob("*.yaml"))[0]
    doc: dict[str, Any] = yaml.safe_load(source.read_text())
    keys = doc["key"] if isinstance(doc["key"], list) else [doc["key"]]
    record: dict[str, Any] = doc["records"][0]
    field = next(f for f in record if f not in keys)
    record[field] = "e2e drift probe"
    mutated = tmp_path / source.name
    mutated.write_text(yaml.safe_dump(doc, sort_keys=False))

    proc = acu("--tenant", scratch_tenant.login, "diff", str(mutated))
    assert proc.returncode == 2, _combined(proc)
    assert "DRIFT" in _combined(proc)


def test_diff_against_nonexistent_tenant_exits_one(acu: RunAcu) -> None:
    """B5 regression (T21): an unknown tenant name must fail loudly, exit 1.

    The failure path depends on instance state, both verified live: on a
    multi-tenant instance with a fresh tenant map the login itself 500s; on
    a single-tenant instance (or under a stale map) the login answers 204
    and silently lands on the default tenant, and only the landed-tenant
    guard in AcumaticaClient stands between that and a false-green diff.
    """
    proc = acu("--tenant", "NoSuchTenantB5", "diff", "baseline")
    assert proc.returncode == 1, _combined(proc)
