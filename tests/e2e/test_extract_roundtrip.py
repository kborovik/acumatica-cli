"""Live extract round-trip against the real instance (T50, `make e2e`).

The round-trip is extract's proof of fitness as the inverse of apply:
tenant A is configured from the scaffolded synthetic data repo (T63:
the conftest scaffolds the packaged config-init templates into a tmp
dir - single org, no repo-root symlinks, no dataset tenants) and dumped
into a/; tenant B is created fresh and configured from a/ alone; diff
over a/ reads B clean (V4); a second extract from B into b/ must come
back byte-identical to a/ - any server-derived field the manifest fails
to strip surfaces here as a byte difference (V22). The GL batch leg
proves the replayed setup chain is complete end to end (B16 class): a
hand-built batch PUT releases to Posted on B.

Pre-build archaeology, probed live before this file was written (V12):

- B9 is alive on this build: the plain list GET on Bootstrap Currency
  500s with the optimization marker while the $select-narrowed list GET
  answers 200 - extract's fallback is the load-bearing Currency read.
- Bootstrap Currency serves the entire currency list (~172 rows;
  IsFinancial marks the configured ones), so the manifest narrows the
  read with filter: IsFinancial eq true (T52) - the extracted file
  carries the configured set and the fallback walks only its keys.
- Unpaged list GETs are complete at this scale: row counts matched a
  $top=10000 sweep on every probed entity (12 to 172 rows).

Opt-in tier: `e2e` marker, deselected by the default suite (V13). Run
via `make e2e` from the repo root; the only repo-root file involved is
the decrypted .env, copied into the scaffold by the conftest. The
tests drive the installed `acu` binary through the shared conftest
runner (V9 contract as scripts see it) and are sequential and stateful
by design; the session fixture always deletes both scratch tenants and
recycles (V5).
"""

import difflib
import subprocess
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from acumatica_cli.client import AcumaticaClient, unwrap
from acumatica_cli.config import Instance
from acumatica_cli.extract import FEATURES_FILE, load_manifest
from acumatica_cli.seed import load_baseline
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

LOGIN_A = "E2EA"
LOGIN_B = "E2EB"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchPair(NamedTuple):
    """The two disposable tenant slots the round-trip runs against."""

    id_a: int
    id_b: int


@pytest.fixture(scope="session")
def scratch_pair(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchPair]:
    """Bracket the session with two clean scratch-tenant slots.

    Setup clears leftovers from a crashed run and reserves the next two
    free CompanyIDs (A is created before B, so B's slot is free when its
    create runs); teardown always deletes both and recycles (V5).
    """
    for login in (LOGIN_A, LOGIN_B):
        delete_tenant(login)
    base = max(t.company_id for t in tenant_manager.list())
    yield ScratchPair(id_a=base + 1, id_b=base + 2)
    for login in (LOGIN_A, LOGIN_B):
        delete_tenant(login)


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
    """Bare apply sweeps the scaffolded repo's bootstrap/, baseline/, setup/."""
    proc = acu("--tenant", LOGIN_A, "apply")
    assert proc.returncode == 0, _combined(proc)


def test_extract_dumps_tenant_a(
    acu: RunAcu, scratch_pair: ScratchPair, out_dirs: tuple[Path, Path]
) -> None:
    """Extract --out a/ emits the config-init set off the configured A.

    Every in-contract manifest row must produce a file (a "(no records)"
    skip here means A's configuration is incomplete - fail loud before
    the byte-compare would); every entity/action file must parse back
    through load_baseline (I.cmd: emitted files are seed files by
    construction). Currency is on the full data-repo contract only (T69)
    - under the packaged minimal surface it skips clean as not-in-contract.
    """
    dir_a, _ = out_dirs
    manifest = load_manifest()
    proc = acu("--tenant", LOGIN_A, "extract", "--out", str(dir_a))
    assert proc.returncode == 0, _combined(proc)
    # Currency (and only Currency) is expected to skip under minimal package
    skips = [ln for ln in proc.stdout.splitlines() if ln.startswith("skip ")]
    assert len(skips) == 1, _combined(proc)
    assert "30-currencies" in skips[0]
    assert "entity not in active Bootstrap contract" in skips[0]
    expected = (
        {spec.file for spec in manifest.entities if spec.entity != "Currency"}
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
    replayed open-period year (2026, the template setup/ chain); the
    accounts (10100 Cash, 11000 Accounts Receivable) and the ZERO
    subaccount come from the replayed template baseline.
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
                "Account": {"value": "11000"},
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
