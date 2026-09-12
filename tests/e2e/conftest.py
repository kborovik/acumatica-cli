"""Shared live-tier machinery: scaffolded data repo, acu runner, janitor.

The tier is self-contained (T63): the session scaffolds a synthetic
single-org data repo from the packaged `acu config init` templates into
a tmp dir, and every acu command runs from that repo - the cwd walk-up
(V3) finds its .env there and the bare apply/diff default dirs resolve
to the scaffolded ``acu-config/`` SEED_DIRS (V28/V30 full seed). No repo-root
data symlinks, no dataset tenants (SalesDemo|T100|U100 stay CLI surface,
never test fixtures).

Live e2e is three pipeline files (T256): provision apply/diff, scenario
run/state, extract round-trip. Per-bug probes fold onto those tenants.
New e2e file only when the proof needs a different tenant shape.

Every e2e file drives the installed `acu` binary through subprocess (the
V9 contract as scripts see it) against the live instance, and every file
cleans up its own scratch tenants; the fixtures here are the one
spelling of that machinery. Session-scoped: the e2e tier is sequential
and stateful by design.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import IO, NamedTuple

import pytest

from acumatica_cli.config import Instance, load_instance, scaffold
from acumatica_cli.tenant import TenantManager

REPO_ROOT = Path(__file__).resolve().parents[2]

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    """One disposable tenant slot a pipeline file runs against."""

    login: str
    company_id: int


class ScratchPair(NamedTuple):
    """Two disposable tenant slots the extract round-trip runs against."""

    id_a: int
    id_b: int


def joined_output(proc: subprocess.CompletedProcess[str]) -> str:
    """Stdout plus stderr - status lines (success/error) go to stderr."""
    return proc.stdout + proc.stderr


def env_without_acu(src: Mapping[str, str] | None = None) -> dict[str, str]:
    """Process environment with ambient ACU_* stripped so a found .env wins."""
    return {k: v for k, v in (src or os.environ).items() if not k.startswith("ACU_")}


def acu_binary() -> Path:
    """This checkout's ``.venv/bin/acu``, never the global PyPI tool.

    ``sys.executable.resolve()`` follows the venv symlink into the
    Homebrew framework ``bin/``, which has no ``acu``. ``sys.prefix`` is
    the venv root under ``uv run`` / ``gmake e2e``.
    """
    path = Path(sys.prefix) / "bin" / "acu"
    if not path.is_file():
        raise FileNotFoundError(f"checkout acu not found at {path}")
    return path


def load_instance_from_dotenv(root: Path) -> Instance:
    """Resolve Instance from ``root/.env`` only — ambient ACU_* do not override.

    Live e2e must follow the repo-root (or scaffold-copied) .env as the
    deployment target. pydantic-settings prefers process env over the
    file; stripping ACU_* for the load makes the file the sole source.
    """
    saved = {k: os.environ.pop(k) for k in list(os.environ) if k.startswith("ACU_")}
    try:
        with contextlib.chdir(root):
            return load_instance()
    finally:
        os.environ.update(saved)


def bracket_tenant(
    login: str, tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    """Clear leftovers, yield the next free CompanyID, always delete on exit."""
    delete_tenant(login)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=login, company_id=company_id)
    delete_tenant(login)


def bracket_pair(
    login_a: str,
    login_b: str,
    tenant_manager: TenantManager,
    delete_tenant: DeleteTenant,
) -> Iterator[ScratchPair]:
    """Clear leftovers, reserve the next two CompanyIDs, always delete both."""
    for login in (login_a, login_b):
        delete_tenant(login)
    base = max(t.company_id for t in tenant_manager.list())
    yield ScratchPair(id_a=base + 1, id_b=base + 2)
    for login in (login_a, login_b):
        delete_tenant(login)


def _pump(pipe: IO[str], lines: list[str], sink: IO[str]) -> None:
    """Copy one pipe to a live sink line by line, keeping every line."""
    for line in pipe:
        lines.append(line)
        sink.write(line)
        sink.flush()


@pytest.fixture(scope="session")
def data_repo(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A synthetic data repo scaffolded from the packaged templates.

    The template set (I.cmd `config init`, V28 single full seed) is the
    whole company definition under ``acu-config/``: bootstrap (company, credit
    terms, features), baseline GL chart, setup action chain, master
    inventory/distribution, plus lifecycle ``acu-scenario/`` and observer
    ``acu-config/views/``. The scaffolded placeholder .env is replaced by
    the real repo-root one so the subprocess walk-up resolves the live
    instance; with no decrypted .env the placeholder is dropped instead
    and resolution rides the process environment alone (V3) -
    live_instance has already failed loud if neither supplies the address.
    """
    root = tmp_path_factory.mktemp("data-repo")
    for _ in scaffold(root):
        pass
    real_env = REPO_ROOT / ".env"
    if real_env.exists():
        shutil.copyfile(real_env, root / ".env")
    else:
        (root / ".env").unlink()
    return root


@pytest.fixture(scope="session")
def acu(data_repo: Path) -> RunAcu:
    """Run this checkout's acu binary from the scaffolded data repo.

    The sibling of ``sys.executable`` is ``.venv/bin/acu`` under
    ``uv run`` / ``gmake e2e`` — never the global PyPI ``acu``. Ambient
    ACU_* are stripped so the scaffold-copied repo-root .env is the
    deployment target (V3: process env would otherwise win). Output is
    streamed through to the terminal as it arrives (acu's own step lines
    are the progress indicator for the minutes-long create + apply)
    while still being buffered for the assertions. `make e2e` passes -s
    so pytest does not swallow the stream.
    """
    acu_bin = str(acu_binary())

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        sys.stderr.write(f"$ acu {' '.join(args)}\n")
        sys.stderr.flush()
        with subprocess.Popen(
            [acu_bin, *args],
            cwd=data_repo,
            env=env_without_acu(),
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


@pytest.fixture(scope="session")
def live_instance() -> Instance:
    """The real target from repo-root .env (ambient ACU_* ignored)."""
    try:
        return load_instance_from_dotenv(REPO_ROOT)
    except SystemExit as exc:
        pytest.exit(
            f"live config missing ({exc}) - decrypt .env.gpg at the repo root",
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
        if not any(t.login_name == login for t in tenant_manager.list()):
            return
        tenant_manager.delete(login_name=login)
        tenant_manager.recycle_app_pool()

    return _delete
