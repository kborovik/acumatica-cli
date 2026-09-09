"""Live virgin-tenant scenario + state lifecycle (SPEC T80/T98/T110/T114).

Self-contained: uses the session-scaffolded packaged ``config init`` seed,
creates a scratch tenant, then apply → run scenario/ → kit-alloc probe →
warm once-skip → diff clean → state write → assert-unchanged. Parallel to
``test_provision_lifecycle`` (apply/diff focus) on a separate tenant login
so the two modules do not share session tenant state.

KitAssembly alloc (T240) folds here: leftover component stock after
20-buy / 30-build is enough for a Hold Qty-1 assembly; Hold does not
move TB so later state asserts stay valid.

Opt-in via ``make e2e FILE=test_scenario_lifecycle``. Default offline suite
stays green without this file (``not e2e``).
"""

from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
import yaml

from acumatica_cli import firstlogin
from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance
from acumatica_cli.run import period_mmYYYY
from acumatica_cli.tenant import TenantManager
from tests.e2e.conftest import (
    DeleteTenant,
    RunAcu,
    ScratchTenant,
    bracket_tenant,
    joined_output,
)

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2ESCEN"
KIT_INVENTORY_ID = "GW-EDGE"
KIT_REVISION = "V1"


@pytest.fixture(scope="session")
def scratch_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    yield from bracket_tenant(SCRATCH_LOGIN, tenant_manager, delete_tenant)


def _kit_assembly_type(instance: Instance) -> str:
    """25r1 Default 24.200.001 uses Assembly; trunk 25.200.001 uses Production."""
    return "Assembly" if instance.api_version.startswith("24.") else "Production"


def test_full_scaffold_layout(data_repo: Path) -> None:
    """T80/T110/T113/T179/V28: config/ umbrella + lifecycle + TB views + README."""
    # Bootstrap contract is package SoT — never scaffolded (T178/T179)
    assert not (data_repo / "config" / "bootstrap" / "project.xml").exists()
    assert (data_repo / "config" / "bootstrap" / "features.yaml").is_file()
    assert (data_repo / "config" / "master").is_dir()
    assert (data_repo / "scenario" / "10-seed-capital.yaml").is_file()
    assert (data_repo / "scenario" / "20-buy.yaml").is_file()
    assert (data_repo / "scenario" / "30-build.yaml").is_file()
    assert (data_repo / "scenario" / "40-sell.yaml").is_file()
    assert not (data_repo / "scenario" / "buy-sell.yaml").exists()
    assert (data_repo / "overlays" / "README.md").is_file()
    assert (
        data_repo / "overlays" / "default-24.200.001" / "scenario" / "30-build.yaml"
    ).is_file()
    assert (data_repo / "config" / "views" / "10-trial-balance.yaml").is_file()
    # T107/V28/V33: golden state/ = trial-balance only (B25)
    assert not (data_repo / "config" / "views" / "20-inventory-summary.yaml").exists()
    assert not (data_repo / "config" / "snapshot").exists()
    assert (data_repo / "README.md").is_file()
    assert list((data_repo / "config" / "master").glob("*.yaml"))


def test_scenario_tenant_create(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu(
        "tenant",
        "create",
        "--id",
        str(scratch_tenant.company_id),
        "--login",
        scratch_tenant.login,
    )
    assert proc.returncode == 0, joined_output(proc)
    assert "AcuBootstrap published" in joined_output(proc)


def test_scenario_apply(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    """Bare apply prefers config/ and includes master after setup (T77/T84)."""
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, joined_output(proc)
    assert "config/master/" in proc.stdout or "Warehouse" in joined_output(proc)


def test_scenario_run(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu("--tenant", scratch_tenant.login, "run", "scenario/")
    assert proc.returncode == 0, joined_output(proc)


def test_kitassembly_alloc_put_updates_existing_line(
    live_instance: Instance,
    scratch_tenant: ScratchTenant,
) -> None:
    """T240/V54/B34: captured StockComponents[0].id PUT updates, no second row.

    Runs after scenario buy/build so component on-hand exists. Hold Qty-1
    does not release, so later TB asserts stay valid.
    """
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


def test_scenario_warm_capital_once_skip(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """T89/V4: second run scenario/ skips once capital (Owner Capital non-stack).

    Cold path ran in test_scenario_run. Warm re-run must print the once skip
    line for 10-seed-capital and still exit 0 for additive legs.
    """
    proc = acu("--tenant", scratch_tenant.login, "run", "scenario/")
    combined = joined_output(proc)
    assert proc.returncode == 0, combined
    assert "once: already present" in combined
    assert "10-seed-capital" in combined


def test_scenario_diff_clean(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu("--tenant", scratch_tenant.login, "diff")
    assert proc.returncode == 0, joined_output(proc)
    assert "no drift" in joined_output(proc)


def test_scenario_state_write(
    acu: RunAcu, scratch_tenant: ScratchTenant, data_repo: Path
) -> None:
    """T98/T105/T107/T112/V32/V33: after scenario, state/ TB is numeric fixed-point.

    Golden inquire view projects EndingBalance as fixed-point strings at
    view decimals — not a roster-only entity list. Inventory-summary is
    not golden this pass (B25 warehouse-only empty Results).
    """
    import re

    # Package TB view pins Period 072026 for mock alignment (V33/V43).
    # Scenario posts to ${current_period}; rewrite the scaffold view so
    # state/ captures the month the JE actually landed.
    view_path = data_repo / "config" / "views" / "10-trial-balance.yaml"
    view = yaml.safe_load(view_path.read_text())
    view["source"]["params"]["Period"] = period_mmYYYY(date.today())
    view_path.write_text(yaml.safe_dump(view, sort_keys=False))

    proc = acu("--tenant", scratch_tenant.login, "state")
    assert proc.returncode == 0, joined_output(proc)
    combined = joined_output(proc)
    assert "trial-balance" in combined or "wrote" in combined

    tb_path = data_repo / "state" / "trial-balance.yaml"
    assert tb_path.is_file()
    assert not (data_repo / "state" / "inventory-summary.yaml").exists()

    tb = yaml.safe_load(tb_path.read_text())
    assert tb["view"] == "trial-balance"
    assert tb["rows"], "trial-balance must have rows after scenario"

    fixed = re.compile(r"^-?\d+\.\d{2}$")
    ending = [r.get("EndingBalance") for r in tb["rows"] if "EndingBalance" in r]
    assert ending, "trial-balance rows must capture EndingBalance (V33)"
    assert all(isinstance(v, str) and fixed.match(v) for v in ending), ending
    # Owner Capital funded by once-class seed-capital (V4)
    capital = next((r for r in tb["rows"] if r.get("Account") == "30000"), None)
    assert capital is not None, "account 30000 (Owner Capital) missing from TB"
    assert float(capital["EndingBalance"]) >= 50000.0


def test_scenario_state_assert_unchanged(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """T98/T105/T112/V4/V32: warm once-capital + state --assert-unchanged exits 0.

    Depends on prior cold state write. Re-run only once-guard capital
    (skip path) so EndingBalance stays byte-stable — additive buy/sell
    legs would move TB cash/inventory observations.
    """
    run = acu(
        "--tenant",
        scratch_tenant.login,
        "run",
        "scenario/10-seed-capital.yaml",
    )
    combined = joined_output(run)
    assert run.returncode == 0, combined
    assert "once: already present" in combined
    proc = acu("--tenant", scratch_tenant.login, "state", "--assert-unchanged")
    assert proc.returncode == 0, joined_output(proc)
