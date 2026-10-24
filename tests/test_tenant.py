"""TenantManager: the exact ac.exe/sqlcmd command lines sent over SSH.

subprocess.run is monkeypatched — these tests pin the verified landmines:
-iname AND -h, the Deleted sub-key with the full spec, the -aup admin
preset, and $LASTEXITCODE propagation.
"""

import shutil
import subprocess
from typing import Any

import pytest

from acumatica_cli import tenant
from acumatica_cli.config import Instance
from acumatica_cli.tenant import Tenant, TenantManager

READ_PASSWORD = tenant._READ_PASSWORD  # pyright: ignore[reportPrivateUsage]

SQLCMD_ROWS = """\
1|System|  |System|
2|Company|Company|Custom|1
3|Child|Child|Custom|2
junk line without pipes
Rows affected: 2
"""


class FakeRun:
    """Queue of (returncode, stdout) results; records each ssh command."""

    def __init__(self, *results: tuple[int, str]):
        self.results = list(results)
        self.commands: list[str] = []
        self.argvs: list[list[str]] = []
        self.inputs: list[str | None] = []

    def __call__(
        self, argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        assert argv[0] == "ssh"
        self.commands.append(argv[-1])
        self.argvs.append(argv)
        self.inputs.append(kwargs.get("input"))
        rc, stdout = self.results.pop(0)
        return subprocess.CompletedProcess(argv, rc, stdout=stdout, stderr="")


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(subprocess, "run", fake)
    return fake


def test_manager_requires_ssh(run: FakeRun) -> None:
    # T67/I.cmd: tenant CRUD hard-errors naming ACU_SSH before any remote
    # when the control-plane address is unresolved (hosted data-plane path)
    inst = Instance(
        base_url="http://acu.test/AcumaticaERP",
        ssh="",
        password="secret",
    )
    with pytest.raises(SystemExit, match="ACU_SSH not set"):
        TenantManager(inst)
    assert run.commands == []


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
            parent_id=1,
        ),
        Tenant(
            company_id=3,
            company_cd="Child",
            login_name="Child",
            company_type="Custom",
            parent_id=2,
        ),
    ]
    assert "ParentCompanyID" in run.commands[0]
    # DB name = config.py constant (T42), no longer an Instance field
    assert "AcumaticaDB.dbo.Company" in run.commands[0]


def test_ssh_failure_raises_with_output(instance: Instance, run: FakeRun) -> None:
    run.results = [(1, "some remote noise")]
    with pytest.raises(RuntimeError, match=r"remote command failed \(1\)"):
        TenantManager(instance).list()


def test_create_builds_full_company_spec(instance: Instance, run: FakeRun) -> None:
    run.results = [(0, "Company created")]
    TenantManager(instance).create(5, "LAB5-DEV1")

    (command,) = run.commands
    assert (
        '-company:"CompanyID=5;ParentID=1;Visible=Yes;'
        'CompanyType=;LoginName=LAB5-DEV1;"' in command
    )
    # both are required or CompanyConfig dies mid-run
    assert '-iname:"AcumaticaERP"' in command
    assert '-h:"C:\\Acumatica\\AcumaticaERP"' in command
    # admin preset makes the tenant REST-loginable without the first-login dance;
    # the password token is read from stdin on the remote side (V62)
    assert '-aun:"admin" $aup -auc:"False"' in command
    assert "[Console]::In.ReadLine()" in command
    assert run.inputs == ["pw\n"]


def test_set_company_cd_updates_when_mismatched(
    instance: Instance, run: FakeRun
) -> None:
    # ac.exe left CompanyCD as Company3; create path aligns it to the login
    run.results = [
        (
            0,
            "1|System|  |System|\n"
            "2|Company|Company|Custom|1\n"
            "4|Company3|LAB6|Custom|1\n",
        ),
        (0, ""),
    ]
    changed = TenantManager(instance).set_company_cd(4, "LAB6")
    assert changed is True
    assert "UPDATE AcumaticaDB.dbo.Company SET CompanyCD = N'LAB6'" in run.commands[1]
    assert "WHERE CompanyID = 4" in run.commands[1]


