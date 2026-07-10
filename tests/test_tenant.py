"""TenantManager: the exact ac.exe/sqlcmd command lines sent over SSH.

subprocess.run is monkeypatched — these tests pin the verified landmines
(docs/ac-exe.md): -iname AND -h, the Deleted sub-key with the full spec,
the -aup admin preset, and $LASTEXITCODE propagation.
"""

import subprocess
from typing import Any

import pytest

from acumatica_cli.config import Instance
from acumatica_cli.tenant import Tenant, TenantManager

SQLCMD_ROWS = """\
1|System|  |System
2|Company|Company|Custom
junk line without pipes
Rows affected: 2
"""


class FakeRun:
    """Queue of (returncode, stdout) results; records each ssh command."""

    def __init__(self, *results: tuple[int, str]):
        self.results = list(results)
        self.commands: list[str] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "ssh"
        self.commands.append(argv[-1])
        rc, stdout = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_list_parses_sqlcmd_rows_and_skips_noise(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS)]
    tenants = TenantManager(instance).list()
    assert tenants == [
        Tenant(company_id=1, company_cd="System", login_name="", company_type="System"),
        Tenant(
            company_id=2,
            company_cd="Company",
            login_name="Company",
            company_type="Custom",
        ),
    ]
    assert "AcuDB.dbo.Company" in run.commands[0]


def test_ssh_failure_raises_with_output(instance: Instance, run: FakeRun) -> None:
    run.results = [(1, "some remote noise")]
    with pytest.raises(RuntimeError, match=r"remote command failed \(1\)"):
        TenantManager(instance).list()


def test_create_builds_full_company_spec(instance: Instance, run: FakeRun) -> None:
    run.results = [(0, "Company created")]
    TenantManager(instance).create(5, "lab5.ca-dev1")

    (command,) = run.commands
    assert (
        '-company:"CompanyID=5;ParentID=1;Visible=Yes;'
        'CompanyType=;LoginName=lab5.ca-dev1;"' in command
    )
    # both are required or CompanyConfig dies mid-run (docs/ac-exe.md)
    assert '-iname:"AcumaticaERP"' in command
    assert '-h:"C:\\Acumatica\\AcumaticaERP"' in command
    # admin preset makes the tenant REST-loginable without the first-login dance
    assert '-aun:"admin" -aup:"pw" -auc:"False"' in command


def test_delete_uses_deleted_subkey_with_full_spec(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS), (0, "Company deleted")]
    TenantManager(instance).delete(2)

    delete_command = run.commands[1]
    assert (
        '-company:"CompanyID=2;ParentID=1;CompanyType=Custom;Deleted=Yes;"'
        in delete_command
    )


def test_delete_unknown_id_raises_before_ssh_config_call(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS)]
    with pytest.raises(RuntimeError, match="no tenant with CompanyID=99"):
        TenantManager(instance).delete(99)
    assert len(run.commands) == 1  # only the list() lookup ran


def test_ping_sends_trivial_readonly_command(instance: Instance, run: FakeRun) -> None:
    # the config check ssh probe: a trivial remote command through _ssh
    # (V18 choke point) that touches nothing on the instance
    run.results = [(0, "pong")]
    TenantManager(instance).ping()

    (command,) = run.commands
    assert "Write-Output pong" in command
    assert "ac.exe" not in command
    assert "sqlcmd" not in command


def test_ping_propagates_ssh_failure(instance: Instance, run: FakeRun) -> None:
    run.results = [(255, "")]
    with pytest.raises(RuntimeError, match=r"remote command failed \(255\)"):
        TenantManager(instance).ping()


def test_recycle_targets_the_instance_pool(instance: Instance, run: FakeRun) -> None:
    run.results = [(0, "")]
    TenantManager(instance).recycle_app_pool()
    assert "Restart-WebAppPool -Name 'AcumaticaERP'" in run.commands[0]


def test_every_ssh_command_propagates_exit_code(
    instance: Instance, run: FakeRun
) -> None:
    """V18: _ssh is the single choke point appending 'exit $LASTEXITCODE'.

    Every remote command — sqlcmd read (the B4 gap), app-pool recycle, and
    ac.exe CompanyConfig — must end with the suffix exactly once: a missing
    suffix means PowerShell-over-ssh swallows the failure; a doubled one
    means a call site regressed to hand-appending.
    """
    manager = TenantManager(instance)
    run.results = [(0, SQLCMD_ROWS), (0, ""), (0, "Company created"), (0, "pong")]
    manager.list()
    manager.recycle_app_pool()
    manager.create(5, "lab5.ca-dev1")
    manager.ping()

    assert len(run.commands) == 4
    for command in run.commands:
        assert command.endswith("\nexit $LASTEXITCODE")
        assert command.count("exit $LASTEXITCODE") == 1
