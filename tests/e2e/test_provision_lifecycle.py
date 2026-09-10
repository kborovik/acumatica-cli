"""Live tenant lifecycle against the real instance (SPEC G, `make e2e`).

The lifecycle is the SPEC G pipeline verbatim, self-contained (T63): the
conftest scaffolds a synthetic single-org company from the packaged
`acu config init` templates into a tmp data repo, then `acu tenant
create` (which chains the bootstrap publish, T45) -> `acu apply` ->
`acu diff` clean, all running from that repo. Bare apply/diff exercise
the default-dirs path (I.cmd/V30): the scaffolded ``config/`` SEED_DIRS
(bootstrap/baseline/setup/master) are preferred over root.

Folded probes after full apply (T256): Role AssignUser SQL + diff,
NumberingSequence NewSymbol insert, 26-char InventoryID after
SegmentedKey, Company DecPlQty / kit 0.012. Throwaway YAML is applied
from tmp_path so it never lands in the shared scaffold master/.

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

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from acumatica_cli import firstlogin
from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import DB_NAME, Instance
from acumatica_cli.seed import BOOTSTRAP_ENDPOINT
from acumatica_cli.tenant import TenantManager
from tests.e2e.conftest import (
    DeleteTenant,
    RunAcu,
    ScratchTenant,
    bracket_tenant,
    joined_output,
)

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2E"
INSERT_ID = "T254NS"
LONG_INVENTORY_ID = "FG-CARDIO-OMEGA-COQ10-60SG"
NUMBERING_YAML = f"""\
entity: NumberingSequence
key: NumberingID
endpoint: bootstrap
records:
- NumberingID: {INSERT_ID}
  Descr: CLI e2e throwaway numbering
  NewSymbol: '<NEW>'
  StartNbr: '000000'
  EndNbr: '999999'
  WarnNbr: '999990'
  NbrStep: 1
  StartDate: '1900-01-01'
- NumberingID: BATCH
  NewSymbol: '<NEW>'
  StartNbr: '000000'
  EndNbr: '999999'
  WarnNbr: '999990'
  NbrStep: 1
  StartDate: '1900-01-01'
