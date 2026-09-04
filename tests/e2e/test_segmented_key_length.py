"""Live SegmentedKey length: 26-char StockItem PUT after apply (T207/B28).

Contract maps Length to CS202000 DataMember Detail (not Details). Does
not ride the full provision apply: package INPreferences currently 500s
on this 26r1 host (IN transit account 12400 is an IN control account).
This file applies bootstrap (SegmentedKey) plus the IN prereqs, PUTs
INPreferences with a non-control transit account, then recycles so the
CS202000 mask cache reloads before the StockItem PUT.
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

SCRATCH_LOGIN = "E2ESK"
LONG_INVENTORY_ID = "FG-CARDIO-OMEGA-COQ10-60SG"

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


def test_apply_bootstrap_widens_segmented_keys(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """SegmentedKey apply is bootstrap-only; V22 before master StockItem."""
    proc = acu("--tenant", scratch_tenant.login, "apply", "config/bootstrap")
    assert proc.returncode == 0, _combined(proc)
    assert "PUT SegmentedKey [BIZACCT]" in _combined(proc)
    assert "PUT SegmentedKey [INVENTORY]" in _combined(proc)


def test_long_inventory_id_put_after_segmented_key(
    acu: RunAcu,
    live_instance: Instance,
    tenant_manager: TenantManager,
    scratch_tenant: ScratchTenant,
    data_repo: Path,
) -> None:
    """T207/V50: 26-char InventoryID PUT succeeds after SegmentedKey apply."""
    assert len(LONG_INVENTORY_ID) == 26
    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})

    # Recycle so CS202000 mask cache picks up Length 30 (app-start compile).
    tenant_manager.recycle_app_pool()
    firstlogin.initialize_admin_password(inst, tenant=scratch_tenant.login)

    with AcumaticaClient(inst) as client:
        # Key-URL GET: list GET may omit detail Length (B9-class projection).
        for dim, want in (("INVENTORY", 30), ("BIZACCT", 30), ("ACCOUNT", 10)):
            rec = client.get_record("SegmentedKey", [dim], BOOTSTRAP_ENDPOINT)
            assert rec is not None, dim
            plain = unwrap(rec)
            assert int(plain["SegmentID"]) == 1, plain
            assert int(plain["Length"]) == want, plain

    # IN surface the package apply cannot currently close on this host:
    # 12400 is ControlAccountModule IN, so package INPreferences 500s.
    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        "config/baseline",
        "config/setup",
        str(data_repo / "config/master/05-numbering-sequences.yaml"),
        str(data_repo / "config/master/10-reason-codes.yaml"),
        str(data_repo / "config/master/30-availability-rules.yaml"),
        str(data_repo / "config/master/40-posting-classes.yaml"),
        str(data_repo / "config/master/53-tax-categories.yaml"),
    )
    assert proc.returncode == 0, _combined(proc)

    with AcumaticaClient(inst) as client:
        client.put(
            "INPreferences",
            {
                "HoldEntry": False,
                "INProgressAcctID": "15000",
                "INProgressSubID": "000000",
                "INTransitAcctID": "15000",
                "INTransitSubID": "000000",
                "TransitBranchID": "LAB5",
                "UpdateGL": True,
                "IssuesReasonCode": "INISSUE",
                "ReceiptReasonCode": "INRECEIPT",
                "AdjustmentReasonCode": "INADJUST",
                "PIReasonCode": "INPI",
                "BatchNumberingID": "BATCH",
                "ReceiptNumberingID": "INRECEIPT",
                "IssueNumberingID": "INISSUE",
                "AdjustmentNumberingID": "INADJUST",
                "KitAssemblyNumberingID": "INKITASSY",
                "AutoPost": True,
                "SummPost": False,
                "NegQty": False,
                "RequireControlTotal": True,
            },
            endpoint=BOOTSTRAP_ENDPOINT,
        )
        client.put(
            "Warehouse",
            {
                "SiteCD": "WH01",
                "Descr": "Main warehouse",
                "Active": True,
                "CountryID": "US",
            },
            endpoint=BOOTSTRAP_ENDPOINT,
        )
        client.put(
            "ItemClass",
            {
                "ClassID": "PARTS",
                "Description": "Purchased components",
                "StockItem": True,
                "ItemType": "Finished Good",
                "ValuationMethod": "Average",
                "BaseUOM": "EA",
                "AvailabilityCalculationRule": "DEFAULT",
                "PostingClass": "PARTS",
                "TaxCategoryID": "EXEMPT",
                "DefaultWarehouseID": "WH01",
            },
        )
        result = client.put(
            "StockItem",
            {
                "InventoryID": LONG_INVENTORY_ID,
                "Description": "T207 long InventoryID probe",
                "ItemClass": "PARTS",
                "ItemStatus": "Active",
                "DefaultWarehouseID": "WH01",
            },
        )
        assert str(unwrap(result)["InventoryID"]).rstrip() == LONG_INVENTORY_ID
        record = client.get_record("StockItem", [LONG_INVENTORY_ID])
        assert record is not None
        assert str(unwrap(record)["InventoryID"]).rstrip() == LONG_INVENTORY_ID
