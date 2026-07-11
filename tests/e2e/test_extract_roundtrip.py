"""Live extract round-trip against the real instance (T50, `make e2e`).

The round-trip is extract's proof of fitness as the inverse of apply:
tenant A is configured from the data repo and dumped into a/; tenant B
is created fresh and configured from a/ alone; diff over a/ reads B
clean (V4); a second extract from B into b/ must come back byte-identical
to a/ - any server-derived field the manifest fails to strip surfaces
here as a byte difference (V22). The GL batch leg proves the replayed
setup chain is complete end to end (B16 class): a hand-built batch PUT
releases to Posted on B.

Pre-build archaeology, probed live before this file was written (V12):

- B9 is alive on this build: the plain list GET on Bootstrap Currency
  500s with the optimization marker while the $select-narrowed list GET
  answers 200 - extract's fallback is the load-bearing Currency read.
- Bootstrap Currency serves the entire currency list (~172 rows;
  IsFinancial marks the configured ones), so the extracted currencies
  file carries the whole list and the round-trip must replay it.
- Unpaged list GETs are complete at this scale: row counts matched a
  $top=10000 sweep on every probed entity (12 to 172 rows).

Opt-in tier: `e2e` marker, deselected by the default suite (V13). Run
via `make e2e` from the repo root, where the gitignored .env / baseline
/ bootstrap / setup symlinks resolve into the sibling data repo. The
tests drive the installed `acu` binary through subprocess (V9 contract
as scripts see it) and are sequential and stateful by design; the
session fixture always deletes both scratch tenants and recycles (V5).
"""

import contextlib
import difflib
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO, NamedTuple

import pytest

from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance, load_instance
from acumatica_cli.extract import FEATURES_FILE, load_manifest
from acumatica_cli.seed import load_baseline
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGIN_A = "E2EA"
LOGIN_B = "E2EB"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]


class ScratchPair(NamedTuple):
    """The two disposable tenant slots the round-trip runs against."""

    id_a: int
    id_b: int


def _pump(pipe: IO[str], lines: list[str], sink: IO[str]) -> None:
    """Copy one pipe to a live sink line by line, keeping every line."""
    for line in pipe:
        lines.append(line)
        sink.write(line)
        sink.flush()


@pytest.fixture(scope="session")
def acu() -> RunAcu:
    """Run the real acu binary from the repo root, streaming text output."""

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


def _delete_if_present(mgr: TenantManager, login: str) -> None:
    """Delete the named tenant if it exists, then recycle (V5) to forget it."""
    tenant = next((t for t in mgr.list() if t.login_name == login), None)
    if tenant is None:
        return
    mgr.delete(tenant.company_id)
    mgr.recycle_app_pool()


@pytest.fixture(scope="session")
def scratch_pair(live_instance: Instance) -> Iterator[ScratchPair]:
    """Bracket the session with two clean scratch-tenant slots.

    Setup clears leftovers from a crashed run and reserves the next two
    free CompanyIDs (A is created before B, so B's slot is free when its
    create runs); teardown always deletes both and recycles (V5).
    """
    mgr = TenantManager(live_instance)
    for login in (LOGIN_A, LOGIN_B):
        _delete_if_present(mgr, login)
    base = max(t.company_id for t in mgr.list())
    yield ScratchPair(id_a=base + 1, id_b=base + 2)
    for login in (LOGIN_A, LOGIN_B):
        _delete_if_present(mgr, login)


@pytest.fixture(scope="session")
def out_dirs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """a/ and b/: the two extract destinations the byte-compare spans."""
    root = tmp_path_factory.mktemp("roundtrip")
    return root / "a", root / "b"


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    """Stdout plus stderr - status lines (success/error) go to stderr."""
    return proc.stdout + proc.stderr


def _yaml_set(root: Path) -> set[str]:
    """Every emitted YAML file under an extract dir, repo-relative."""
    return {str(p.relative_to(root)) for p in root.rglob("*.yaml")}


def test_tenant_a_bootstraps(acu: RunAcu, scratch_pair: ScratchPair) -> None:
    """Tenant A: created from the data repo, bootstrap chained at birth."""
    proc = acu("tenant", "create", "--id", str(scratch_pair.id_a), "--login", LOGIN_A)
    assert proc.returncode == 0, _combined(proc)
    assert f"tenant {LOGIN_A} is ready" in _combined(proc)
    assert "AcuBootstrap published" in _combined(proc)


def test_apply_configures_tenant_a(acu: RunAcu, scratch_pair: ScratchPair) -> None:
    """Bare apply sweeps the data repo's bootstrap/, baseline/, setup/."""
    proc = acu("--tenant", LOGIN_A, "apply")
    assert proc.returncode == 0, _combined(proc)


def test_extract_dumps_tenant_a(
    acu: RunAcu, scratch_pair: ScratchPair, out_dirs: tuple[Path, Path]
) -> None:
    """Extract --out a/ emits the full manifest set off the configured A.

    Every manifest row must produce a file (a "(no records)" skip here
    means A's configuration is incomplete - fail loud before the
    byte-compare would); every entity/action file must parse back
    through load_baseline (I.cmd: emitted files are seed files by
    construction). Currency arriving at all is the live B9-dodge proof:
    its only working read is the $select-narrowed fallback.
    """
    dir_a, _ = out_dirs
    manifest = load_manifest()
    proc = acu("--tenant", LOGIN_A, "extract", "--out", str(dir_a))
    assert proc.returncode == 0, _combined(proc)
    assert "skip" not in _combined(proc), _combined(proc)
    expected = (
        {spec.file for spec in manifest.entities}
        | {synth.file for synth in manifest.setup}
        | {FEATURES_FILE}
    )
    assert _yaml_set(dir_a) == expected
    for rel in expected - {FEATURES_FILE}:
        load_baseline(dir_a / rel)


