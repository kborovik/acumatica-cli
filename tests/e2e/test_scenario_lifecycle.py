"""Live virgin-tenant scenario + snapshot lifecycle (SPEC T80/T98/T110).

Self-contained: scaffolds the packaged full ``config init`` seed into a tmp
data repo, creates a scratch tenant, then apply → run scenario/ → warm
once-skip → diff clean → snapshot write → assert-unchanged. Parallel to
``test_provision_lifecycle`` (apply/diff focus) on a separate tenant login
so the two modules do not share session tenant state.

Opt-in via ``make e2e FILE=test_scenario_lifecycle``. Default offline suite
stays green without this file (``not e2e``).
"""

import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, NamedTuple

import pytest

from acumatica_cli.config import scaffold
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_LOGIN = "E2ESCEN"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    login: str
    company_id: int


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def scenario_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Full-scaffold data repo with live credentials from repo-root .env."""
    import shutil

    root = tmp_path_factory.mktemp("scenario-data-repo")
    for _ in scaffold(root):
        pass
    real_env = REPO_ROOT / ".env"
    if real_env.exists():
        shutil.copyfile(real_env, root / ".env")
    else:
        (root / ".env").unlink(missing_ok=True)
    return root


@pytest.fixture(scope="module")
def scenario_acu(scenario_repo: Path) -> RunAcu:
    """Run installed ``acu`` from the scenario scaffold (stream like conftest)."""

    def _pump(pipe: IO[str], lines: list[str], sink: IO[str]) -> None:
        for line in pipe:
            lines.append(line)
            sink.write(line)
            sink.flush()

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        sys.stderr.write(f"$ acu {' '.join(args)}\n")
        sys.stderr.flush()
        with subprocess.Popen(
            ["acu", *args],
            cwd=scenario_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as proc:
            assert proc.stdout is not None
            assert proc.stderr is not None
            out: list[str] = []
            err: list[str] = []
            readers = [
                threading.Thread(target=_pump, args=(proc.stdout, out, sys.stdout)),
                threading.Thread(target=_pump, args=(proc.stderr, err, sys.stderr)),
            ]
            for reader in readers:
                reader.start()
            try:
                returncode = proc.wait(timeout=3600)
            except subprocess.TimeoutExpired:
                proc.kill()
                raise
            finally:
                for reader in readers:
                    reader.join()
        return subprocess.CompletedProcess(
            ["acu", *args], returncode, "".join(out), "".join(err)
        )

    return run


@pytest.fixture(scope="module")
def scenario_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    delete_tenant(SCRATCH_LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    delete_tenant(SCRATCH_LOGIN)


def test_full_scaffold_layout(scenario_repo: Path) -> None:
    """T80/T110/V28: config/ umbrella + lifecycle + TB snapshot + README."""
    assert (scenario_repo / "config" / "bootstrap" / "project.xml").is_file()
    assert (scenario_repo / "config" / "master").is_dir()
    assert (scenario_repo / "scenario" / "10-seed-capital.yaml").is_file()
    assert (scenario_repo / "scenario" / "20-buy.yaml").is_file()
    assert (scenario_repo / "scenario" / "30-build.yaml").is_file()
    assert (scenario_repo / "scenario" / "40-sell.yaml").is_file()
    assert not (scenario_repo / "scenario" / "buy-sell.yaml").exists()
    assert (scenario_repo / "config" / "snapshot" / "10-trial-balance.yaml").is_file()
    # T107/V28/V33: golden state/ = trial-balance only (B25)
    assert not (
        scenario_repo / "config" / "snapshot" / "20-inventory-summary.yaml"
    ).exists()
    assert not (scenario_repo / "snapshot").exists()
    assert (scenario_repo / "README.md").is_file()
    assert list((scenario_repo / "config" / "master").glob("*.yaml"))


def test_scenario_tenant_create(
    scenario_acu: RunAcu, scenario_tenant: ScratchTenant
) -> None:
    proc = scenario_acu(
        "tenant",
        "create",
        "--id",
        str(scenario_tenant.company_id),
        "--login",
        scenario_tenant.login,
    )
    assert proc.returncode == 0, _combined(proc)
    assert "AcuBootstrap published" in _combined(proc)


def test_scenario_apply(scenario_acu: RunAcu, scenario_tenant: ScratchTenant) -> None:
    """Bare apply prefers config/ and includes master after setup (T77/T84)."""
    proc = scenario_acu("--tenant", scenario_tenant.login, "apply")
    assert proc.returncode == 0, _combined(proc)
    assert "config/master/" in proc.stdout or "Warehouse" in _combined(proc)


def test_scenario_run(scenario_acu: RunAcu, scenario_tenant: ScratchTenant) -> None:
    proc = scenario_acu("--tenant", scenario_tenant.login, "run", "scenario/")
    assert proc.returncode == 0, _combined(proc)


def test_scenario_warm_capital_once_skip(
    scenario_acu: RunAcu, scenario_tenant: ScratchTenant
) -> None:
    """T89/V4: second run scenario/ skips once capital (Owner Capital non-stack).

    Cold path ran in test_scenario_run. Warm re-run must print the once skip
    line for 10-seed-capital and still exit 0 for additive legs.
    """
    proc = scenario_acu("--tenant", scenario_tenant.login, "run", "scenario/")
    combined = _combined(proc)
    assert proc.returncode == 0, combined
    assert "once: already present" in combined
    assert "10-seed-capital" in combined


def test_scenario_diff_clean(
    scenario_acu: RunAcu, scenario_tenant: ScratchTenant
) -> None:
    proc = scenario_acu("--tenant", scenario_tenant.login, "diff")
    assert proc.returncode == 0, _combined(proc)
    assert "no drift" in _combined(proc)


def test_scenario_snapshot_write(
    scenario_acu: RunAcu, scenario_tenant: ScratchTenant, scenario_repo: Path
) -> None:
    """T98/T102/T105/T107/V32/V33: after scenario, state/ TB is numeric fixed-point.

    Golden inquire view projects EndingBalance as fixed-point strings at
    view decimals — not a roster-only entity list. Inventory-summary is
    not golden this pass (B25 warehouse-only empty Results).
    """
    import re

    import yaml

    proc = scenario_acu("--tenant", scenario_tenant.login, "snapshot")
    assert proc.returncode == 0, _combined(proc)
    combined = _combined(proc)
    assert "trial-balance" in combined or "wrote" in combined

    tb_path = scenario_repo / "state" / "trial-balance.yaml"
    assert tb_path.is_file()
    assert not (scenario_repo / "state" / "inventory-summary.yaml").exists()

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


def test_scenario_snapshot_assert_unchanged(
    scenario_acu: RunAcu, scenario_tenant: ScratchTenant
) -> None:
    """T98/T105/T107/V4/V32: warm once-capital + snapshot --assert-unchanged exits 0.

    Depends on prior cold snapshot write. Re-run only once-guard capital
    (skip path) so EndingBalance stays byte-stable — additive buy/sell
    legs would move TB cash/inventory observations.
    """
    run = scenario_acu(
        "--tenant",
        scenario_tenant.login,
        "run",
        "scenario/10-seed-capital.yaml",
    )
    combined = _combined(run)
    assert run.returncode == 0, combined
    assert "once: already present" in combined
    proc = scenario_acu(
        "--tenant", scenario_tenant.login, "snapshot", "--assert-unchanged"
    )
    assert proc.returncode == 0, _combined(proc)
