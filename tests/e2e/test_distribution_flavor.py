"""Live virgin-tenant verify for ``--flavor distribution`` (SPEC T80, issue #18).

Self-contained: scaffolds packaged distribution templates into a tmp data
repo, creates a scratch tenant, then bootstrap → apply → run scenario →
diff clean. Parallel to finance-minimal ``test_provision_lifecycle`` but
does not share its session tenant (different login, different seed set).

Opt-in via ``make e2e FILE=test_distribution_flavor``. Default offline
suite stays green without this file (``not e2e``).
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from acumatica_cli.config import scaffold
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_LOGIN = "E2EDIST"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    login: str
    company_id: int


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


@pytest.fixture(scope="module")
def dist_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Distribution-flavor data repo with live credentials from repo-root .env."""
    import shutil

    root = tmp_path_factory.mktemp("dist-data-repo")
    for _ in scaffold(root, flavor="distribution"):
        pass
    real_env = REPO_ROOT / ".env"
    if real_env.exists():
        shutil.copyfile(real_env, root / ".env")
    else:
        (root / ".env").unlink(missing_ok=True)
    return root


@pytest.fixture(scope="module")
def dist_acu(dist_repo: Path) -> RunAcu:
    """Run installed ``acu`` from the distribution scaffold (stream like conftest)."""
    import subprocess
    import sys
    import threading
    from typing import IO

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
            cwd=dist_repo,
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
def dist_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    delete_tenant(SCRATCH_LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    delete_tenant(SCRATCH_LOGIN)


def test_distribution_scaffold_layout(dist_repo: Path) -> None:
    """T80/V28: packaged flavor ships contract, master, scenario, README."""
    assert (dist_repo / "bootstrap" / "project.xml").is_file()
    assert (dist_repo / "master").is_dir()
    assert (dist_repo / "scenario" / "buy-build-sell.yaml").is_file()
    assert (dist_repo / "README.md").is_file()
    assert list((dist_repo / "master").glob("*.yaml"))


def test_distribution_tenant_create(
    dist_acu: RunAcu, dist_tenant: ScratchTenant
) -> None:
    proc = dist_acu(
        "tenant",
        "create",
        "--id",
        str(dist_tenant.company_id),
        "--login",
        dist_tenant.login,
    )
    assert proc.returncode == 0, _combined(proc)
    assert "AcuBootstrap published" in _combined(proc)


def test_distribution_apply(dist_acu: RunAcu, dist_tenant: ScratchTenant) -> None:
    """Bare apply includes master/ after setup/ (T77 SEED_DIRS)."""
    proc = dist_acu("--tenant", dist_tenant.login, "apply")
    assert proc.returncode == 0, _combined(proc)
    assert "master/" in proc.stdout or "Warehouse" in _combined(proc)


def test_distribution_scenario(dist_acu: RunAcu, dist_tenant: ScratchTenant) -> None:
    proc = dist_acu("--tenant", dist_tenant.login, "run", "scenario/")
    assert proc.returncode == 0, _combined(proc)


def test_distribution_diff_clean(dist_acu: RunAcu, dist_tenant: ScratchTenant) -> None:
    proc = dist_acu("--tenant", dist_tenant.login, "diff")
    assert proc.returncode == 0, _combined(proc)
    assert "no drift" in _combined(proc)
