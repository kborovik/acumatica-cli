"""Live KitAssembly StockComponents id round-trip (T240/V54/B34).

GET $expand=StockComponents keeps row id through unwrap. PUT of that id
plus LineNbr/Allocations updates the existing Components line instead of
inserting a second row (500 Components commit).
"""

import subprocess
from collections.abc import Callable, Iterator
from typing import NamedTuple

import pytest

from acumatica_cli import firstlogin
from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2EKIT"
KIT_INVENTORY_ID = "GW-EDGE"
KIT_REVISION = "V1"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    login: str
    company_id: int


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def _kit_assembly_type(instance: Instance) -> str:
    """25r1 Default 24.200.001 uses Assembly; trunk 25.200.001 uses Production."""
    return "Assembly" if instance.api_version.startswith("24.") else "Production"


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


def test_apply_configures_inventory(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, _combined(proc)
    assert "Warehouse" in _combined(proc) or "KitSpecification" in _combined(proc)


def test_buy_receipts_component_stock(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """On-hand for GW-EDGE components so KitAssembly persist is not qty-blocked."""
    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "run",
        "scenario/10-seed-capital.yaml",
        "scenario/20-buy.yaml",
    )
    assert proc.returncode == 0, _combined(proc)


def test_kitassembly_alloc_put_updates_existing_line(
    live_instance: Instance,
    scratch_tenant: ScratchTenant,
) -> None:
    """T240/V54/B34: captured StockComponents[0].id PUT updates, no second row."""
    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})
    firstlogin.initialize_admin_password(inst, tenant=scratch_tenant.login)
    kit_type = _kit_assembly_type(inst)
    with AcumaticaClient(inst) as client:
        created = unwrap(
            client.put(
                "KitAssembly",
                {
                    "Type": kit_type,
                    "KitInventoryID": KIT_INVENTORY_ID,
                    "Revision": KIT_REVISION,
                    "Qty": 1,
                    "WarehouseID": "WH01",
                    "LocationID": "MAIN",
                    "ReasonCode": "INASSEMBLY",
                    "Hold": True,
                },
            )
        )
        ref = created["ReferenceNbr"]
        rec = client.get_record(
            "KitAssembly",
            [kit_type, ref],
            params={"$expand": "StockComponents/Allocations"},
        )
        assert rec is not None
        plain = unwrap(rec)
        rows = plain["StockComponents"]
        assert rows, plain
        row0 = rows[0]
        assert "id" in row0, row0
        n_before = len(rows)
        ids_before = [row["id"] for row in rows]
        allocs = row0.get("Allocations") or [
            {
                "Qty": row0.get("Qty") or row0.get("ComponentQty") or 1,
                "LocationID": row0.get("LocationID") or "MAIN",
            }
        ]
        client.put(
            "KitAssembly",
            {
                "Type": kit_type,
                "ReferenceNbr": ref,
                "StockComponents": [
                    {
                        "id": row0["id"],
                        "LineNbr": row0["LineNbr"],
                        "Allocations": allocs,
                    }
                ],
            },
        )
        after = client.get_record(
            "KitAssembly",
            [kit_type, ref],
            params={"$expand": "StockComponents"},
        )
        assert after is not None
        after_plain = unwrap(after)
        after_rows = after_plain["StockComponents"]
        assert len(after_rows) == n_before, after_plain
        assert [row["id"] for row in after_rows] == ids_before
        assert after_rows[0]["id"] == row0["id"]
