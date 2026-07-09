"""Tenant CRUD on the instance.

Thin SSH wrapper around ac.exe CompanyConfig (the only supported CLI route,
see docs/ac-exe.md) plus a sqlcmd read side.
"""

import subprocess

from .config import Instance
from .models import Model


class Tenant(Model):
    """One row of the instance's Company table.

    login_name is dbo.Company.CompanyKey — the name on the sign-in page and
    the REST login's tenant value. company_cd is the internal auto-generated
    code (ac.exe ignores LoginName for it; see docs/ac-exe.md).
    """

    company_id: int
    company_cd: str
    login_name: str
    company_type: str


class TenantManager:
    """Tenant CRUD via SSH to the Windows guest."""

    def __init__(self, instance: Instance):
        self.instance = instance

    def _ssh(self, command: str) -> str:
        # PowerShell-over-ssh returns 0 even when the native command fails;
        # appending the exit here (single choke point, never at call sites)
        # makes every remote failure propagate (SPEC V18, B4).
        r = subprocess.run(
            [
                "ssh",
                "-o",
                "BatchMode=yes",
                self.instance.ssh,
                command + "\nexit $LASTEXITCODE",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if r.returncode != 0:
            raise RuntimeError(
                f"remote command failed ({r.returncode}):\n{r.stdout}\n{r.stderr}"
            )
        return r.stdout

    def list(self) -> list[Tenant]:
        """Read tenants from the Company table (sqlcmd over SSH)."""
        query = (
            "SET NOCOUNT ON; "
            "SELECT CompanyID, CompanyCD, ISNULL(CompanyKey, ''), CompanyType "
            f"FROM {self.instance.db_name}.dbo.Company ORDER BY CompanyID"
        )
        out = self._ssh(f'sqlcmd -S "(local)" -E -C -W -h -1 -s "|" -Q "{query}"')
        tenants = []
        for line in out.splitlines():
            parts = [p.strip() for p in line.split("|")]
            if len(parts) == 4 and parts[0].isdigit():
                tenants.append(
                    Tenant(
                        company_id=int(parts[0]),
                        company_cd=parts[1],
                        login_name=parts[2],
                        company_type=parts[3],
                    )
                )
        return tenants

    def recycle_app_pool(self) -> None:
        """Recycle the IIS app pool so the running app reloads its tenant map.

        The tenant map loads at app start; without this, tenants created or
        deleted by CompanyConfig stay invisible to the sign-in page and REST
        routing silently falls back to the default tenant (docs/rest-api.md).
        The pool is named after the instance (how acumatica-infra builds it).
        """
        self._ssh(
            "Import-Module WebAdministration; "
            f"Restart-WebAppPool -Name '{self.instance.instance_name}'"
        )

    def _company_config(self, company: str, extra: str = "") -> str:
        # -iname AND -h are both required: without -iname CompanyConfig can't
        # find the site; without -h its web.config step dies on a null path
        # (verified 2026-07-08, docs/ac-exe.md).
        return self._ssh(
            f"& '{self.instance.ac_exe}' "
            f'-configmode:"CompanyConfig" -output:"Forced" '
            f'-iname:"{self.instance.instance_name}" '
            f'-h:"{self.instance.instance_path}" '
            f'-dbsrvname:"(local)" -dbsrvwinauth:"True" '
            f'-dbname:"{self.instance.db_name}" -dbnew:"False" '
            f'-company:"{company}"{extra}'
        )

    def create(
        self,
        company_id: int,
        login_name: str,
        parent_id: int = 1,
        visible: bool = True,
        company_type: str = "",
    ) -> str:
        """Create (or, for an existing CompanyID, overwrite!) a tenant.

        company_type: '' = clean tenant, 'SalesDemo' = demo data. The admin
        flags preset the tenant's admin to the instance credentials with no
        must-change flag, so the tenant is REST-loginable right after the
        app-pool recycle — no first-login dance (verified, docs/ac-exe.md).
        """
        company = (
            f"CompanyID={company_id};ParentID={parent_id};"
            f"Visible={'Yes' if visible else 'No'};"
            f"CompanyType={company_type};LoginName={login_name};"
        )
        admin = (
            f' -aun:"{self.instance.username}" '
            f'-aup:"{self.instance.password}" -auc:"False"'
        )
        return self._company_config(company, extra=admin)

    def delete(self, company_id: int) -> str:
        """Delete the tenant and all its data.

        Two verified gotchas (docs/ac-exe.md):

        - The sub-key is ``Deleted``, not the officially documented
          ``Delete`` — an unknown key is silently ignored and the run degrades
          into a *destructive data re-insert* into the tenant.
        - The full spec (``ParentID`` + ``CompanyType``) must be passed. With
          only ``CompanyID;Deleted=Yes`` the preflight reads an empty
          CompanyType as the system tenant and aborts with "The system company
          cannot be removed."
        """
        target = next((t for t in self.list() if t.company_id == company_id), None)
        if target is None:
            raise RuntimeError(f"no tenant with CompanyID={company_id}")
        return self._company_config(
            f"CompanyID={company_id};ParentID=1;"
            f"CompanyType={target.company_type};Deleted=Yes;"
        )
