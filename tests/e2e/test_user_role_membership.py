"""Live Role.Users PUT inserts UsersInRoles (T228/V53/B31/B32).

User.Roles contract-detail PUT never writes UsersInRoles (silent 200).
Persist is SM201005 Role detail Users -> UsersByRole.Username.
SQL UsersInRoles on the session tenant CompanyID is the proof.
GET/diff of User.Roles may stay empty until the read path is confirmed.
"""

import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import NamedTuple

import pytest

from acumatica_cli.config import DB_NAME
from acumatica_cli.tenant import TenantManager

pytestmark = pytest.mark.e2e

SCRATCH_LOGIN = "E2EROLES"
IN_MANAGER_USER = "e2eroles"
IN_MANAGER_ROLE_YAML = """\
entity: Role
key: Rolename
endpoint: bootstrap
records:
- Rolename: IN Manager
  Descr: Full access to IN functions
"""
IN_MANAGER_USER_YAML = """\
entity: User
key: Username
endpoint: bootstrap
records:
- Username: e2eroles
  FirstName: E2E
  LastName: Roles
  Email: e2eroles@example.com
  IsApproved: true
  PasswordNeverExpires: true
  PasswordChangeOnNextLogin: false
"""
IN_MANAGER_MEMBERSHIP_YAML = """\
entity: Role
key: Rolename
endpoint: bootstrap
detail_keys:
  Users: Username
records:
- Rolename: IN Manager
  Users:
  - Username: e2eroles
"""

RunAcu = Callable[..., subprocess.CompletedProcess[str]]
DeleteTenant = Callable[[str], None]


class ScratchTenant(NamedTuple):
    login: str
    company_id: int


def _combined(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout + proc.stderr


def _users_in_roles(
    tenant_manager: TenantManager, company_id: int, username: str
) -> str:
    """Sqlcmd dump of UsersInRoles for one user in the session tenant."""
    query = (
        "SET NOCOUNT ON; "
        "SELECT Username, Rolename, CompanyID "
        f"FROM {DB_NAME}.dbo.UsersInRoles "
        f"WHERE CompanyID = {company_id} AND Username = '{username}'"
    )
    return tenant_manager._ssh(  # pyright: ignore[reportPrivateUsage]
        f'sqlcmd -S "(local)" -E -C -W -h -1 -s "|" -Q "{query}"'
    )


def _has_role(dump: str, rolename: str) -> bool:
    for line in dump.splitlines():
        parts = [p.strip() for p in line.split("|")]
        if len(parts) == 3 and parts[1] == rolename and parts[2].isdigit():
            return True
    return False


@pytest.fixture(scope="module")
def scratch_tenant(
    tenant_manager: TenantManager, delete_tenant: DeleteTenant
) -> Iterator[ScratchTenant]:
    delete_tenant(SCRATCH_LOGIN)
    company_id = max(t.company_id for t in tenant_manager.list()) + 1
    yield ScratchTenant(login=SCRATCH_LOGIN, company_id=company_id)
    delete_tenant(SCRATCH_LOGIN)


def test_tenant_create_bootstraps(acu: RunAcu, scratch_tenant: ScratchTenant) -> None:
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


def test_put_role_users_inserts_users_in_roles(
    acu: RunAcu,
    tenant_manager: TenantManager,
    scratch_tenant: ScratchTenant,
    data_repo: Path,
) -> None:
    """T228/V53: PUT Role Users: [{Username}] inserts UsersInRoles."""
    role_path = data_repo / "config" / "master" / "92-e2e-in-manager-role.yaml"
    user_path = data_repo / "config" / "master" / "93-e2e-in-manager-user.yaml"
    membership_path = data_repo / "config" / "master" / "94-e2e-in-manager-users.yaml"
    role_path.write_text(IN_MANAGER_ROLE_YAML)
    user_path.write_text(IN_MANAGER_USER_YAML)
    membership_path.write_text(IN_MANAGER_MEMBERSHIP_YAML)

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        str(role_path),
        str(user_path),
        str(membership_path),
    )
    assert proc.returncode == 0, _combined(proc)
    assert "PUT Role [IN Manager]" in _combined(proc)
    assert f"PUT User [{IN_MANAGER_USER}]" in _combined(proc)

    dump = _users_in_roles(tenant_manager, scratch_tenant.company_id, IN_MANAGER_USER)
    assert _has_role(dump, "IN Manager"), dump


def test_republish_apply_assigns_new_user_membership(
    acu: RunAcu,
    tenant_manager: TenantManager,
    scratch_tenant: ScratchTenant,
) -> None:
    """T228: republish + apply 90-roles, 91-users, 92-role-users assigns soadmin."""
    proc = acu("--tenant", scratch_tenant.login, "bootstrap")
    assert proc.returncode == 0, _combined(proc)
    assert "AcuBootstrap" in _combined(proc)

    proc = acu(
        "--tenant",
        scratch_tenant.login,
        "apply",
        "config/master/90-roles.yaml",
        "config/master/91-users.yaml",
        "config/master/92-role-users.yaml",
    )
    assert proc.returncode == 0, _combined(proc)
    assert "PUT User [soadmin]" in _combined(proc)
    assert "PUT Role [SO Admin]" in _combined(proc)

    dump = _users_in_roles(tenant_manager, scratch_tenant.company_id, "soadmin")
    assert _has_role(dump, "SO Admin"), dump