"""


@pytest.fixture(scope="session")
def scratch_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    """Bracket the session with a clean scratch-tenant slot."""
    yield from bracket_tenant(SCRATCH_LOGIN, tenant_manager, delete_tenant)


def _users_in_roles(
    tenant_manager: TenantManager, company_id: int, username: str
) -> str:
    """Sqlcmd dump of UsersInRoles for one user in the session tenant."""
    query = (
        "SET NOCOUNT ON; "
        "SELECT Username, Rolename, CompanyID "
        f"FROM {DB_NAME}.dbo.UsersInRoles "
        f"WHERE CompanyID = {company_id} AND Username = '{username}'"
    )
    return tenant_manager._ssh(  # pyright: ignore[reportPrivateUsage]
        f'sqlcmd -S "(local)" -E -C -W -h -1 -s "|" -Q "{query}"'
    )


def _has_role(dump: str, rolename: str) -> bool:
    for line in dump.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[1] == rolename and parts[2].isdigit():
            return True
    return False


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
    assert proc.returncode == 0, joined_output(proc)
    assert f"tenant {scratch_tenant.login} is ready" in joined_output(proc)
    assert "AcuBootstrap published" in joined_output(proc)


def test_apply_configures_the_fresh_tenant(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """Bare apply sweeps default config/ SEED_DIRS (T44/V30)."""
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, joined_output(proc)
    assert "config/" in proc.stdout or "Warehouse" in joined_output(proc)


def test_diff_is_clean_on_configured_tenant(
    acu: RunAcu, scratch_tenant: ScratchTenant
) -> None:
    """Independent read-back over everything applied (SPEC G byte-identical).

    A clean setup/ diff is the live proof of the whole GL setup chain: each
    action file's done_when probe answers non-empty, so the FinYearSetup
    row and the 2026 company periods exist on the tenant.
    """
    proc = acu("--tenant", scratch_tenant.login, "diff")
    assert proc.returncode == 0, joined_output(proc)
    assert "no drift" in joined_output(proc)


def test_apply_is_idempotent(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
    proc = acu("--tenant", scratch_tenant.login, "apply")
    assert proc.returncode == 0, joined_output(proc)
    # every setup/ action re-verifies through its done_when probe and
    # skips - the T36 re-run leg: no second invoke, zero mutations
    assert "skip GeneratePeriods (already done)" in joined_output(proc)
    assert "skip GenerateCalendar (already done)" in joined_output(proc)


def test_diff_detects_injected_drift(
    acu: RunAcu, scratch_tenant: ScratchTenant, data_repo: Path, tmp_path: Path
) -> None:
    source = sorted((data_repo / "config" / "baseline").glob("*.yaml"))[0]
    doc: dict[str, Any] = yaml.safe_load(source.read_text())
    keys = doc["key"] if isinstance(doc["key"], list) else [doc["key"]]
    record: dict[str, Any] = doc["records"][0]
    field = next(f for f in record if f not in keys)
    record[field] = "e2e drift probe"
    mutated = tmp_path / source.name
    mutated.write_text(yaml.safe_dump(doc, sort_keys=False))

    proc = acu("--tenant", scratch_tenant.login, "diff", str(mutated))
    assert proc.returncode == 2, joined_output(proc)
    assert "DRIFT" in joined_output(proc)


def test_kit_componentqty_round_trips_after_decplqty(
    live_instance: Instance, scratch_tenant: ScratchTenant
) -> None:
    """T223/V52/B30: live kit ComponentQty 0.012 after Company DecPlQty 3.

    Scratch revision is not in the demo seed, so config/ diff stays clean.
    WeightUOM/VolumeUOM come from the packaging Company PUT (folded T223).
    """
    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})
    firstlogin.initialize_admin_password(inst, tenant=scratch_tenant.login)
    with AcumaticaClient(inst) as client:
        company = client.get_record("Company", ["LAB5"], BOOTSTRAP_ENDPOINT)
        assert company is not None
        plain = unwrap(company)
        assert int(plain["DecPlQty"]) == 3, plain
        assert plain["WeightUOM"] == "KG", plain
        assert plain["VolumeUOM"] == "LITER", plain
        client.put(
            "KitSpecification",
            {
                "KitInventoryID": "GW-EDGE",
                "RevisionID": "E2E",
                "Description": "qty precision probe",
                "Active": True,
                "StockComponents": [
                    {
                        "StockInventoryID": "ENCL-STD",
                        "ComponentQty": 0.012,
                        "UOM": "EA",
                    }
                ],
            },
        )
        kit = client.get_record(
            "KitSpecification",
            ["GW-EDGE", "E2E"],
            params={"$expand": "StockComponents"},
        )
        assert kit is not None
        qty = unwrap(kit)["StockComponents"][0]["ComponentQty"]
        assert float(qty) == 0.012
        assert float(qty) != 0.01


def test_role_users_sql_and_diff_clean(
    acu: RunAcu,
    tenant_manager: TenantManager,
    scratch_tenant: ScratchTenant,
) -> None:
    """T249/V57/B37: after 92-role-users AssignUser, SQL + acu diff exit 0."""
    dump = _users_in_roles(tenant_manager, scratch_tenant.company_id, "soadmin")
    assert _has_role(dump, "SO Admin"), dump

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "diff",
        "config/master/90-roles.yaml",
        "config/master/91-users.yaml",
        "config/master/92-role-users.yaml",
    )
    assert proc.returncode == 0, joined_output(proc)
    out = joined_output(proc)
    assert "Users[" not in out
    assert ".Roles[" not in out


def test_insert_newsymbol_and_reapply_batch(
    acu: RunAcu,
    live_instance: Instance,
    scratch_tenant: ScratchTenant,
    data_repo: Path,
    tmp_path: Path,
) -> None:
    """T254/V58: insert T254NS with NewSymbol; re-apply BATCH; no 422."""
    path = tmp_path / "05-e2e-numbering-newsymbol.yaml"
    path.write_text(NUMBERING_YAML)

    proc = acu("--tenant", scratch_tenant.login, "apply", str(path))
    out = joined_output(proc)
    assert proc.returncode == 0, out
    assert "422" not in out
    assert "Either New Number Symbol or Manual Numbering" not in out
    assert f"PUT NumberingSequence [{INSERT_ID}]" in out
    assert "PUT NumberingSequence [BATCH]" in out

    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})
    with AcumaticaClient(inst) as client:
        rec = client.get_record("NumberingSequence", [INSERT_ID], BOOTSTRAP_ENDPOINT)
        assert rec is not None, INSERT_ID
        plain = unwrap(rec)
        assert plain["NumberingID"] == INSERT_ID
        assert plain["Descr"] == "CLI e2e throwaway numbering"
        assert plain["StartNbr"] == "000000"
        assert plain["EndNbr"] == "999999"
        assert plain["NewSymbol"] == "<NEW>"

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        str(data_repo / "config/master/05-numbering-sequences.yaml"),
    )
    out = joined_output(proc)
    assert proc.returncode == 0, out
    assert "422" not in out
    assert "PUT NumberingSequence [BATCH]" in out

    proc = acu("--tenant", scratch_tenant.login, "apply", str(path))
    out = joined_output(proc)
    assert proc.returncode == 0, out
    assert "422" not in out
    assert f"PUT NumberingSequence [{INSERT_ID}]" in out
    assert "PUT NumberingSequence [BATCH]" in out


def test_long_inventory_id_put_after_segmented_key(
    live_instance: Instance,
    tenant_manager: TenantManager,
    scratch_tenant: ScratchTenant,
) -> None:
    """T207/V50: 26-char InventoryID PUT succeeds after SegmentedKey apply.

    Recycle so CS202000 mask cache picks up Length 30 (app-start compile).
    Last among tenant-using probes because recycle drops live sessions.
    """
    assert len(LONG_INVENTORY_ID) == 26
    inst = live_instance.model_copy(update={"tenant": scratch_tenant.login})
    tenant_manager.recycle_app_pool()
    firstlogin.initialize_admin_password(inst, tenant=scratch_tenant.login)

    with AcumaticaClient(inst) as client:
        for dim, want in (("INVENTORY", 30), ("BIZACCT", 30), ("ACCOUNT", 10)):
            rec = client.get_record("SegmentedKey", [dim], BOOTSTRAP_ENDPOINT)
            assert rec is not None, dim
            plain = unwrap(rec)
            assert int(plain["SegmentID"]) == 1, plain
            assert int(plain["Length"]) == want, plain
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


def test_diff_against_nonexistent_tenant_exits_one(acu: RunAcu) -> None:
    """B5 regression (T21): an unknown tenant name must fail loudly, exit 1.

    The failure path depends on instance state, both verified live: on a
    multi-tenant instance with a fresh tenant map the login itself 500s; on
    a single-tenant instance (or under a stale map) the login answers 204
    and silently lands on the default tenant, and only the landed-tenant
    guard in AcumaticaClient stands between that and a false-green diff.
    """
    proc = acu("--tenant", "NoSuchTenantB5", "diff", "config/baseline")
    assert proc.returncode == 1, joined_output(proc)