def test_set_company_cd_noop_when_already_aligned(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, "3|LAB5|LAB5|Custom|1\n")]
    changed = TenantManager(instance).set_company_cd(3, "LAB5")
    assert changed is False
    assert len(run.commands) == 1  # list only; no UPDATE


def test_set_company_cd_rejects_collision(instance: Instance, run: FakeRun) -> None:
    run.results = [
        (
            0,
            "2|Company|Company|Custom|1\n3|LAB5|LAB5|Custom|1\n4|Company3|LAB6|Custom|1\n",
        ),
    ]
    with pytest.raises(RuntimeError, match=r"CompanyCD 'LAB5' already used"):
        TenantManager(instance).set_company_cd(4, "LAB5")
    assert len(run.commands) == 1  # list only


def test_set_company_cd_escapes_sql_quotes(instance: Instance, run: FakeRun) -> None:
    run.results = [(0, "4|Company3|O'Brien|Custom|1\n"), (0, "")]
    TenantManager(instance).set_company_cd(4, "O'Brien")
    assert "SET CompanyCD = N'O''Brien'" in run.commands[1]


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


def test_delete_by_login_name_resolves_company_id(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS), (0, "Company deleted")]
    TenantManager(instance).delete(login_name="Company")

    delete_command = run.commands[1]
    assert (
        '-company:"CompanyID=2;ParentID=1;CompanyType=Custom;Deleted=Yes;"'
        in delete_command
    )


def test_delete_sends_live_parent_id(instance: Instance, run: FakeRun) -> None:
    """A tenant made with --parent 2 is deleted with ParentID=2 (gh #62)."""
    run.results = [(0, SQLCMD_ROWS), (0, "Company deleted")]
    TenantManager(instance).delete(login_name="Child")
    assert (
        '-company:"CompanyID=3;ParentID=2;CompanyType=Custom;Deleted=Yes;"'
        in run.commands[1]
    )


def test_delete_null_parent_non_system_falls_back_to_parent_1(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, "4|Old|Old|Custom|\n"), (0, "Company deleted")]
    TenantManager(instance).delete(4)
    assert '-company:"CompanyID=4;ParentID=1;CompanyType=Custom;' in run.commands[1]


def test_list_skips_rows_without_parent_column(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, "2|Company|Company|Custom\n")]
    assert TenantManager(instance).list() == []


def test_delete_system_tenant_refused_before_config_call(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS)]
    with pytest.raises(RuntimeError, match="system tenant"):
        TenantManager(instance).delete(1)
    assert len(run.commands) == 1


def test_delete_case_only_login_mismatch_refused(
    instance: Instance, run: FakeRun
) -> None:
    """Gh #62: --login company never deletes the tenant Company."""
    run.results = [(0, SQLCMD_ROWS)]
    with pytest.raises(RuntimeError, match=r"differs only in case.*'Company'"):
        TenantManager(instance).delete(login_name="company")
    assert len(run.commands) == 1


def test_delete_unknown_id_raises_before_ssh_config_call(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS)]
    with pytest.raises(RuntimeError, match="no tenant with CompanyID=99"):
        TenantManager(instance).delete(99)
    assert len(run.commands) == 1  # only the list() lookup ran


def test_delete_unknown_login_raises_before_ssh_config_call(
    instance: Instance, run: FakeRun
) -> None:
    run.results = [(0, SQLCMD_ROWS)]
    with pytest.raises(RuntimeError, match=r"no tenant with login 'missing'"):
        TenantManager(instance).delete(login_name="missing")
    assert len(run.commands) == 1


def test_delete_requires_exactly_one_identity(instance: Instance, run: FakeRun) -> None:
    with pytest.raises(ValueError, match="exactly one of company_id or login_name"):
        TenantManager(instance).delete()
    with pytest.raises(ValueError, match="exactly one of company_id or login_name"):
        TenantManager(instance).delete(2, login_name="Company")
    assert run.commands == []


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
    manager.create(5, "LAB5-DEV1")
    manager.ping()

    assert len(run.commands) == 4
    for command in run.commands:
        assert command.endswith("\nexit $LASTEXITCODE")
        assert command.count("exit $LASTEXITCODE") == 1


