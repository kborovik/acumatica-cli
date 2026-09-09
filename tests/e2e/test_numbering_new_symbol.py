"""Live NumberingSequence insert with Header NewSymbol (T254/V58/B38).

Insert of a new NumberingID 422s when NewSymbol is unmapped. Re-apply of
tenant-native BATCH must still succeed (no NewSymbol mask 422 on update).
Throwaway id is CLI-test only — not QORD/QNCR (QMS product sequences).
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance
from acumatica_cli.seed import BOOTSTRAP_ENDPOINT
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2ENUM"
INSERT_ID = "T254NS"
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


def test_insert_newsymbol_and_reapply_batch(
    acu: RunAcu,
    live_instance: Instance,
    scratch_tenant: ScratchTenant,
    data_repo: Path,
) -> None:
    """T254/V58: insert T254NS with NewSymbol; re-apply BATCH; no 422."""
    path = data_repo / "config" / "master" / "05-e2e-numbering-newsymbol.yaml"
    path.write_text(NUMBERING_YAML)

    proc = acu("--tenant", scratch_tenant.login, "apply", str(path))
    out = _combined(proc)
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

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        str(data_repo / "config/master/05-numbering-sequences.yaml"),
    )
    out = _combined(proc)
    assert proc.returncode == 0, out
    assert "422" not in out
    assert "PUT NumberingSequence [BATCH]" in out

    proc = acu("--tenant", scratch_tenant.login, "apply", str(path))
    out = _combined(proc)
    assert proc.returncode == 0, out
    assert "422" not in out
    assert f"PUT NumberingSequence [{INSERT_ID}]" in out
    assert "PUT NumberingSequence [BATCH]" in out
