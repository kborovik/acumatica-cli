"""Live Company DecPlQty after UOMs then packaging PUT (T223/V52).

Kit milligram BOMs stay out of the LAB5 demo seed. Live kit 0.012 is
``tests/e2e/test_provision_lifecycle.py`` after full apply.
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from acumatica_cli import firstlogin
from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance
from acumatica_cli.seed import BOOTSTRAP_ENDPOINT
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2EQTY"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    login: str
    company_id: int


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def scratch_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    delete_tenant(SCRATCH_LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    delete_tenant(SCRATCH_LOGIN)


def test_tenant_create_bootstraps(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
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


def test_company_get_returns_decplqty_after_put(
    acu: RunAcu,
    live_instance: Instance,
    scratch_tenant: ScratchTenant,
    data_repo: Path,
) -> None:
    """T223/V52: key-URL GET returns DecPlQty 3 after packaging Company PUT."""
    proc = acu("--tenant", scratch_tenant.login, "apply", "config/bootstrap")
    assert proc.returncode == 0, _combined(proc)
    assert "PUT Company [LAB5]" in _combined(proc)

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        str(data_repo / "config/baseline/90-uoms.yaml"),
        str(data_repo / "config/baseline/91-company-packaging.yaml"),
    )
    assert proc.returncode == 0, _combined(proc)
    assert "PUT Company [LAB5]" in _combined(proc)

    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})
    firstlogin.initialize_admin_password(inst, tenant=scratch_tenant.login)
    with AcumaticaClient(inst) as client:
        rec = client.get_record("Company", ["LAB5"], BOOTSTRAP_ENDPOINT)
        assert rec is not None
        plain = unwrap(rec)
        assert int(plain["DecPlQty"]) == 3, plain
        assert plain["WeightUOM"] == "KG", plain
        assert plain["VolumeUOM"] == "LITER", plain