BAD_IDENTITIES = ['DEV"', "DEV;x=1", "DE V", "$env:X", "DEV`", "lab.dev", ""]


@pytest.mark.parametrize("login", BAD_IDENTITIES)
def test_create_rejects_unsafe_login_before_ssh(
    instance: Instance, run: FakeRun, login: str
) -> None:
    # V62: --login lands in remote PowerShell + the ;-delimited -company spec
    with pytest.raises(SystemExit, match=r"--login .* not allowed"):
        TenantManager(instance).create(5, login)
    assert run.commands == []


def test_create_rejects_unsafe_user_before_ssh(run: FakeRun) -> None:
    # V62: ACU_USER lands in -aun:"..."; same closed charset as --login
    inst = Instance(
        base_url="http://acu.test/AcumaticaERP",
        ssh="user@acu.test",
        user='ad"min',
        password="pw",
    )
    with pytest.raises(SystemExit, match=r"ACU_USER .* not allowed"):
        TenantManager(inst).create(5, "DEV")
    assert run.commands == []


def test_delete_rejects_unsafe_login_before_ssh(
    instance: Instance, run: FakeRun
) -> None:
    with pytest.raises(SystemExit, match=r"--login .* not allowed"):
        TenantManager(instance).delete(login_name="DEV;Deleted=Yes")
    assert run.commands == []


# one value carrying every PowerShell landmine at once (gh #48 acceptance)
ALL_SPECIALS = "P$a`s\"s'w$(x)0rd"


@pytest.mark.parametrize(
    "password", ["Pa$$w0rd", "a`b", 'q"uote', "it's", "$(x)", ALL_SPECIALS]
)
def test_create_sends_password_on_stdin_only(run: FakeRun, password: str) -> None:
    # V62: byte-for-byte on stdin; never local argv (ps) nor remote cmd text
    inst = Instance(
        base_url="http://acu.test/AcumaticaERP",
        ssh="user@acu.test",
        user="admin",
        password=password,
    )
    run.results = [(0, "Company created")]
    TenantManager(inst).create(5, "DEV")

    assert run.inputs == [password + "\n"]
    (argv,) = run.argvs
    assert all(password not in arg for arg in argv)
    assert "-aup:" not in run.commands[0].split("& '")[1]


def test_non_secret_commands_send_no_stdin(instance: Instance, run: FakeRun) -> None:
    run.results = [(0, SQLCMD_ROWS), (0, "")]
    manager = TenantManager(instance)
    manager.list()
    manager.recycle_app_pool()
    assert run.inputs == [None, None]


def _msvc_token(cmd: str) -> str:
    """Decode one command-line token by MSVC/CommandLineToArgvW rules."""
    out: list[str] = []
    i, n = 0, len(cmd)
    while i < n:
        if cmd[i] == "\\":
            j = i
            while j < n and cmd[j] == "\\":
                j += 1
            k = j - i
            if j < n and cmd[j] == '"':
                out.append("\\" * (k // 2))
                if k % 2:
                    out.append('"')
                    j += 1
            else:
                out.append("\\" * k)
            i = j
        elif cmd[i] == '"':
            i += 1
        else:
            out.append(cmd[i])
            i += 1
    return "".join(out)


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="pwsh not installed")
@pytest.mark.parametrize(
    "password",
    [
        "Pa$$w0rd",
        "a`b",
        'a"b',
        "it's",
        'x\\"y',
        "sp ace\\",
        "back\\",
        "$(x)",
        ALL_SPECIALS,
    ],
)
def test_read_password_prelude_round_trips(password: str) -> None:
    # V62: the remote prelude builds a -aup token ac.exe decodes back to the
    # exact password (legacy native-arg passing wraps whitespace tokens only)
    r = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            READ_PASSWORD + "[Console]::Out.Write($aup)",
        ],
        input=password + "\n",
        capture_output=True,
        text=True,
        check=True,
    )
    token = r.stdout
    if any(ch.isspace() for ch in token):
        token = f'"{token}"'
    assert _msvc_token(token) == "-aup:" + password
