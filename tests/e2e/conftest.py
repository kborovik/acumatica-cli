"""Shared live-tier machinery: the acu binary runner + the tenant janitor.

Every e2e file drives the installed `acu` binary through subprocess (the
V9 contract as scripts see it) against the data-repo instance, and every
file cleans up its own scratch tenants; the fixtures here are the one
spelling of that machinery. Session-scoped: the e2e tier is sequential
and stateful by design.
"""

import contextlib
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import IO

import pytest

from acumatica_cli.config import Instance, load_instance
from acumatica_cli.tenant import TenantManager

REPO_ROOT = Path(__file__).resolve().parents[2]

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


def _pump(pipe: IO[str], lines: list[str], sink: IO[str]) -> None:
    """Copy one pipe to a live sink line by line, keeping every line."""
    for line in pipe:
        lines.append(line)
        sink.write(line)
        sink.flush()


@pytest.fixture(scope="session")
def acu() -> RunAcu:
    """Run the real acu binary from the repo root, streaming text output.

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
            "run 'make decrypt' in the sibling data repo",
            returncode=1,
        )


@pytest.fixture(scope="session")
def tenant_manager(live_instance: Instance) -> TenantManager:
    """The control-plane handle every scratch-tenant fixture shares."""
    return TenantManager(live_instance)


@pytest.fixture(scope="session")
def delete_tenant(tenant_manager: TenantManager) -> DeleteTenant:
    """Delete the named tenant if it exists, then recycle (V5) to forget it."""

    def _delete(login: str) -> None:
        tenant = next((t for t in tenant_manager.list() if t.login_name == login), None)
        if tenant is None:
            return
        tenant_manager.delete(tenant.company_id)
        tenant_manager.recycle_app_pool()

    return _delete
