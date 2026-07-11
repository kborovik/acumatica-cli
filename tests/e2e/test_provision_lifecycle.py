"""Live tenant lifecycle against the real instance (SPEC G, `make e2e`).

The lifecycle is the SPEC G pipeline verbatim: `acu tenant create` (which
chains the bootstrap publish, T45) -> `acu apply` -> `acu diff` clean.

Opt-in tier: every test carries the `e2e` marker, which the default suite
deselects (`make check` stays offline, V13). Run via `make e2e` from the
repo root, where the gitignored acu.yaml / .env / baseline / bootstrap
symlinks resolve into ../acumatica-baseline.

The tests drive the installed `acu` binary through subprocess - not
CliRunner - so the exit-code and plain-text contract (V9) is exercised
exactly as a script or agent sees it. They are sequential and stateful by
design: pytest runs them in file order, and each step builds on the tenant
state the previous one proved. The session-scoped fixture below brackets
the run: it clears any leftover scratch tenant on the way in and always
deletes it on the way out, so nothing persists on the instance.
"""

import contextlib
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, Any, NamedTuple

import pytest
import yaml

from acumatica_cli.config import Instance, load_instance
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRATCH_LOGIN = "E2E"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]


class ScratchTenant(NamedTuple):
    """The disposable tenant slot the lifecycle runs against."""

    login: str
    company_id: int


def _pump(pipe: IO[str], lines: list[str], sink: IO[str]) -> None:
    """Copy one pipe to a live sink line by line, keeping every line."""
    for line in pipe:
        lines.append(line)
        sink.write(line)
        sink.flush()


@pytest.fixture(scope="session")
def acu() -> RunAcu:
    """Run the real acu binary from the repo root, capturing text output.

    Output is streamed through to the terminal as it arrives (acu's own
    step lines are the progress indicator for the minutes-long create +
    apply) while still being buffered for the assertions. `make e2e`
    passes -s so pytest does not swallow the stream.
    """

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        sys.stderr.write(f"$ acu {' '.join(args)}\n")
        sys.stderr.flush()
        with subprocess.Popen(
            ["acu", *args],
            cwd=REPO_ROOT,
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
                returncode = proc.wait(timeout=1800)
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


@pytest.fixture(scope="session")
def live_instance() -> Instance:
    """The real target, resolved exactly as every live command resolves it."""
    try:
        with contextlib.chdir(REPO_ROOT):
            return load_instance()
    except SystemExit as exc:
        pytest.exit(
            f"live config missing ({exc}) - "
            "run 'make decrypt' in ../acumatica-baseline",
            returncode=1,
        )


def _delete_if_present(mgr: TenantManager, login: str) -> None:
    """Delete the named tenant if it exists, then recycle (V5) to forget it."""
    tenant = next((t for t in mgr.list() if t.login_name == login), None)
    if tenant is None:
        return
    mgr.delete(tenant.company_id)
    mgr.recycle_app_pool()


@pytest.fixture(scope="session")
def scratch_tenant(live_instance: Instance) -> Iterator[ScratchTenant]:
    """Bracket the session with a clean scratch-tenant slot.

    Setup clears a leftover tenant from a crashed run and picks the next
    free CompanyID; teardown always deletes the tenant (TenantManager.delete
    has no confirmation prompt, unlike the CLI command).
    """
    mgr = TenantManager(live_instance)
    _delete_if_present(mgr, SCRATCH_LOGIN)
    company_id = max(t.company_id for t in mgr.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    _delete_if_present(mgr, SCRATCH_LOGIN)


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
    acu: RunAcu, scratch_tenant: ScratchTenant, tmp_path: Path
) -> None:
    source = sorted((REPO_ROOT / "baseline").glob("*.yaml"))[0]
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
