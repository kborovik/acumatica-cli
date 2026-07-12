"""Live multi-org LedgerCompany extract -> diff (T58/B21, gh issue #7).

A SalesDemo-dataset tenant links several organizations to one ledger,
so the B14-era key (LedgerCD alone) under-keyed the extracted file: diff
right after extract exited 2 with permanent false drift, and apply would
have collapsed the org links to one. This file pins the fixed contract
live: extract emits the org-ledger links keyed by the
[LedgerCD, OrganizationID] pair (V25 holds by construction - the file
parses back through load_baseline, whose dup-tuple check would reject an
under-keyed emit), and diff over exactly that file reads the multi-org
tenant clean (V4). The single-org B14 regression leg lives in the
round-trip file: its diff-clean test reads LedgerCompany back on a
one-link tenant.

Opt-in tier: `e2e` marker, run via `make e2e FILE=test_multiorg_extract`.
The SalesDemo dataset insert makes tenant create the slow leg (minutes);
teardown always deletes the scratch tenant and recycles (V5).
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from acumatica_cli.seed import BaselineFile, load_baseline
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

LOGIN = "E2EMULTI"
LEDGER_FILE = "baseline/60-ledger-company.yaml"

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    """Stdout plus stderr - status lines (success/error) go to stderr."""
    return proc.stdout + proc.stderr


@pytest.fixture(scope="session")
def multiorg_tenant(
    acu: RunAcu, tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[str]:
    """A SalesDemo-dataset scratch tenant: multi-organization by content."""
    delete_tenant(LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    proc = acu(
        "tenant",
        "create",
        "--id",
        str(company_id),
        "--login",
        LOGIN,
        "--type",
        "SalesDemo",
    )
    assert proc.returncode == 0, _combined(proc)
    yield LOGIN
    delete_tenant(LOGIN)


@pytest.fixture(scope="session")
def extract_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The one extract destination both tests share."""
    return tmp_path_factory.mktemp("multiorg")


def test_extract_keys_every_org_ledger_link(
    acu: RunAcu, multiorg_tenant: str, extract_dir: Path
) -> None:
    """The emitted file's declared key identifies each org-ledger link."""
    proc = acu(
        "--tenant",
        multiorg_tenant,
        "extract",
        "--out",
        str(extract_dir),
        "--only",
        "LedgerCompany",
    )
    assert proc.returncode == 0, _combined(proc)
    parsed = load_baseline(extract_dir / LEDGER_FILE)
    assert isinstance(parsed, BaselineFile)
    assert parsed.keys == ["LedgerCD", "OrganizationID"]
    # the leg is only a leg if the dataset really is multi-org: at least
    # one ledger must link more than one organization (the B21 repro -
    # SalesDemo linked three organizations per ledger on 26.101.0225)
    ledgers = [r["LedgerCD"] for r in parsed.records]
    assert len(ledgers) > len(set(ledgers)), parsed.records


def test_diff_right_after_extract_is_clean(
    acu: RunAcu, multiorg_tenant: str, extract_dir: Path
) -> None:
    """The issue-#7 acceptance: no LedgerCompany drift behind the extract."""
    proc = acu("--tenant", multiorg_tenant, "diff", str(extract_dir / LEDGER_FILE))
    assert proc.returncode == 0, _combined(proc)
    assert "no drift" in _combined(proc)