def test_tenant_b_bootstraps(acu: RunAcu, scratch_pair: ScratchPair) -> None:
    """Tenant B: the fresh replay target, bootstrap chained at birth."""
    proc = acu("tenant", "create", "--id", str(scratch_pair.id_b), "--login", LOGIN_B)
    assert proc.returncode == 0, _combined(proc)
    assert f"tenant {LOGIN_B} is ready" in _combined(proc)


def test_replay_extract_onto_tenant_b(
    acu: RunAcu, scratch_pair: ScratchPair, out_dirs: tuple[Path, Path]
) -> None:
    """B is configured from a/ alone, in the V22 dir order."""
    dir_a, _ = out_dirs
    proc = acu(
        "--tenant",
        LOGIN_B,
        "apply",
        str(dir_a / "bootstrap"),
        str(dir_a / "baseline"),
        str(dir_a / "setup"),
    )
    assert proc.returncode == 0, _combined(proc)


def test_diff_over_extract_is_clean_on_b(
    acu: RunAcu, scratch_pair: ScratchPair, out_dirs: tuple[Path, Path]
) -> None:
    """Independent read-back: a/ vs B shows no drift (V4, exit 0)."""
    dir_a, _ = out_dirs
    proc = acu(
        "--tenant",
        LOGIN_B,
        "diff",
        str(dir_a / "bootstrap"),
        str(dir_a / "baseline"),
        str(dir_a / "setup"),
    )
    assert proc.returncode == 0, _combined(proc)
    assert "no drift" in _combined(proc)


def test_reextract_is_byte_identical(
    acu: RunAcu, scratch_pair: ScratchPair, out_dirs: tuple[Path, Path]
) -> None:
    """Extract --out b/ off B returns a/ byte for byte (V22 leak detector).

    A field the server derives (or rewrites) that the manifest fails to
    strip survives the replay with a different value on B and surfaces
    here as a byte difference - the assertion message carries the first
    differing file's unified diff for the archaeology loop.
    """
    dir_a, dir_b = out_dirs
    proc = acu("--tenant", LOGIN_B, "extract", "--out", str(dir_b))
    assert proc.returncode == 0, _combined(proc)
    assert _yaml_set(dir_b) == _yaml_set(dir_a)
    for rel in sorted(_yaml_set(dir_a)):
        text_a = (dir_a / rel).read_text(encoding="utf-8")
        text_b = (dir_b / rel).read_text(encoding="utf-8")
        if text_a != text_b:
            delta = "".join(
                difflib.unified_diff(
                    text_a.splitlines(keepends=True),
                    text_b.splitlines(keepends=True),
                    fromfile=f"a/{rel}",
                    tofile=f"b/{rel}",
                )
            )
            pytest.fail(f"re-extract differs in {rel}:\n{delta}")


def test_gl_batch_posts_on_tenant_b(
    live_instance: Instance, scratch_pair: ScratchPair
) -> None:
    """A GL batch releases to Posted on B - the replayed chain is whole.

    The T37-verified recipe: the batch payload is hand-built (wrap() is
    scalar-only and Details is a list, so the test talks to the client's
    session directly), released via ReleaseJournalTransaction, then the
    batch is polled by key URL until Posted. The date sits inside the
    replayed open-period year; accounts and the ZERO subaccount come
    from the replayed baseline.
    """
    inst = live_instance.model_copy(update={"tenant": LOGIN_B})
    payload = {
        "Module": {"value": "GL"},
        "TransactionDate": {"value": "2026-06-15"},
        "Description": {"value": "T50 extract round-trip probe"},
        "Hold": {"value": False},
        "Details": [
            {
                "Account": {"value": "10100"},
                "Subaccount": {"value": "000000"},
                "DebitAmount": {"value": 125.0},
            },
            {
                "Account": {"value": "10200"},
                "Subaccount": {"value": "000000"},
                "CreditAmount": {"value": 125.0},
            },
        ],
    }
    with AcumaticaClient(inst) as client:
        r = client._checked(  # pyright: ignore[reportPrivateUsage]
            client._http.put(  # pyright: ignore[reportPrivateUsage]
                client._url("JournalTransaction"),  # pyright: ignore[reportPrivateUsage]
                json=payload,
            )
        )
        batch_nbr = unwrap(r.json())["BatchNbr"]
        client.invoke(
            "JournalTransaction",
            "ReleaseJournalTransaction",
            {"Module": "GL", "BatchNbr": batch_nbr},
        )
        status = None
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            record = client.get_record("JournalTransaction", ["GL", batch_nbr])
            assert record is not None, f"batch {batch_nbr} vanished after release"
            status = unwrap(record).get("Status")
            if status == "Posted":
                break
            time.sleep(2)
        assert status == "Posted", f"batch {batch_nbr} ended {status!r}, not Posted"
