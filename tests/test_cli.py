"""CLI wiring: exit codes, stream routing, and the top-level error handler.

Pins diff's exit-2-on-drift contract (V9) — no live instance, no SSH.
"""

import itertools
import json
import os
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
import yaml
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.client import AcumaticaClient as RealClient
from acumatica_cli.client import format_failure, wrap
from acumatica_cli.config import INIT_TEMPLATES, Instance, default_out, load_instance
from acumatica_cli.tenant import Tenant, TenantManager

BASELINE = """\
entity: UnitsOfMeasure
key: UOM
records:
  - UOM: KG
    Description: Kilogram
"""


BOOTSTRAP_YAML = """\
entity: CreditTerms
endpoint: Bootstrap/1.11.0
key: TermsID
records:
  - TermsID: NET30
"""

SETUP_YAML = """\
action: GenerateCalendar
entity: MasterCalendar
endpoint: Bootstrap/1.11.0
record:
  FinancialYear: 2026
done_when:
  filter: FinancialYear eq '2026'
"""


FEATURES_YAML = "- MultiCompany\n- Multicurrency\n"


SWAGGER = b'{"openapi": "3.0.1"}'


class DummyClient:
    """Stands in for AcumaticaClient where no HTTP call should happen."""

    def __init__(self, instance: Instance, **kwargs: Any):
        self.instance = instance

    def swagger(self) -> bytes:
        return SWAGGER

    def list_endpoints(self) -> list[tuple[str, str]]:
        # T74: config check endpoints probe after REST login
        return self.entity_root()[0]

    def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
        # T92: bare-array path — no live ERP build id
        return [("Default", self.instance.api_version)], None

    def __enter__(self) -> DummyClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch, instance: Instance) -> Instance:
    """Point the CLI at the fake instance and a no-op REST client."""
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: instance)
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    return instance


def _baseline(tmp_path: Path) -> Path:
    path = tmp_path / "uoms.yaml"
    path.write_text(BASELINE)
    return path


class FakeDist:
    """Stands in for importlib.metadata's Distribution in _version tests."""

    def __init__(self, version: str, direct_url: str | None):
        self.version = version
        self._direct_url = direct_url

    def read_text(self, filename: str) -> str | None:
        assert filename == "direct_url.json"
        return self._direct_url


def test_version_marks_editable_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """PEP 610 editable metadata renders `<version>+dev (<checkout path>)`."""
    direct_url = '{"url":"file:///home/kb/checkout","dir_info":{"editable":true}}'
    monkeypatch.setattr(cli, "distribution", lambda name: FakeDist("1.2.3", direct_url))

    assert cli._version() == "1.2.3+dev (/home/kb/checkout)"  # pyright: ignore[reportPrivateUsage]


def test_version_plain_without_direct_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wheel install has no direct_url.json and renders the bare version."""
    monkeypatch.setattr(cli, "distribution", lambda name: FakeDist("1.2.3", None))

    assert cli._version() == "1.2.3"  # pyright: ignore[reportPrivateUsage]


def test_version_plain_when_not_editable(monkeypatch: pytest.MonkeyPatch) -> None:
    """direct_url.json without editable (e.g. `pip install .`) stays plain."""
    direct_url = '{"url":"file:///home/kb/checkout","dir_info":{}}'
    monkeypatch.setattr(cli, "distribution", lambda name: FakeDist("1.2.3", direct_url))

    assert cli._version() == "1.2.3"  # pyright: ignore[reportPrivateUsage]


def test_version_flag_prints_dist_version() -> None:
    """`acu --version` exits 0 and carries the installed dist version."""
    result = CliRunner().invoke(cli.cli, ["--version"])

    assert result.exit_code == 0
    assert cli._version() in result.output  # pyright: ignore[reportPrivateUsage]


def test_tenant_list_renders_table(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_list(self: TenantManager) -> list[Tenant]:
        return [
            Tenant(
                company_id=2,
                company_cd="Company",
                login_name="Company",
                company_type="Custom",
                parent_id=1,
            )
        ]

    monkeypatch.setattr(TenantManager, "list", fake_list)
    result = CliRunner().invoke(cli.cli, ["tenant", "list"])

    assert result.exit_code == 0
    # rich wraps the table title to the console width, so match the
    # fragments rather than the one-line concatenation
    assert "Tenants on" in result.output
    # V9/B27: hostname only — never scheme or site path in the title
    assert "acu.test" in result.output
    assert "http://" not in result.output
    assert "AcumaticaERP" not in result.output
    assert "Company" in result.output
    assert "Parent" in result.output


def test_tenant_delete_case_only_login_mismatch_exits_1(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # gh #62: real TenantManager.delete -> find_login refuses before ac.exe
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=3,
                company_cd="ACUCLI",
                login_name="ACUCLI",
                company_type="Custom",
                parent_id=1,
            )
        ],
    )
    config_calls: list[str] = []
    monkeypatch.setattr(
        TenantManager,
        "_company_config",
        lambda self, *a, **k: config_calls.append("config") or "",
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "delete", "--login", "acucli", "--yes"]
    )

    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "differs only in case from existing 'ACUCLI' (id 3)" in out
    assert config_calls == []


def test_tenant_recycle_invokes_app_pool(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T142/T143: site-wide recycle is control-plane only — wires
    # TenantManager.recycle_app_pool (Restart-WebAppPool under the hood);
    # --yes skips the destructive confirm (V16)
    calls: list[str] = []
    rest_opens = 0

    class TrackingClient(DummyClient):
        def __enter__(self) -> DummyClient:
            nonlocal rest_opens
            rest_opens += 1
            return self

    monkeypatch.setattr(cli, "AcumaticaClient", TrackingClient)
    monkeypatch.setattr(
        TenantManager,
        "recycle_app_pool",
        lambda self: calls.append("recycle"),
    )
    result = CliRunner().invoke(cli.cli, ["tenant", "recycle", "--yes"])

    assert result.exit_code == 0
    assert calls == ["recycle"]
    assert rest_opens == 0  # V1: no data-plane session
    assert "app pool recycled" in result.stderr


def test_tenant_recycle_aborts_without_yes(wired: Instance) -> None:
    # confirmation_option: default answer is no → exit 1, no remote
    result = CliRunner().invoke(cli.cli, ["tenant", "recycle"], input="n\n")

    assert result.exit_code == 1
    assert "Aborted" in result.output


def test_tenant_recycle_fails_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T143/I.cmd: empty ACU_SSH post-default → hard error naming key before
    # any remote (same class as tenant list)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=\nACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["tenant", "recycle", "--yes"])

    assert result.exit_code != 0
    assert "ACU_SSH not set" in result.output


def test_tenant_recycle_help_lists_yes(wired: Instance) -> None:
    result = CliRunner().invoke(cli.cli, ["tenant", "recycle", "--help"])

    assert result.exit_code == 0
    assert "--yes" in result.output
    assert "app pool" in result.output.lower() or "recycle" in result.output.lower()


def test_tenant_delete_by_id(wired: Instance, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[object] = []

    def fake_delete(
        self: TenantManager,
        company_id: int | None = None,
        *,
        login_name: str | None = None,
    ) -> str:
        calls.append(("delete", company_id, login_name))
        return "Company deleted\n"

    monkeypatch.setattr(TenantManager, "delete", fake_delete)
    monkeypatch.setattr(
        TenantManager, "recycle_app_pool", lambda self: calls.append("recycle")
    )
    result = CliRunner().invoke(cli.cli, ["tenant", "delete", "--id", "3", "--yes"])

    assert result.exit_code == 0
    assert calls == [("delete", 3, None), "recycle"]
    assert "Company deleted" in result.output
    # T169/V9: ac.exe delete + recycle both on step path (piped stderr)
    assert "deleting tenant id 3 on" in result.stderr
    assert "recycling app pool" in result.stderr


def test_tenant_delete_by_login(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    def fake_delete(
        self: TenantManager,
        company_id: int | None = None,
        *,
        login_name: str | None = None,
    ) -> str:
        calls.append(("delete", company_id, login_name))
        return "Company deleted\n"

    monkeypatch.setattr(TenantManager, "delete", fake_delete)
    monkeypatch.setattr(
        TenantManager, "recycle_app_pool", lambda self: calls.append("recycle")
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "delete", "--login", "DEV", "--yes"]
    )

    assert result.exit_code == 0
    assert calls == [("delete", None, "DEV"), "recycle"]
    # T169/V9: login-only label on step path
    assert "deleting tenant DEV on" in result.stderr
    assert "recycling app pool" in result.stderr


def test_tenant_delete_requires_id_or_login(wired: Instance) -> None:
    result = CliRunner().invoke(cli.cli, ["tenant", "delete", "--yes"])

    assert result.exit_code != 0
    assert "exactly one of --id or --login" in result.output


def test_tenant_delete_rejects_both_id_and_login(wired: Instance) -> None:
    result = CliRunner().invoke(
        cli.cli, ["tenant", "delete", "--id", "3", "--login", "DEV", "--yes"]
    )

    assert result.exit_code != 0
    assert "exactly one of --id or --login" in result.output


def test_tenant_delete_unknown_login_exits(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_delete(
        self: TenantManager,
        company_id: int | None = None,
        *,
        login_name: str | None = None,
    ) -> str:
        raise RuntimeError(f"no tenant with login {login_name!r}")

    monkeypatch.setattr(TenantManager, "delete", fake_delete)
    result = CliRunner().invoke(
        cli.cli, ["tenant", "delete", "--login", "missing", "--yes"]
    )

    assert result.exit_code != 0
    assert "no tenant with login 'missing'" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ["tenant", "create", "--login", 'DEV"'],
        ["tenant", "create", "--login", "DEV;x=1", "--no-init"],
        ["tenant", "delete", "--login", "$env:X", "--yes"],
    ],
)
def test_tenant_unsafe_login_exits_1_without_ssh(
    wired: Instance, monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    # V62: refused before any SSH; exit 1, never click's 2 (2 = drift)
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    result = CliRunner().invoke(cli.cli, argv)

    assert result.exit_code == 1
    assert "--login" in result.output
    assert "not allowed" in result.output
    assert calls == []


def test_tenant_create_unsafe_user_exits_1_without_ssh(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    bad = wired.model_copy(update={"user": "ad$min"})
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: bad)
    result = CliRunner().invoke(cli.cli, ["tenant", "create", "--login", "DEV"])

    assert result.exit_code == 1
    assert "ACU_USER" in result.output
    assert "not allowed" in result.output
    assert calls == []


@pytest.fixture
def create_env(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """A data repo + monkeypatched chain that records every create step."""
    calls: list[str] = []
    # features.yaml is package-build config: it must reach publish() as the
    # feature list (V2)
    dest = tmp_path / "acu-config" / "bootstrap"
    dest.mkdir(parents=True)
    (dest / "features.yaml").write_text(FEATURES_YAML)
    monkeypatch.setattr(cli, "find_data_root", lambda: tmp_path)
    # the exists-skip probe (T47) reads the live tenant list before every
    # create; an empty list keeps the fresh path exactly as before
    monkeypatch.setattr(TenantManager, "list", lambda self: [])
    monkeypatch.setattr(
        TenantManager,
        "create",
        lambda self, *a, **k: calls.append("create") or "Company created",
    )
    # post-create CompanyCD ← login align (ac.exe leaves Company2/Company3)
    monkeypatch.setattr(
        TenantManager,
        "set_company_cd",
        lambda self, cid, cd: calls.append(f"set_cd:{cd}") or True,
    )
    monkeypatch.setattr(
        TenantManager, "recycle_app_pool", lambda self: calls.append("recycle")
    )
    monkeypatch.setattr(
        cli.firstlogin,
        "initialize_admin_password",
        lambda inst, tenant: calls.append(f"init:{tenant}") or "already initialized",
    )
    monkeypatch.setattr(
        cli.bootstrap,
        "publish",
        lambda client, **k: (
            calls.append(
                f"publish:{client.instance.tenant}:"
                + (",".join(k["features"]) if k["features"] else "default")
            )
            or "published"
        ),
    )
    return calls


def test_tenant_create_chains_init_and_bootstrap(create_env: list[str]) -> None:
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 0
    # the ordered pipeline (T45):
    # create over SSH, recycle + login check (V5), then the bootstrap
    # publish into the NEW tenant (never the config default) followed by
    # the post-publish recycle - the publish's own restart caches the
    # feature slot before the plugin's insert commits
    assert create_env == [
        "create",
        "set_cd:Scratch",
        "recycle",
        "init:Scratch",
        "publish:Scratch:MultiCompany,Multicurrency",
        "recycle",
        "init:Scratch",
    ]
    assert "tenant Scratch is ready" in result.stderr
    assert "AcuBootstrap published" in result.stderr


def test_tenant_create_no_init_skips_bootstrap(create_env: list[str]) -> None:
    # an unrecycled tenant is invisible to REST, so --no-init must skip the
    # whole init + bootstrap chain, not just the recycle; CD align still runs
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch", "--no-init"]
    )

    assert result.exit_code == 0
    assert create_env == ["create", "set_cd:Scratch"]
    assert "skipping init" in result.stderr


def test_tenant_create_defaults_features_without_data_repo(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # no data repo (V3 lax discovery) -> publish() gets features=None and
    # falls back to the built-in six (SPEC I.data)
    monkeypatch.setattr(cli, "find_data_root", lambda: None)
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 0
    assert "publish:Scratch:default" in create_env


def test_tenant_create_recycles_even_when_already_published(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cli.bootstrap,
        "publish",
        lambda client, **k: create_env.append("publish") or "already published",
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch"]
    )

    # resume path: the recycle stays — a publish interrupted before its
    # recycle would otherwise leave the feature slot cached pre-plugin
    assert result.exit_code == 0
    assert create_env == [
        "create",
        "set_cd:Scratch",
        "recycle",
        "init:Scratch",
        "publish",
        "recycle",
        "init:Scratch",
    ]


def test_tenant_create_exists_skips_create_and_still_chains(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T47 (closes B17): the login already on the instance -> skip the ac.exe
    # create, but the init + digest-gated publish chain still runs — re-running
    # create is the republish route for existing tenants (V4: the gate is the
    # live tenant list, never a marker)
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=3,
                company_cd="Scratch",
                login_name="Scratch",
                company_type="",
            )
        ],
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 0
    assert "skip create: tenant Scratch exists (id 3)" in result.stdout
    # set_cd still runs on exists-skip so a mismatched CompanyCD converges
    assert create_env == [
        "set_cd:Scratch",
        "recycle",
        "init:Scratch",
        "publish:Scratch:MultiCompany,Multicurrency",
        "recycle",
        "init:Scratch",
    ]


def test_tenant_create_exists_id_mismatch_errors(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T47: --id must match the existing tenant's CompanyID — a mismatch is a
    # hard error naming both, and nothing (create or chain) runs
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=2,
                company_cd="Scratch",
                login_name="Scratch",
                company_type="",
            )
        ],
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 1
    assert "tenant Scratch exists with CompanyID 2, not 3" in result.output
    assert create_env == []


def test_tenant_create_case_only_login_mismatch_errors(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # gh #62: --login scratch while Scratch exists is neither a fresh create
    # (new CompanyID) nor an exists-skip (bootstrap under a login that never
    # routes) - hard error naming the real login, nothing runs
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=3,
                company_cd="Scratch",
                login_name="Scratch",
                company_type="Custom",
            )
        ],
    )
    result = CliRunner().invoke(cli.cli, ["tenant", "create", "--login", "scratch"])

    assert result.exit_code == 1
    out = " ".join(result.output.split())
    assert "differs only in case from existing 'Scratch' (id 3)" in out
    assert "pass --login Scratch" in out
    assert create_env == []


def test_tenant_create_login_only_allocates_next_free_id(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T166/T167: omit --id → next free CompanyID = max(list)+1 (ac.exe never
    # auto-picks); create + set_cd use the allocated id
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=1,
                company_cd="System",
                login_name="",
                company_type="System",
            ),
            Tenant(
                company_id=2,
                company_cd="Company",
                login_name="Company",
                company_type="Custom",
            ),
        ],
    )
    seen: list[int] = []
    monkeypatch.setattr(
        TenantManager,
        "create",
        lambda self, cid, login, parent, visible, ctype: (
            seen.append(cid) or create_env.append("create") or "Company created"
        ),
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--login", "Scratch", "--no-init"]
    )

    assert result.exit_code == 0
    assert seen == [3]  # max(1, 2) + 1
    assert create_env == ["create", "set_cd:Scratch"]


def test_tenant_create_exists_skip_without_id_adopts_live_id(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T166/T167: login exists + omit --id → adopt live CompanyID, skip ac.exe
    # create, still run set_cd + init/publish chain
    monkeypatch.setattr(
        TenantManager,
        "list",
        lambda self: [
            Tenant(
                company_id=5,
                company_cd="Scratch",
                login_name="Scratch",
                company_type="",
            )
        ],
    )
    result = CliRunner().invoke(cli.cli, ["tenant", "create", "--login", "Scratch"])

    assert result.exit_code == 0
    assert "skip create: tenant Scratch exists (id 5)" in result.stdout
    assert create_env == [
        "set_cd:Scratch",
        "recycle",
        "init:Scratch",
        "publish:Scratch:MultiCompany,Multicurrency",
        "recycle",
        "init:Scratch",
    ]


def test_tenant_create_type_rejects_unknown_dataset(create_env: list[str]) -> None:
    # T56/V9/V12: --type validates client-side against the box-verified
    # dataset set - a non-member errors naming the allowed
    # set, and nothing runs (no SSH, no chain); System stays excluded
    result = CliRunner().invoke(
        cli.cli,
        ["tenant", "create", "--id", "3", "--login", "Scratch", "--type", "System"],
    )

    assert result.exit_code != 0
    for name in ("SalesDemo", "T100", "U100"):
        assert name in result.output
    assert create_env == []


@pytest.mark.parametrize("dataset", ["SalesDemo", "T100", "U100"])
def test_tenant_create_type_passes_dataset_through(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch, dataset: str
) -> None:
    # T56/V7: a member of the verified set reaches mgr.create unchanged
    seen: list[str] = []
    monkeypatch.setattr(
        TenantManager,
        "create",
        lambda self, cid, login, parent, visible, ctype: (
            seen.append(ctype) or "created"
        ),
    )
    result = CliRunner().invoke(
        cli.cli,
        [
            *("tenant", "create", "--id", "3", "--login", "Scratch"),
            *("--type", dataset, "--no-init"),
        ],
    )

    assert result.exit_code == 0
    assert seen == [dataset]


def test_tenant_create_type_omitted_means_clean_tenant(
    create_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # T56/I.cmd: --type omitted -> ac.exe's empty CompanyType (clean tenant)
    seen: list[str] = []
    monkeypatch.setattr(
        TenantManager,
        "create",
        lambda self, cid, login, parent, visible, ctype: (
            seen.append(ctype) or "created"
        ),
    )
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch", "--no-init"]
    )

    assert result.exit_code == 0
    assert seen == [""]


def test_tenant_create_help_lists_exact_dataset_names(wired: Instance) -> None:
    # T56/V16: the help text documents the exact dataset names - the click
    # Choice metavar carries the full allowed set
    result = CliRunner().invoke(cli.cli, ["tenant", "create", "--help"])

    assert result.exit_code == 0
    assert "SalesDemo|T100|U100" in result.output


def test_extract_help_documents_config_hard_cut(wired: Instance) -> None:
    # T120/V12: extract is inverse of apply under config/ only (no root emit)
    result = CliRunner().invoke(cli.cli, ["survey", "extract", "--help"])

    assert result.exit_code == 0
    assert "inverse of apply" in result.output
    assert "acu-config/" in result.output
    assert "seed_catalog.yaml" in result.output
    assert "acu --tenant DEV survey extract" in result.output


def test_root_help_is_agent_oriented(wired: Instance) -> None:
    """Root --help carries mental model + workflow so agents need no skill."""
    result = CliRunner().invoke(cli.cli, ["--help"])

    assert result.exit_code == 0
    out = result.output
    assert "MENTAL MODEL" in out
    assert "TYPICAL WORKFLOW" in out
    assert "COMMAND MAP" in out
    assert "EXIT CODES" in out
    assert "CONFIG RESOLUTION" in out
    assert "PATHS" in out
    assert "acu-config/" in out
    assert "acu-scenario/" in out
    assert "apply acu-config" in out
    assert "REST data plane" in out
    assert "SSH control plane" in out
    # sole writer + drift contract
    assert "apply" in out
    assert "diff" in out
    # V48/V47: config check stays; no root check verb; cold rebuild is compose
    assert "config check" in out
    assert "tenant create" in out
    assert "survey extract" in out
    assert "survey inventory" in out
    assert "survey reconcile" in out
    assert "cold CI" not in out
    assert "check [--tenant" not in out
    assert "-h, --help" in out or "--help" in out


def test_apply_help_lists_examples_and_exit_codes(wired: Instance) -> None:
    result = CliRunner().invoke(cli.cli, ["apply", "--help"])

    assert result.exit_code == 0
    assert "Examples" in result.output
    assert "acu --tenant DEV apply" in result.output
    assert "sole tenant writer" in result.output.lower()


def test_root_check_command_is_gone(wired: Instance) -> None:
    # T230/V47: no acu check verb; acu config check stays
    result = CliRunner().invoke(cli.cli, ["check"])

    assert result.exit_code != 0
    assert "No such command" in result.output
    assert "check" not in cli.cli.commands
    config_help = CliRunner().invoke(cli.cli, ["config", "check", "--help"])
    assert config_help.exit_code == 0
    assert "preflight" in config_help.output.lower()


def test_survey_owns_extract_inventory_reconcile(wired: Instance) -> None:
    # T234/V15: survey noun owns extract|inventory|reconcile; L1 gone, no aliases
    for name in ("extract", "inventory", "reconcile"):
        result = CliRunner().invoke(cli.cli, [name])
        assert result.exit_code != 0
        assert "No such command" in result.output
        assert name not in cli.cli.commands
    assert "survey" in cli.cli.commands
    help_r = CliRunner().invoke(cli.cli, ["survey", "--help"])
    assert help_r.exit_code == 0
    for name in ("extract", "inventory", "reconcile"):
        assert name in help_r.output
        assert name in cli.survey_group.commands


def test_dropped_check_helpers_absent() -> None:
    # T231/V47/V17: dropped helpers stay gone; cold rebuild is compose, not a cmd
    repo = Path(__file__).resolve().parents[1]
    needles = (
        "check" + "_cmd",
        "_run_" + "lifecycle",
        "_life" + "cycle_",
        "_resolve_check" + "_tenant",
    )
    hits: list[str] = []
    for root_name in ("src", "tests"):
        for path in (repo / root_name).rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".md", ".yaml", ".yml"}:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for needle in needles:
                if needle in text:
                    hits.append(f"{path.relative_to(repo)}: {needle}")
    assert hits == []


def test_provision_cmd_is_gone(wired: Instance) -> None:
    # T45: tenant create chains the bootstrap publish itself; the separate
    # provision command must not exist
    result = CliRunner().invoke(cli.cli, ["provision"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_bootstrap_publishes_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T68/I.cmd: hosted path — blank ACU_SSH= opt-out (T124); publish is pure
    # REST; recycle is skipped with a warning (feature slot may stay stale)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=\n"
        "ACU_TENANT=Hosted\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    calls: list[str] = []
    monkeypatch.setattr(
        cli.bootstrap,
        "publish",
        lambda client, **k: (
            calls.append(f"publish:{client.instance.tenant}") or "published"
        ),
    )
    monkeypatch.setattr(
        TenantManager,
        "recycle_app_pool",
        lambda self: calls.append("recycle"),
    )

    result = CliRunner().invoke(cli.cli, ["bootstrap"])

    assert result.exit_code == 0, result.output
    assert calls == ["publish:Hosted"]
    assert "AcuBootstrap published" in result.stderr
    assert "skipped app-pool recycle" in result.stderr


def test_bootstrap_recycles_when_ssh_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T68: control plane present → post-publish recycle + wait (same as
    # tenant create's feature-slot fix)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=user@acu.test\n"
        "ACU_TENANT=DEV\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    calls: list[str] = []
    monkeypatch.setattr(
        cli.bootstrap,
        "publish",
        lambda client, **k: calls.append("publish") or "already published",
    )
    monkeypatch.setattr(
        TenantManager,
        "recycle_app_pool",
        lambda self: calls.append("recycle"),
    )
    monkeypatch.setattr(
        cli.firstlogin,
        "initialize_admin_password",
        lambda inst, tenant: calls.append(f"wait:{tenant}") or "already initialized",
    )

    result = CliRunner().invoke(cli.cli, ["bootstrap"])

    assert result.exit_code == 0, result.output
    assert calls == ["publish", "recycle", "wait:DEV"]
    assert "AcuBootstrap already published" in result.stderr


def test_bootstrap_requires_tenant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V5: session tenant must be explicit — empty ACU_TENANT is refuse
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["bootstrap"])

    assert result.exit_code != 0
    assert "tenant not set" in result.output


def test_bootstrap_export_writes_zip_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T68 --export: local-only zip write; no password, no HTTP, no SSH;
    # features.yaml from data repo is spliced the same as live publish
    (tmp_path / ".env").write_text("# data-repo sentinel for find_data_root\n")
    dest = tmp_path / "acu-config" / "bootstrap"
    dest.mkdir(parents=True)
    (dest / "features.yaml").write_text(FEATURES_YAML)
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "AcuBootstrap.zip"
    http_calls: list[str] = []
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda *a, **k: http_calls.append("client") or None,
    )

    result = CliRunner().invoke(cli.cli, ["bootstrap", "--export", str(out)])

    assert result.exit_code == 0, result.output
    assert out.is_file()
    assert out.stat().st_size > 0
    assert http_calls == []
    # rich may soft-wrap long paths across lines on a narrow console
    assert "wrote" in result.stderr
    assert out.name in result.stderr
    # content parity with publish: digest project.xml (zip container
    # bytes are non-deterministic — bootstrap.content_digest)
    expected = cli.bootstrap.package_zip(["MultiCompany", "Multicurrency"])
    assert cli.bootstrap.content_digest(out.read_bytes()) == (
        cli.bootstrap.content_digest(expected)
    )


def test_bootstrap_export_missing_parent_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "no-such-dir" / "AcuBootstrap.zip"

    result = CliRunner().invoke(cli.cli, ["bootstrap", "--export", str(missing)])

    assert result.exit_code != 0
    assert "directory does not exist" in result.output


def test_config_show_emits_env_without_password(wired: Instance) -> None:
    # I.cmd config show: resolved .env-format doc, ACU_* keys with resolved
    # values; ACU_PASSWORD never emitted in any form (V2)
    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert "ACU_BASE_URL=http://acu.test/AcumaticaERP" in result.output
    assert "ACU_SSH=user@acu.test" in result.output
    assert "ACU_TENANT=T1" in result.output
    assert "ACU_API_VERSION=25.200.001" in result.output
    assert "ACU_USER=admin" in result.output
    assert "pw" not in result.output  # the password value, in no form
    key_lines = [
        line for line in result.output.splitlines() if line.startswith("ACU_PASSWORD")
    ]
    assert key_lines == []


def test_config_show_round_trips_through_load_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd: `acu config show > .env` is a valid config - reloading it
    # resolves to the identical instance (the whole point of the .env emit),
    # the password supplied out of band (process environment)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    original = load_instance()

    result = CliRunner().invoke(cli.cli, ["config", "show"])
    assert result.exit_code == 0

    regenerated = tmp_path / "regenerated" / "deeper"
    regenerated.mkdir(parents=True)
    (regenerated / ".env").write_text(result.output)
    monkeypatch.chdir(regenerated)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    assert load_instance() == original


def test_config_show_env_reflects_acu_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd: ACU_USER is a real key of the emitted doc (the .env format
    # carries credentials-except-password by design) and round-trips
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_USER=auditor\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    original = load_instance()

    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert "ACU_USER=auditor" in result.output
    regenerated = tmp_path / "regenerated" / "deeper"
    regenerated.mkdir(parents=True)
    (regenerated / ".env").write_text(result.output)
    monkeypatch.chdir(regenerated)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    assert load_instance() == original


def test_config_show_reflects_global_flag_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T42/I.cmd: config show resolves through the same load_instance path,
    # so flag overrides surface per key while untouched keys keep .env
    # values; the password flag is never emitted in any form (V2)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=Administrator@acu.test\n"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        cli.cli,
        [
            "--url",
            "http://edge.example/AcumaticaERP",
            "--api-version",
            "24.200.001",
            "--username",
            "auditor",
            "--password",
            "flag-secret",
            "config",
            "show",
        ],
    )

    assert result.exit_code == 0
    assert "ACU_BASE_URL=http://edge.example/AcumaticaERP" in result.output
    assert "ACU_SSH=Administrator@acu.test" in result.output  # .env survives
    assert "ACU_API_VERSION=24.200.001" in result.output
    assert "ACU_USER=auditor" in result.output
    assert "flag-secret" not in result.output
    assert not [
        line for line in result.output.splitlines() if line.startswith("ACU_PASSWORD")
    ]


def test_url_flag_rejected_after_subcommand(wired: Instance) -> None:
    # V16: globals valid only before the subcommand - T42 flags included
    result = CliRunner().invoke(
        cli.cli, ["config", "show", "--url", "http://edge.example"]
    )

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_config_init_scaffolds_data_repo(tmp_path: Path) -> None:
    # I.cmd config init / V28/T108/T179: single full seed under config/ +
    # scenario/; never scaffolds project.xml (package SoT); T113 views;
    # runs where V3 discovery finds no .env (tmp_path has none up-tree)
    repo = tmp_path / "repo"
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--host", "erp.test", str(repo)]
    )

    assert result.exit_code == 0
    expected = [
        ".env",
        ".gitignore",
        "README.md",
        "acu-config/baseline/10-subaccounts.yaml",
        "acu-config/baseline/20-accounts.yaml",
        "acu-config/baseline/40-ledger.yaml",
        "acu-config/baseline/50-gl-preferences.yaml",
        "acu-config/baseline/60-ledger-company.yaml",
        "acu-config/baseline/90-uoms.yaml",
        "acu-config/baseline/91-company-packaging.yaml",
        "acu-config/bootstrap/company.yaml",
        "acu-config/bootstrap/credit-terms.yaml",
        "acu-config/bootstrap/segmented-key.yaml",
        "acu-config/bootstrap/features.yaml",
        "acu-config/setup/10-financial-year.yaml",
        "acu-config/setup/20-master-calendar.yaml",
        "acu-config/setup/30-open-periods.yaml",
        "acu-config/master/20-in-preferences.yaml",
        "acu-config/views/10-trial-balance.yaml",
        "acu-scenario/10-seed-capital.yaml",
        "acu-scenario/20-buy.yaml",
        "acu-scenario/30-build.yaml",
        "acu-scenario/40-sell.yaml",
        "acu-scenario/50-ar-documents.yaml",
        "acu-scenario/60-ap-documents.yaml",
        "acu-scenario/70-inventory-receipt.yaml",
        "acu-config/master/77-taxes.yaml",
        "acu-config/master/78-tax-zones.yaml",
        "acu-config/master/81-non-stock-items.yaml",
        "overlays/README.md",
        "overlays/default-24.200.001/README.md",
        "overlays/default-24.200.001/acu-scenario/30-build.yaml",
    ]
    for rel in expected:
        assert (repo / rel).is_file(), rel
    assert not (repo / "acu-config" / "bootstrap" / "project.xml").exists()
    assert not (repo / "bootstrap").exists()
    assert not (repo / "master").exists()
    assert not (repo / "snapshot").exists()
    writes = [ln for ln in result.output.splitlines() if ln.startswith("write ")]
    assert len(writes) == len(INIT_TEMPLATES)
    overlay_build = (
        repo / "overlays" / "default-24.200.001" / "acu-scenario" / "30-build.yaml"
    ).read_text()
    assert "Type: Assembly" in overlay_build
    assert "Type: Production" not in overlay_build
    ov_readme = (repo / "overlays" / "README.md").read_text()
    assert "matrix.yaml" not in ov_readme
    assert "api_version" in ov_readme
    seed_readme = (repo / "README.md").read_text()
    assert "matrix.yaml" not in seed_readme
    assert "--cell" not in seed_readme
    assert "ACU_API_VERSION" in seed_readme
    half_readme = (repo / "overlays" / "default-24.200.001" / "README.md").read_text()
    assert "default_api" not in half_readme
    assert "ACU_API_VERSION" in half_readme
    assert not (repo / "matrix.yaml").exists()
    assert not (repo / "target.yaml").exists()
    # --host substitutes into ACU_BASE_URL; ACU_API_VERSION is the pin;
    # ACU_SSH omitted (T124/V27/V28)
    env = (repo / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.test/AcumaticaERP" in env
    assert "ACU_API_VERSION=25.200.001" in env
    assert not any(ln.startswith("ACU_SSH=") for ln in env.splitlines())
    # T104/V28/V33: TB is EndingBalance-class inquire, not roster Account
    tb = (repo / "acu-config" / "views" / "10-trial-balance.yaml").read_text()
    assert "inquire: AccountSummaryInquiry" in tb
    assert "EndingBalance" in tb
    assert "entity: Account" not in tb
    assert "erp.example.com" not in env
    # T107/V33: inventory-summary not golden
    assert not (repo / "acu-config" / "views" / "20-inventory-summary.yaml").exists()


def test_config_init_defaults_to_cwd_with_placeholder_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config init: <dir> optional (cwd), --host optional (placeholder);
    # ACU_BASE_URL + ACU_API_VERSION in .env; ACU_SSH omitted (T124/V27/V28)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["config", "init"])

    assert result.exit_code == 0
    env = (tmp_path / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.example.com/AcumaticaERP" in env
    assert "ACU_API_VERSION=25.200.001" in env
    assert not any(ln.startswith("ACU_SSH=") for ln in env.splitlines())
    assert not (tmp_path / "matrix.yaml").exists()


def test_config_init_rerun_skips_and_never_overwrites(tmp_path: Path) -> None:
    # I.cmd config init: per-file skip-if-exists - `skip <file> (exists)`,
    # exit 0, zero mutations; next-step banner still prints after skips
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])
    (tmp_path / ".env").write_text("ACU_BASE_URL=http://hand.edited/X\n")

    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    skip_lines = [ln for ln in result.output.splitlines() if ln.startswith("skip ")]
    assert len(skip_lines) == len(INIT_TEMPLATES)
    assert all(ln.endswith(" (exists)") for ln in skip_lines)
    assert "next:" in result.output
    assert (tmp_path / ".env").read_text() == "ACU_BASE_URL=http://hand.edited/X\n"


def test_config_init_writes_no_secrets(tmp_path: Path) -> None:
    # V2: the scaffolded .env carries placeholder credentials only - never
    # real secrets - and is kept out of git by the scaffolded .gitignore
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    env = (tmp_path / ".env").read_text()
    assert "ACU_USER=admin" in env
    assert "ACU_PASSWORD=\n" in env
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".env" in gitignore
    assert "schemas/" in gitignore
    assert "inventory/" in gitignore
    assert "findings/" in gitignore


def test_config_init_scaffold_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T26/T108: empty dir -> init --host -> config show succeeds and
    # bare apply --dry-run parses full config/ seed (features.yaml skipped)
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    shown = CliRunner().invoke(cli.cli, ["config", "show"])
    assert shown.exit_code == 0
    assert "ACU_BASE_URL=http://erp.test/AcumaticaERP" in shown.output
    # T124: scaffold omits ACU_SSH; show emits derived Administrator@host
    assert "ACU_SSH=Administrator@erp.test" in shown.output

    applied = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "acu-config/"])
    assert applied.exit_code == 0, applied.output
    assert "would PUT Company" in applied.output
    assert "would PUT CreditTerms" in applied.output
    assert "would PUT Subaccount" in applied.output
    assert "would PUT Account" in applied.output
    assert "would PUT Ledger" in applied.output
    assert "would PUT GLPreferences" in applied.output
    assert "would PUT LedgerCompany" in applied.output
    assert "would PUT UnitsOfMeasure" in applied.output
    assert "would invoke GeneratePeriods" in applied.output
    assert "would invoke GenerateCalendar" in applied.output
    assert "would invoke ProcessAll" in applied.output
    assert "would PUT Warehouse" in applied.output or "Warehouse" in applied.output
    assert "acu-config/master/" in applied.output or "INPreferences" in applied.output
    # V22: numbered prefixes encode apply order within baseline before setup
    order = [
        applied.output.index("would PUT Subaccount ["),
        applied.output.index("would PUT Account ["),
        applied.output.index("would PUT Ledger ["),
        applied.output.index("would PUT GLPreferences ["),
        applied.output.index("would PUT LedgerCompany ["),
        applied.output.index("would PUT UnitsOfMeasure ["),
        applied.output.index("would invoke GeneratePeriods"),
        applied.output.index("would invoke GenerateCalendar"),
        applied.output.index("would invoke ProcessAll"),
    ]
    assert order == sorted(order)


def test_config_init_template_set_is_feature_closed(tmp_path: Path) -> None:
    # V22 feature closure (B15 + masters): SubAccount + warehouse/kit features
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    features = yaml.safe_load(
        (tmp_path / "acu-config" / "bootstrap" / "features.yaml").read_text()
    )
    for name in [
        "FinancialModule",
        "FinancialStandard",
        "DistributionModule",
        "Inventory",
        "Branch",
        "MultiCompany",
        "SubAccount",
        "Warehouse",
        "WarehouseLocation",
        "KitAssemblies",
    ]:
        assert name in features


def test_init_templates_cover_every_packaged_file() -> None:
    # V28/V68: a template file left out of INIT_TEMPLATES never scaffolds
    root = Path(cli.__file__).parent / "templates"
    packaged = {
        p.relative_to(root).as_posix()
        for p in root.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    }
    assert packaged == {resource for resource, _ in INIT_TEMPLATES}


def test_config_init_scenarios_guard_each_document(tmp_path: Path) -> None:
    # V68: migration scenarios = one once-guarded doc per ERP document
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    for name in [
        "50-ar-documents.yaml",
        "60-ap-documents.yaml",
        "70-inventory-receipt.yaml",
    ]:
        docs = list(yaml.safe_load_all((tmp_path / "acu-scenario" / name).read_text()))
        assert docs, name
        for doc in docs:
            assert doc["once"] is True, (name, doc["scenario"])
            assert "filter" in doc["present"]["get"], (name, doc["scenario"])


def test_config_init_template_set_is_reference_closed(tmp_path: Path) -> None:
    # V22/V29: org-referencing templates share company AcctCD
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    company = yaml.safe_load(
        (tmp_path / "acu-config" / "bootstrap" / "company.yaml").read_text()
    )
    acct_cd = company["records"][0]["AcctCD"]
    ledger_link = yaml.safe_load(
        (tmp_path / "acu-config" / "baseline" / "60-ledger-company.yaml").read_text()
    )
    assert ledger_link["records"][0]["OrganizationID"] == acct_cd
    open_periods = yaml.safe_load(
        (tmp_path / "acu-config" / "setup" / "30-open-periods.yaml").read_text()
    )
    assert open_periods["record"]["OrganizationID"] == acct_cd
    in_prefs = yaml.safe_load(
        (tmp_path / "acu-config" / "master" / "20-in-preferences.yaml").read_text()
    )
    cash = yaml.safe_load(
        (tmp_path / "acu-config" / "master" / "63-cash-account.yaml").read_text()
    )
    assert in_prefs["records"][0]["TransitBranchID"] == acct_cd
    assert cash["records"][0]["BranchID"] == acct_cd


def test_config_init_prints_next_step_cmds(tmp_path: Path) -> None:
    # V28/V60: next-step cmds use explicit acu-config / acu-scenario paths
    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    assert "next:" in result.output
    assert "acu bootstrap" in result.output
    assert "acu apply acu-config" in result.output
    assert "acu run acu-scenario" in result.output
    assert "acu diff acu-config" in result.output
    assert "acu state acu-config/views" in result.output
    assert "acu check" not in result.output
    assert not (tmp_path / "qms").exists()
    assert not (tmp_path / "customization").exists()
    assert not (tmp_path / "acu-config" / "qms").exists()


def test_config_init_no_flavor_option(tmp_path: Path) -> None:
    # V28/T108: --flavor removed; free/unknown flag is rejected by click
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--flavor", "distribution", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert (
        "no such option" in result.output.lower()
        or "no such option" in str(result.exception).lower()
        or "Error" in result.output
    )


def test_expand_files_root_seed_dirs_order(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T77/V30: explicit umbrella expands SEED_DIRS order bootstrap…master
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    for name in ("bootstrap", "baseline", "setup", "master"):
        d = tmp_path / name
        d.mkdir()
        (d / "x.yaml").write_text(
            "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
        )
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "diff",
        lambda client, baseline: seen.append(str(baseline.path)) or [],
    )

    result = CliRunner().invoke(cli.cli, ["diff", "."])

    assert result.exit_code == 0
    assert [s.replace("\\", "/") for s in seen] == [
        "bootstrap/x.yaml",
        "baseline/x.yaml",
        "setup/x.yaml",
        "master/x.yaml",
    ]


def test_typed_path_does_not_merge_trees(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V30/V59: typed path used as given; never merge a sibling tree
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    for name in ("bootstrap", "baseline"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "root.yaml").write_text(seed)
        (tmp_path / "config" / name).mkdir(parents=True)
        (tmp_path / "config" / name / "cfg.yaml").write_text(seed)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "diff",
        lambda client, baseline: (
            seen.append(str(baseline.path).replace("\\", "/")) or []
        ),
    )

    result = CliRunner().invoke(cli.cli, ["diff", "config/"])

    assert result.exit_code == 0, result.output
    assert seen == ["config/bootstrap/cfg.yaml", "config/baseline/cfg.yaml"]
    assert not any("root.yaml" in s for s in seen)


def test_seed_dirs_exclude_inventory_and_findings() -> None:
    """V35/T130: inventory/ + findings/ are dual-reader trees, never SEED_DIRS."""
    assert "inventory" not in cli.SEED_DIRS
    assert "findings" not in cli.SEED_DIRS
    assert cli.SEED_DIRS == ("bootstrap", "baseline", "setup", "master")


def test_expand_files_ignores_inventory_and_findings(tmp_path: Path) -> None:
    """V35/T130: umbrella expand of config/ never loads dual-reader trees."""
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    (tmp_path / "config" / "baseline").mkdir(parents=True)
    (tmp_path / "config" / "baseline" / "uoms.yaml").write_text(seed)
    inv = tmp_path / "inventory"
    (inv / "tables").mkdir(parents=True)
    (inv / "summary.yaml").write_text(
        "erp: 26.101.0225\nexport_mode: xml-zip\ntables: {Account: 1}\n",
        encoding="utf-8",
    )
    (inv / "tables" / "Account.yaml").write_text(
        "name: Account\ncolumns: []\nrows: []\n",
        encoding="utf-8",
    )
    findings = tmp_path / "findings"
    findings.mkdir()
    (findings / "summary.yaml").write_text(
        "inventory: inventory/\nfindings: {unmapped: 1}\n",
        encoding="utf-8",
    )
    (findings / "unmapped.yaml").write_text(
        "kind: unmapped\ntables: [{name: X, rows: 0}]\n",
        encoding="utf-8",
    )

    expanded = [
        str(p).replace("\\", "/") for p in cli.expand_files((tmp_path / "config",))
    ]
    assert expanded == [
        str(tmp_path / "config" / "baseline" / "uoms.yaml").replace("\\", "/")
    ]
    assert not any("inventory" in p or "findings" in p for p in expanded)


def test_apply_diff_explicit_config_ignores_inventory_findings(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V35/T130: apply/diff config/ never load inventory/ or findings/ YAML."""
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    (tmp_path / "config" / "baseline").mkdir(parents=True)
    (tmp_path / "config" / "baseline" / "uoms.yaml").write_text(seed)
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "summary.yaml").write_text(
        "erp: null\nexport_mode: xml-folder\ntables: {}\n",
        encoding="utf-8",
    )
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "summary.yaml").write_text(
        "findings: {}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)

    seen_diff: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "diff",
        lambda client, baseline: (
            seen_diff.append(str(baseline.path).replace("\\", "/")) or []
        ),
    )
    result = CliRunner().invoke(cli.cli, ["diff", "config/"])
    assert result.exit_code == 0, result.output
    assert seen_diff == ["config/baseline/uoms.yaml"]
    assert "inventory" not in result.output
    assert "findings" not in result.output

    seen_apply: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "apply",
        lambda client, baseline, *_, **__: (
            seen_apply.append(str(baseline.path).replace("\\", "/")) or (1, [])
        ),
    )
    result = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "config/"])
    assert result.exit_code == 0, result.output
    assert seen_apply == ["config/baseline/uoms.yaml"]
    assert "inventory" not in result.output
    assert "findings" not in result.output


def test_expand_files_umbrella_config_seed_order(tmp_path: Path) -> None:
    # T84/V22/V30: acu apply config/ expands nested SEED_DIRS fixed order
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    for name in ("master", "bootstrap", "setup", "baseline"):  # intentional scramble
        d = tmp_path / "config" / name
        d.mkdir(parents=True)
        (d / f"{name}.yaml").write_text(seed)
    (tmp_path / "config" / "bootstrap" / "features.yaml").write_text("- Inventory\n")

    paths = [
        str(p).replace("\\", "/") for p in cli.expand_files((tmp_path / "config",))
    ]

    assert paths == [
        str(tmp_path / "config" / "bootstrap" / "bootstrap.yaml").replace("\\", "/"),
        str(tmp_path / "config" / "baseline" / "baseline.yaml").replace("\\", "/"),
        str(tmp_path / "config" / "setup" / "setup.yaml").replace("\\", "/"),
        str(tmp_path / "config" / "master" / "master.yaml").replace("\\", "/"),
    ]
    assert not any(p.endswith("features.yaml") for p in paths)


def test_expand_files_leaf_dir_unchanged(tmp_path: Path) -> None:
    # leaf seed dir still expands its own *.yaml only (no SEED_DIRS children)
    d = tmp_path / "baseline"
    d.mkdir()
    (d / "b.yaml").write_text("entity: X\nkey: K\nrecords: []\n")
    (d / "a.yaml").write_text("entity: X\nkey: K\nrecords: []\n")

    paths = [p.name for p in cli.expand_files((d,))]

    assert paths == ["a.yaml", "b.yaml"]


def test_expand_files_scenario_dir_yaml_only(tmp_path: Path) -> None:
    # V59: run <dir> expands *.yaml in that dir; overlay is a separate path
    sc = tmp_path / "scenario"
    sc.mkdir()
    (sc / "10-seed-capital.yaml").write_text("scenario: c\nsteps: []\n")
    (sc / "30-build.yaml").write_text("scenario: build\nsteps: []\n")
    ov = tmp_path / "overlays" / "default-24.200.001" / "scenario"
    ov.mkdir(parents=True)
    (ov / "30-build.yaml").write_text("scenario: build-overlay\nsteps: []\n")

    paths = [p.name for p in cli.expand_files((sc,))]

    assert paths == ["10-seed-capital.yaml", "30-build.yaml"]


def test_apply_explicit_config_does_not_append_overlay(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V59: typed path only; overlay config is not auto-appended
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://h/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    for name in ("bootstrap", "baseline", "setup", "master"):
        d = tmp_path / "config" / name
        d.mkdir(parents=True)
        (d / f"{name}.yaml").write_text(seed)
    ov_m = tmp_path / "overlays" / "default-24.200.001" / "config" / "master"
    ov_m.mkdir(parents=True)
    (ov_m / "99-rewrite.yaml").write_text(seed)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "diff",
        lambda client, baseline: (
            seen.append(str(baseline.path).replace("\\", "/")) or []
        ),
    )

    result = CliRunner().invoke(cli.cli, ["diff", "config/"])

    assert result.exit_code == 0, result.output
    assert "overlays/default-24.200.001" not in " ".join(seen)
    assert "99-rewrite.yaml" not in " ".join(seen)


def test_apply_acu_config_umbrella_skips_sibling_custom(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V60/V61: apply acu-config expands SEED_DIRS; sibling custom is not applied."""
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    custom = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: CUSTOM\n"
    for name in ("bootstrap", "baseline", "setup", "master"):
        d = tmp_path / "acu-config" / name
        d.mkdir(parents=True)
        (d / f"{name}.yaml").write_text(seed)
    (tmp_path / "qms").mkdir()
    (tmp_path / "qms" / "custom.yaml").write_text(custom)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "apply",
        lambda client, baseline, *_, **__: (
            seen.append(str(baseline.path).replace("\\", "/")) or (1, [])
        ),
    )

    result = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "acu-config"])

    assert result.exit_code == 0, result.output
    assert seen == [
        "acu-config/bootstrap/bootstrap.yaml",
        "acu-config/baseline/baseline.yaml",
        "acu-config/setup/setup.yaml",
        "acu-config/master/master.yaml",
    ]
    assert not any("qms" in s or "custom.yaml" in s for s in seen)


def test_apply_acu_config_then_custom_dir(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V61: apply acu-config <custom-dir> applies stock then the free-text dir."""
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    custom = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: CUSTOM\n"
    (tmp_path / "acu-config" / "bootstrap").mkdir(parents=True)
    (tmp_path / "acu-config" / "bootstrap" / "stock.yaml").write_text(seed)
    (tmp_path / "qms").mkdir()
    (tmp_path / "qms" / "custom.yaml").write_text(custom)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "apply",
        lambda client, baseline, *_, **__: (
            seen.append(str(baseline.path).replace("\\", "/")) or (1, [])
        ),
    )

    result = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "acu-config", "qms"])

    assert result.exit_code == 0, result.output
    assert seen == ["acu-config/bootstrap/stock.yaml", "qms/custom.yaml"]


def test_run_acu_scenario_expands_yaml(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V59/V60: run acu-scenario expands *.yaml under that dir."""
    sc = tmp_path / "acu-scenario"
    sc.mkdir()
    (sc / "10-seed-capital.yaml").write_text("scenario: capital\nsteps: []\n")
    (sc / "20-buy.yaml").write_text("scenario: buy\nsteps: []\n")
    monkeypatch.chdir(tmp_path)
    loaded: list[str] = []
    monkeypatch.setattr(
        cli.run,
        "run",
        lambda client, scenario, dry_run=False: (
            loaded.append(scenario.scenario) or cli.run.Outcome.PASSED
        ),
    )

    result = CliRunner().invoke(cli.cli, ["run", "--dry-run", "acu-scenario"])

    assert result.exit_code == 0, result.output
    assert loaded == ["capital", "buy"]


def test_run_overlay_same_basename_wins(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V44: run trunk + overlay dir; later same-basename replaces trunk."""
    sc = tmp_path / "acu-scenario"
    sc.mkdir()
    (sc / "10-seed-capital.yaml").write_text("scenario: capital\nsteps: []\n")
    (sc / "30-build.yaml").write_text("scenario: build-trunk\nsteps: []\n")
    ov = tmp_path / "overlays" / "default-24.200.001" / "acu-scenario"
    ov.mkdir(parents=True)
    (ov / "30-build.yaml").write_text("scenario: build-overlay\nsteps: []\n")
    monkeypatch.chdir(tmp_path)
    loaded: list[str] = []
    monkeypatch.setattr(
        cli.run,
        "run",
        lambda client, scenario, dry_run=False: (
            loaded.append(scenario.scenario) or cli.run.Outcome.PASSED
        ),
    )

    result = CliRunner().invoke(
        cli.cli,
        [
            "run",
            "--dry-run",
            "acu-scenario",
            "overlays/default-24.200.001/acu-scenario",
        ],
    )

    assert result.exit_code == 0, result.output
    assert loaded == ["capital", "build-overlay"]


def test_src_has_no_default_seed_dirs() -> None:
    """T268/V59: src/ never defines default_seed_dirs."""
    src = Path(__file__).resolve().parents[1] / "src"
    hits: list[str] = []
    for path in src.rglob("*.py"):
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if "default_seed_dirs" in line:
                hits.append(f"{path.relative_to(src.parent)}:{i}")
    assert hits == []


@pytest.fixture
def check_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real data repo for config check: one .env, password in the file only.

    The conftest autouse scrub keeps the process environment clean, so the
    secrets probe must source ACU_PASSWORD from the found .env (V3).
    """
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_config_check_strict_flag_is_gone(check_repo: Path) -> None:
    # T214/V27: --strict retired with the matrix probe
    result = CliRunner().invoke(cli.cli, ["config", "check", "--strict"])
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_config_check_all_probes_ok(
    check_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config check: dependency order, one ok line each on stdout (V9),
    # exit 0; the secrets probe sources ACU_PASSWORD from .env (V3); the REST
    # probe is the client context manager itself - login + landed-tenant
    # verify on enter (V5), logout guaranteed on exit (V6); ssh probe = ping
    calls: list[str] = []

    class RecordingClient(DummyClient):
        def __enter__(self) -> RecordingClient:
            calls.append("enter")
            return self

        def __exit__(
            self,
            exc_type: type[BaseException] | None = None,
            exc: BaseException | None = None,
            tb: TracebackType | None = None,
        ) -> None:
            calls.append("exit")

    monkeypatch.setattr(cli, "AcumaticaClient", RecordingClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: calls.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("ok discovery (")
    assert lines[0].endswith(".env)")
    assert lines[1] == "ok secrets (ACU_PASSWORD set)"
    assert lines[2] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[3] == "ok endpoints (Default/25.200.001 present)"
    assert lines[4] == "ok ssh (Administrator@acu.test)"
    assert calls == ["enter", "exit", "ping"]


def test_config_check_discovery_fail_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3 lax discovery: no .env up-tree is fine WITH --url, but with
    # neither the probe fails naming ACU_BASE_URL, exit 1, later probes
    # never run
    probes: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AcumaticaClient", lambda *a, **k: probes.append("rest"))
    monkeypatch.setattr(TenantManager, "ping", lambda self: probes.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("fail discovery: no .env found")
    assert "ACU_BASE_URL" in lines[0]
    assert probes == []


def test_config_check_flags_only_passes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T42 verify shape: no .env anywhere - the globals supply the full
    # config and every probe passes (V3 lax discovery, I.cmd precedence)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(
        cli.cli,
        [
            "--url",
            "http://acu.test/AcumaticaERP",
            "--ssh",
            "user@acu.test",
            "--password",
            "pw",
            "--tenant",
            "T1",
            "config",
            "check",
        ],
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0] == "ok discovery (no .env - flags only)"
    assert lines[1] == "ok secrets (--password)"
    assert lines[2] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[3] == "ok endpoints (Default/25.200.001 present)"
    assert lines[4] == "ok ssh (user@acu.test)"
    assert "pw" not in result.output  # the secret value is never printed (V2)


def test_config_check_secrets_fail_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config check: discovery/secrets fail stops - no live probes run
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_TENANT=T1\n"
    )
    monkeypatch.chdir(tmp_path)
    probes: list[str] = []
    monkeypatch.setattr(cli, "AcumaticaClient", lambda *a, **k: probes.append("rest"))
    monkeypatch.setattr(TenantManager, "ping", lambda self: probes.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0].startswith("ok discovery (")
    assert lines[1].startswith("fail secrets: password not set")
    assert len(lines) == 2
    assert probes == []


def test_config_check_rest_fail_still_probes_ssh(
    check_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T27 verify: wrong password -> REST fail while ssh still reports, exit 1;
    # a tenant-guard refusal (V5) surfaces the same way - __enter__ raises
    class RefusingClient(DummyClient):
        def __enter__(self) -> RefusingClient:
            raise RuntimeError("login failed (401)")

    monkeypatch.setattr(cli, "AcumaticaClient", RefusingClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "fail rest: login failed (401)" in result.output
    assert "ok ssh (Administrator@acu.test)" in result.output


def test_config_check_ssh_fail_still_probes_rest(
    check_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config check: REST + ssh probe independently, either way exit 1
    def boom(self: TenantManager) -> None:
        raise RuntimeError("remote command failed (255)")

    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", boom)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "ok rest (http://acu.test/AcumaticaERP, tenant T1)" in result.output
    assert "fail ssh: remote command failed (255)" in result.output


def test_config_check_skips_ssh_when_blank(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T124/I.cmd: blank ACU_SSH= hosted opt-out → skip ssh line, never fail;
    # REST still probed; exit 0 when non-skipped probes pass
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)
    probes: list[str] = []
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: probes.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines[0].startswith("ok discovery (")
    assert lines[1] == "ok secrets (ACU_PASSWORD set)"
    assert lines[2] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[3] == "ok endpoints (Default/25.200.001 present)"
    assert lines[4] == "skip ssh (ACU_SSH not set)"
    assert probes == []


def test_tenant_list_fails_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T124/I.cmd: blank ACU_SSH= → empty post-default; tenant cmds hard-error
    # naming ACU_SSH before any remote
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=\nACU_PASSWORD=secret\n"
    )
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["tenant", "list"])

    assert result.exit_code != 0
    assert "ACU_SSH not set" in result.output


def test_config_check_discovery_fails_without_base_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config check: discovery needs base_url from --url or ACU_BASE_URL
    # (V27); none → fail
    (tmp_path / ".env").write_text("ACU_SSH=Administrator@acu.test\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "fail discovery:" in result.output
    assert "missing required base_url" in result.output


def test_tenant_short_flag_is_gone(wired: Instance) -> None:
    # T43/V16: every flag is long-only - the tenant global was the last
    # surviving short form (same retirement class as T9's `schema -o`)
    result = CliRunner().invoke(cli.cli, ["-t", "T1", "config", "show"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_global_host_flag_is_gone(wired: Instance) -> None:
    # T40: derivation retired, nothing for a global --host to re-run; the
    # flag survives only on config init as template substitution
    result = CliRunner().invoke(cli.cli, ["--host", "edge.example", "config", "show"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_cell_flag_is_gone(wired: Instance) -> None:
    # T218/V27: --cell retired with matrix.yaml
    result = CliRunner().invoke(cli.cli, ["--cell", "x", "config", "show"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_host_flag_rejected_on_other_subcommands(wired: Instance) -> None:
    # V16: --host belongs to config init alone
    result = CliRunner().invoke(cli.cli, ["config", "show", "--host", "edge.example"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_apply_dry_run_summary(wired: Instance, tmp_path: Path) -> None:
    result = CliRunner().invoke(
        cli.cli, ["apply", "--dry-run", str(_baseline(tmp_path))]
    )

    assert result.exit_code == 0
    assert "would PUT UnitsOfMeasure [KG]" in result.output
    assert "1 record(s) (dry run)" in result.output


def test_apply_transport_error_summary_exits_1(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V45/gh #51: timed-out PUT -> later records apply, summary names it, exit 1."""
    path = tmp_path / "uoms.yaml"
    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n"
        "  - UOM: KG\n    Description: Kilogram\n"
        "  - UOM: HOUR\n    Description: Hour\n"
    )
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.content.decode())
            if '"KG"' in puts[-1]:  # persistent: outlasts the gh #52 retries
                raise httpx.ReadTimeout("timed out", request=request)
        if request.url.path.endswith("/Frames/Login.aspx"):
            # landed-tenant guard (V5); the wired instance is tenant T1
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", str(path)])

    assert result.exit_code == 1
    assert len(puts) == 4  # KG x3 tries, HOUR x1
    assert "PUT UnitsOfMeasure [HOUR]" in result.output
    assert "x apply: 1 error(s), 1 record(s) applied" in result.stderr
    assert f"{path}: UnitsOfMeasure [KG]: request timed out to " in result.stderr


def test_apply_connect_error_reports_stop_and_not_applied(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V45/gh #51: hard-stop names stopped-at record + every file not applied."""
    first, second = tmp_path / "10-a.yaml", tmp_path / "20-b.yaml"
    for path in (first, second):
        path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", str(first), str(second)])

    assert result.exit_code == 1
    err = result.stderr
    assert f"x apply: stopped at {first}: UnitsOfMeasure [KG]: cannot connect" in err
    assert "x apply: 0 record(s) applied before the stop" in err
    assert f"x not applied: {first} (from UnitsOfMeasure [KG]; 0 of" in err
    assert f"x not applied: {second}" in err
    assert f"{second} -> " not in result.output


def _pool_handler(
    log: list[str], refuse_login: int | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Mock tenant T1 that logs login / logout / PUT and can refuse login k."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/auth/login"):
            log.append("login")
            if log.count("login") == refuse_login:
                return httpx.Response(500, json={"exceptionMessage": "API Login Limit"})
            return httpx.Response(204)
        if path.endswith("/auth/logout"):
            log.append("logout")
            return httpx.Response(204)
        if path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        if request.method == "PUT":
            log.append("put")
        return httpx.Response(200, json={})

    return handler


@pytest.mark.parametrize("command", ["apply", "run"])
def test_concurrency_opens_n_sessions_before_write(
    wired: Instance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """V66/gh #71: N logins (tenant-verified) precede the first write; N logouts."""
    path = tmp_path / "f.yaml"
    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n"
        if command == "apply"
        else "scenario: s\nsteps:\n"
        "  - id: s1\n    put: UnitsOfMeasure\n    record: {UOM: KG}\n"
    )
    log: list[str] = []
    handler = _pool_handler(log)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, [command, "--concurrency", "3", str(path)])

    assert result.exit_code == 0, result.output
    assert log[:3] == ["login"] * 3
    assert log.count("logout") == 3
    assert "put" in log


@pytest.mark.parametrize("command", ["apply", "run"])
def test_concurrency_refused_login_logs_out_opened_no_write(
    wired: Instance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """V66/V6: login 3/3 refused -> opened sessions logged out, exit 1, 0 writes."""
    path = tmp_path / "f.yaml"
    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n"
        if command == "apply"
        else "scenario: s\nsteps:\n"
        "  - id: s1\n    put: UnitsOfMeasure\n    record: {UOM: KG}\n"
    )
    log: list[str] = []
    handler = _pool_handler(log, refuse_login=3)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, [command, "--concurrency", "3", str(path)])

    assert isinstance(result.exception, RuntimeError)
    assert "put" not in log
    # 2 opened sessions + the refused one's own V6 cleanup; none left open
    assert log == ["login"] * 3 + ["logout"] * 3
    message = str(result.exception)  # main() maps RuntimeError -> x, exit 1
    assert "session 3/3 login failed" in message
    assert "--concurrency 3 needs 3 API-user license slots" in message


@pytest.mark.parametrize("command", ["apply", "run"])
def test_concurrency_rejects_zero(
    wired: Instance, tmp_path: Path, command: str
) -> None:
    """V66: --concurrency is IntRange(min=1); 0 is a usage error, no HTTP."""
    path = tmp_path / "f.yaml"
    path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")
    result = CliRunner().invoke(cli.cli, [command, "--concurrency", "0", str(path)])

    assert result.exit_code == 2
    assert "--concurrency" in result.output


def test_concurrency_default_is_one_session(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V66: default --concurrency 1 = the plain single session (one login)."""
    path = tmp_path / "f.yaml"
    path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")
    log: list[str] = []
    handler = _pool_handler(log)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", str(path)])

    assert result.exit_code == 0, result.output
    assert log == ["login", "put", "logout"]


def _wire_pool(monkeypatch: pytest.MonkeyPatch, log: list[str]) -> None:
    handler = _pool_handler(log)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("concurrency", ["1", "2"])
def test_checkpoint_apply_rerun_skips_done_records(
    wired: Instance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrency: str,
) -> None:
    """V67: run 1 records each PUT; run 2 skips them (no PUT) + counts them."""
    path = tmp_path / "f.yaml"
    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n  - UOM: LB\n"
    )
    ckpt = tmp_path / "ckpt.jsonl"
    args = ["apply", "--concurrency", concurrency, "--checkpoint", str(ckpt)]
    log: list[str] = []
    _wire_pool(monkeypatch, log)

    first = CliRunner().invoke(cli.cli, [*args, str(path)])
    assert first.exit_code == 0, first.output
    assert log.count("put") == 2
    assert len(ckpt.read_text().splitlines()) == 2

    log.clear()
    second = CliRunner().invoke(cli.cli, ["--output", "json", *args, str(path)])
    assert second.exit_code == 0, second.output
    assert "put" not in log
    assert "skip UnitsOfMeasure [KG] (checkpoint)" in second.output
    assert json.loads(second.stdout)["checkpoint"] == 2


def test_checkpoint_apply_edited_record_reapplies(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V67: digest covers content - an edited record PUTs again."""
    path = tmp_path / "f.yaml"
    path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")
    ckpt = tmp_path / "ckpt.jsonl"
    args = ["apply", "--checkpoint", str(ckpt), str(path)]
    log: list[str] = []
    _wire_pool(monkeypatch, log)
    assert CliRunner().invoke(cli.cli, args).exit_code == 0

    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n    Descr: Kilo\n"
    )
    log.clear()
    result = CliRunner().invoke(cli.cli, args)

    assert result.exit_code == 0, result.output
    assert log.count("put") == 1
    assert "apply: 0 from checkpoint" in result.output


def test_checkpoint_run_skips_done_scenario(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V67: a recorded scenario is skipped on rerun with no step HTTP."""
    path = tmp_path / "s.yaml"
    path.write_text(
        "scenario: s\nsteps:\n"
        "  - id: s1\n    put: UnitsOfMeasure\n    record: {UOM: KG}\n"
    )
    ckpt = tmp_path / "ckpt.jsonl"
    args = ["run", "--checkpoint", str(ckpt), str(path)]
    log: list[str] = []
    _wire_pool(monkeypatch, log)
    assert CliRunner().invoke(cli.cli, args).exit_code == 0
    assert log.count("put") == 1

    log.clear()
    result = CliRunner().invoke(cli.cli, args)

    assert result.exit_code == 0, result.output
    assert "put" not in log
    assert "(checkpoint)" in result.output
    assert "1 from checkpoint" in result.output


def test_checkpoint_run_same_name_same_body_docs_all_run(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V67: twin docs (one file + a copy) each run once; rerun skips all."""
    body = (
        "scenario: s\nsteps:\n"
        "  - id: s1\n    put: UnitsOfMeasure\n    record: {UOM: KG}\n"
    )
    (tmp_path / "a.yaml").write_text(body + "---\n" + body)
    (tmp_path / "b.yaml").write_text(body)
    ckpt = tmp_path / "ckpt.jsonl"
    args = ["run", "--checkpoint", str(ckpt), str(tmp_path / "a.yaml")]
    args.append(str(tmp_path / "b.yaml"))
    log: list[str] = []
    _wire_pool(monkeypatch, log)

    first = CliRunner().invoke(cli.cli, args)
    assert first.exit_code == 0, first.output
    assert log.count("put") == 3
    assert "0 from checkpoint" in first.output

    log.clear()
    second = CliRunner().invoke(cli.cli, args)
    assert second.exit_code == 0, second.output
    assert "put" not in log
    assert "3 from checkpoint" in second.output


@pytest.mark.parametrize(
    "args",
    [
        ["apply", "--plan"],
        ["apply", "--dry-run"],
        ["run", "--dry-run"],
    ],
)
def test_checkpoint_refuses_read_only_modes(
    wired: Instance, tmp_path: Path, args: list[str]
) -> None:
    """V67: --checkpoint x --plan/--dry-run = usage error (exit 2)."""
    path = tmp_path / "f.yaml"
    path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")
    ckpt = tmp_path / "ckpt.jsonl"
    result = CliRunner().invoke(cli.cli, [*args, "--checkpoint", str(ckpt), str(path)])

    assert result.exit_code == 2
    assert "--checkpoint cannot combine" in result.output
    assert not ckpt.exists()


@pytest.mark.parametrize("command", ["apply", "run"])
def test_checkpoint_malformed_fails_before_login(
    wired: Instance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    command: str,
) -> None:
    """V67: a malformed checkpoint line exits 1 with zero HTTP."""
    path = tmp_path / "f.yaml"
    path.write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n"
        if command == "apply"
        else "scenario: s\nsteps:\n"
        "  - id: s1\n    put: UnitsOfMeasure\n    record: {UOM: KG}\n"
    )
    ckpt = tmp_path / "ckpt.jsonl"
    ckpt.write_text("garbage\n")
    log: list[str] = []
    _wire_pool(monkeypatch, log)
    result = CliRunner().invoke(
        cli.cli, [command, "--checkpoint", str(ckpt), str(path)]
    )

    assert isinstance(result.exception, RuntimeError)
    assert "ckpt.jsonl:1: malformed checkpoint line" in str(result.exception)
    assert log == []


def _bulk_handler(
    log: list[str], done: Counter[str], fail_at: int | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    """Thread-safe mock T1: counts each record's successful PUT by UOM key.

    ``fail_at`` = from the k-th PUT on, ConnectError (host gone: a killed load
    past the gh #52 blip retry, V45 stop).
    """
    lock = threading.Lock()
    puts = itertools.count(1)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        with lock:
            if path.endswith("/auth/login"):
                log.append("login")
                return httpx.Response(204)
            if path.endswith("/auth/logout"):
                log.append("logout")
                return httpx.Response(204)
        if path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        if request.method == "PUT":
            with lock:
                if fail_at is not None and next(puts) >= fail_at:
                    raise httpx.ConnectError("killed", request=request)
                done[json.loads(request.content)["UOM"]["value"]] += 1
        return httpx.Response(200, json={})

    return handler


def _bulk_seed(path: Path, count: int) -> None:
    rows = "".join(f"  - UOM: U{n:05d}\n" for n in range(count))
    path.write_text(f"entity: UnitsOfMeasure\nkey: UOM\nrecords:\n{rows}")


def test_bulk_apply_10k_records_pool_of_8_puts_each_once(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V66/gh #71: 10K records over 8 sessions -> each PUT once, 8 in / 8 out."""
    path = tmp_path / "bulk.yaml"
    _bulk_seed(path, 10_000)
    log: list[str] = []
    done: Counter[str] = Counter()
    handler = _bulk_handler(log, done)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", "--concurrency", "8", str(path)])

    assert result.exit_code == 0, result.output
    assert len(done) == 10_000
    assert set(done.values()) == {1}
    assert log == ["login"] * 8 + ["logout"] * 8


@pytest.mark.parametrize("concurrency", ["1", "4"])
def test_killed_load_resumes_from_checkpoint_without_duplicates(
    wired: Instance,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    concurrency: str,
) -> None:
    """V67/V45: load dies mid-file -> exit 1 + not-applied; rerun finishes, 0 dup."""
    path = tmp_path / "bulk.yaml"
    _bulk_seed(path, 200)
    ckpt = tmp_path / "ckpt.jsonl"
    args = ["apply", "--concurrency", concurrency, "--checkpoint", str(ckpt)]
    log: list[str] = []
    done: Counter[str] = Counter()
    handlers = iter([_bulk_handler(log, done, fail_at=120), _bulk_handler(log, done)])
    handler = next(handlers)
    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    killed = CliRunner().invoke(cli.cli, [*args, str(path)])

    assert killed.exit_code == 1
    assert f"not applied: {path}" in killed.output
    first = sum(done.values())
    assert 0 < first < 200
    assert len(ckpt.read_text().splitlines()) == first

    handler = next(handlers)
    resumed = CliRunner().invoke(cli.cli, ["--output", "json", *args, str(path)])

    assert resumed.exit_code == 0, resumed.output
    assert len(done) == 200
    assert set(done.values()) == {1}
    assert json.loads(resumed.stdout)["checkpoint"] == first


def test_apply_422_survives_dead_logout(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #53: PUT 422 then logout ConnectError -> 422 text + summary, exit 1."""
    path = tmp_path / "uoms.yaml"
    path.write_text("entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/entity/auth/logout"):
            raise httpx.ConnectError("refused", request=request)
        if request.method == "PUT":
            return httpx.Response(422, json={"exceptionMessage": "KG is reserved"})
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", str(path)])

    assert result.exit_code == 1
    err = " ".join(result.stderr.split())  # rich wraps
    assert "x apply: 1 error(s), 0 record(s) applied" in err
    assert "UnitsOfMeasure -> 422: KG is reserved" in err
    assert "! logout failed: cannot connect" in err


def test_diff_clean_exits_zero(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _baseline(tmp_path)
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(path)])

    assert result.exit_code == 0
    assert (
        f"{path} -> T1 on http://acu.test/AcumaticaERP (UnitsOfMeasure)"
        in result.output
    )
    assert "1 record(s) ok" in result.output
    assert "+ no drift on T1 (http://acu.test/AcumaticaERP, 1 file(s))" in result.stderr


def test_diff_directory_expands_to_yaml_files(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uoms.yaml").write_text(BASELINE)
    (tmp_path / "terms.yaml").write_text(BASELINE)
    (tmp_path / "notes.txt").write_text("not a baseline")
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ no drift on T1 (http://acu.test/AcumaticaERP, 2 file(s))" in result.stderr


def test_directory_expansion_skips_features_yaml(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """features.yaml is package-build config (I.data), never a seed file."""
    (tmp_path / "uoms.yaml").write_text(BASELINE)
    (tmp_path / "features.yaml").write_text(FEATURES_YAML)
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ no drift on T1 (http://acu.test/AcumaticaERP, 1 file(s))" in result.stderr


def test_apply_empty_directory_errors(wired: Instance, tmp_path: Path) -> None:
    # a lone features.yaml does not make a directory seedable either
    (tmp_path / "features.yaml").write_text(FEATURES_YAML)
    result = CliRunner().invoke(cli.cli, ["apply", str(tmp_path)])

    assert result.exit_code == 1
    assert "no seed *.yaml files in directory" in result.output


def _seed_repo(tmp_path: Path) -> None:
    """A minimal data repo: .env plus one seed file per scaffolded dir."""
    (tmp_path / ".env").write_text("ACU_BASE_URL=http://acu.test/AcumaticaERP\n")
    for dirname, fname, body in (
        ("bootstrap", "terms.yaml", BOOTSTRAP_YAML),
        ("baseline", "uoms.yaml", BASELINE),
        ("setup", "calendar.yaml", SETUP_YAML),
    ):
        (tmp_path / dirname).mkdir()
        (tmp_path / dirname / fname).write_text(body)


def test_diff_explicit_umbrella_seed_order(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V30: explicit dir with SEED_DIRS children expands in fixed order
    _seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed, "diff", lambda client, baseline: seen.append(baseline.path.name) or []
    )

    result = CliRunner().invoke(cli.cli, ["diff", "."])

    assert result.exit_code == 0
    assert seen == ["terms.yaml", "uoms.yaml", "calendar.yaml"]
    assert "+ no drift on T1 (http://acu.test/AcumaticaERP, 3 file(s))" in result.stderr


def _boom_client(*_a: object, **_k: object) -> None:
    raise AssertionError("bare command must not open HTTP")


@pytest.mark.parametrize("cmd", ["apply", "diff", "run", "state"])
def test_bare_data_path_cmd_prints_help_no_http(
    wired: Instance, monkeypatch: pytest.MonkeyPatch, cmd: str
) -> None:
    # V59: zero args print that command's help, exit non-zero, no HTTP
    monkeypatch.setattr(cli, "AcumaticaClient", _boom_client)
    result = CliRunner().invoke(cli.cli, [cmd])

    assert result.exit_code == 1
    assert "Usage:" in result.output
    assert cmd in result.output.lower()
    help_r = CliRunner().invoke(cli.cli, [cmd, "--help"])
    assert help_r.exit_code == 0
    assert result.output.strip() == help_r.output.strip()


def test_explicit_files_override_default_dirs(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T44/I.cmd: explicit FILES behavior unchanged - the default never
    # augments an explicit argument list
    _seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed, "diff", lambda client, baseline: seen.append(baseline.path.name) or []
    )

    result = CliRunner().invoke(cli.cli, ["diff", "baseline/uoms.yaml"])

    assert result.exit_code == 0
    assert seen == ["uoms.yaml"]


def test_bare_apply_does_not_plan_config_umbrella(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V59: omitted path prints help; explicit config/ still plans the umbrella
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setenv("ACU_BASE_URL", "http://erp.test/AcumaticaERP")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    bare = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
    explicit = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "acu-config/"])

    assert bare.exit_code == 1
    assert "Usage:" in bare.output
    assert "would PUT" not in bare.output
    assert explicit.exit_code == 0, explicit.output
    plans = [line for line in explicit.output.splitlines() if "would " in line]
    assert plans


def test_diff_drift_exits_two_with_lines_on_stdout(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _baseline(tmp_path)
    drift = "UnitsOfMeasure [KG].Description: source='Kilogram' live='kg'"
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [drift])
    result = CliRunner().invoke(cli.cli, ["diff", str(path)])

    # the load-bearing contract (V9): exit 0 ok, 1 error, 2 drift
    assert result.exit_code == 2
    assert (
        f"{path} -> T1 on http://acu.test/AcumaticaERP (UnitsOfMeasure)"
        in result.output
    )
    assert "1 record(s), 1 drift(s)" in result.output
    assert "x DRIFT on T1 (http://acu.test/AcumaticaERP):" in result.stderr
    assert drift in result.output


def test_schema_writes_swagger_to_out_dir(wired: Instance, tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["schema", "--out", str(tmp_path / "dump")])

    out_file = tmp_path / "dump" / "swagger-Default-25.200.001.json"
    assert result.exit_code == 0
    assert out_file.read_bytes() == SWAGGER
    assert f"{out_file} ({len(SWAGGER)} bytes)" in result.output


def test_schema_short_o_flag_is_gone(wired: Instance, tmp_path: Path) -> None:
    # V16: short flags are reserved for globals
    result = CliRunner().invoke(cli.cli, ["schema", "-o", str(tmp_path)])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_schema_defaults_to_data_root_schemas(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("# data-repo sentinel\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    result = CliRunner().invoke(cli.cli, ["schema"])

    assert result.exit_code == 0
    assert (tmp_path / "schemas" / "swagger-Default-25.200.001.json").exists()
    assert not (sub / "schemas").exists()


def test_main_maps_runtime_error_to_one_line_and_exit_1(
    wired: Instance,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fake_list(self: TenantManager) -> list[Tenant]:
        raise RuntimeError("remote command failed (255)")

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.delenv("ACU_DEBUG", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    assert "x remote command failed (255)" in capsys.readouterr().err


def test_main_maps_connect_error_to_friendly_line(
    wired: Instance,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Transport connect → class + target + hint; never raw errno dump (V9/T123)."""
    req = httpx.Request("GET", "https://erp.example.com/AcumaticaERP/entity/auth/login")

    def fake_list(self: TenantManager) -> list[Tenant]:
        raise httpx.ConnectError("[Errno 61] Connection refused", request=req)

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.delenv("ACU_DEBUG", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "x cannot connect to " in err
    assert "https://erp.example.com/AcumaticaERP/entity/auth/login" in err
    assert "ACU_BASE_URL" in err
    assert "Connection refused" not in err
    assert "Errno 61" not in err


def test_main_maps_timeout_to_friendly_line(
    wired: Instance,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    req = httpx.Request(
        "GET", "https://erp.example.com/AcumaticaERP/entity/Default/25.200.001"
    )

    def fake_list(self: TenantManager) -> list[Tenant]:
        raise httpx.ConnectTimeout("timed out", request=req)

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.delenv("ACU_DEBUG", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "x request timed out to " in err
    assert "entity/Default/25.200.001" in err
    assert "ACU_BASE_URL" in err
    assert err.count("timed out") == 1  # kind only, not raw str(exc) dump


def test_main_maps_tls_error_to_friendly_line(
    wired: Instance,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    req = httpx.Request("GET", "https://erp.example.com/AcumaticaERP/")

    def fake_list(self: TenantManager) -> list[Tenant]:
        raise httpx.ConnectError(
            "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            request=req,
        )

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.delenv("ACU_DEBUG", raising=False)

    with pytest.raises(SystemExit) as excinfo:
        cli.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert "x TLS error to https://erp.example.com/AcumaticaERP/" in err
    assert "ACU_BASE_URL" in err
    assert "CERTIFICATE_VERIFY_FAILED" not in err


def test_format_failure_uses_explicit_target() -> None:
    req = httpx.Request("GET", "https://other.example/path")
    exc = httpx.ConnectError("refused", request=req)
    msg = format_failure(exc, target="https://erp.example.com/AcumaticaERP")
    assert msg == (
        "cannot connect to https://erp.example.com/AcumaticaERP "
        "(check ACU_BASE_URL / network / instance up)"
    )


def test_main_reraises_under_acu_debug(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_list(self: TenantManager) -> list[Tenant]:
        raise RuntimeError("boom")

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.setenv("ACU_DEBUG", "1")

    with pytest.raises(RuntimeError, match="boom"):
        cli.main()


def test_main_reraises_transport_under_acu_debug(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    req = httpx.Request("GET", "https://erp.example.com/")

    def fake_list(self: TenantManager) -> list[Tenant]:
        raise httpx.ConnectError("refused", request=req)

    monkeypatch.setattr(TenantManager, "list", fake_list)
    monkeypatch.setattr(sys, "argv", ["acu", "tenant", "list"])
    monkeypatch.setenv("ACU_DEBUG", "1")

    with pytest.raises(httpx.ConnectError, match="refused"):
        cli.main()


@pytest.mark.parametrize("command", ["apply", "diff"])
def test_invalid_late_file_sends_nothing(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    """Gh #54: a bad file after a good one in an umbrella dir -> no HTTP."""
    leaf = tmp_path / "acu-config" / "baseline"
    leaf.mkdir(parents=True)
    (leaf / "10-uoms.yaml").write_text(BASELINE)
    (leaf / "20-bad.yaml").write_text("entity: UnitsOfMeasure\nrecords: []\n")
    (leaf / "30-broken.yaml").write_text("entity: [\n")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, [command, str(tmp_path / "acu-config")])

    assert result.exit_code == 1
    assert requests == []  # no login, no PUT/GET
    err = " ".join(result.output.split())
    assert "2 of 3 seed file(s) failed validation - nothing sent" in err
    assert "20-bad.yaml" in err
    assert "30-broken.yaml: invalid YAML" in err


def _subdir_of_data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env").write_text("# data-repo sentinel\n")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    return sub


def test_extract_default_out_resolves_to_data_root(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #59: run from <root>/sub/, default --out is <root>/acu-config."""
    _subdir_of_data_root(tmp_path, monkeypatch)
    seen: list[Path] = []

    def fake_run(client: object, out_dir: Path, **_: object) -> int:
        seen.append(out_dir)
        return 0

    monkeypatch.setattr(cli.extract, "run", fake_run)
    result = CliRunner().invoke(cli.cli, ["survey", "extract"])

    assert result.exit_code == 0, result.output
    assert seen == [Path("../acu-config")]
    assert seen[0].resolve() == (tmp_path / "acu-config").resolve()


def test_state_default_out_resolves_to_data_root(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #59: run from <root>/sub/, default --out is <root>/state."""
    sub = _subdir_of_data_root(tmp_path, monkeypatch)
    view = sub / "10-tb.yaml"
    view.write_text("x\n")
    seen: list[Path] = []

    def fake_run_views(
        client: object, views: object, *, out_dir: Path, mode: str
    ) -> int:
        seen.append(out_dir)
        return 0

    monkeypatch.setattr(cli.state, "load_view", lambda path: path)
    monkeypatch.setattr(cli.state, "run_views", fake_run_views)
    result = CliRunner().invoke(cli.cli, ["state", "--dry-run", str(view)])

    assert result.exit_code == 0, result.output
    assert seen[0].resolve() == (tmp_path / "state").resolve()


def test_default_out_without_data_root_is_cwd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert default_out("state") == Path("state")


_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "argv",
    [
        ["apply", "--dry-run", str(_REPO / "acu-config")],
        ["run", "--dry-run", str(_REPO / "acu-scenario")],
        ["state", "--dry-run", str(_REPO / "acu-config" / "views")],
    ],
    ids=["apply", "run", "state"],
)
def test_dry_run_needs_no_config_or_session(
    argv: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #63: dry-run with no .env and no ACU_* resolves nothing, opens nothing."""
    for key in list(os.environ):
        if key.startswith("ACU_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)

    def no_client(*_: object, **__: object) -> None:
        raise AssertionError("dry-run opened a session")

    monkeypatch.setattr(cli, "AcumaticaClient", no_client)
    result = CliRunner().invoke(cli.cli, argv)

    assert result.exit_code == 0, result.output
    assert "password" not in result.output


def test_run_step_422_names_file_and_step_and_lists_not_run(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #58: 422 on step 2 of scenario 1 -> x <file> [<id>]; scenario 2 not run."""
    first, second = tmp_path / "10-a.yaml", tmp_path / "20-b.yaml"
    steps = (
        "steps:\n"
        "  - id: one\n    put: SalesOrder\n    record: {OrderType: SO}\n"
        "  - id: two\n    put: SalesOrder\n    record: {OrderType: SO}\n"
    )
    first.write_text("scenario: a\n" + steps)
    second.write_text("scenario: b\n" + steps)
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
            if len(puts) == 2:
                return httpx.Response(422, json={"message": "bad order"})
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["run", str(tmp_path)])

    assert result.exit_code == 1
    assert len(puts) == 2
    assert f"x {first} [two]: PUT " in result.stderr
    assert f"x not run: {second}" in result.stderr
    assert "x run: 0 passed, 1 failed, 0 skipped, 1 not run on T1" in result.stderr


def test_run_multi_document_failure_folds_not_run_range(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #73: failure at doc 2 of 5 -> x <file>#2; rest folds to one line."""
    load = tmp_path / "10-orders.yaml"
    load.write_text(
        "".join(
            f"---\nscenario: so-{n}\nsteps:\n"
            "  - id: order\n    put: SalesOrder\n    record: {OrderType: SO}\n"
            for n in range(1, 6)
        )
    )
    later = tmp_path / "20-after.yaml"
    later.write_text("scenario: after\nsteps: []\n")
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
            if len(puts) == 2:
                return httpx.Response(422, json={"message": "bad order"})
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["run", str(tmp_path)])
    puts.clear()
    json_result = CliRunner().invoke(
        cli.cli, ["--output", "json", "run", str(tmp_path)]
    )

    assert result.exit_code == 1
    assert f"x {load}#2 [order]: PUT " in result.stderr
    assert f"x not run: {load}#3-#5 (3 documents)" in result.stderr
    assert f"x not run: {later}" in result.stderr
    assert "x run: 1 passed, 1 failed, 0 skipped, 4 not run on T1" in result.stderr
    doc = json.loads(json_result.stdout)
    assert [(s["doc"], s["outcome"]) for s in doc["scenarios"]] == [
        (1, "passed"),
        (2, "failed"),
    ]
    assert doc["not_run"] == [f"{load}#3", f"{load}#4", f"{load}#5", str(later)]


def test_apply_plan_prints_summary_and_writes_nothing(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #55: --plan reads live, labels records, ends with the count line."""
    path = _baseline(tmp_path)
    puts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            puts.append(request.url.path)
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        if request.url.path.endswith("/UnitsOfMeasure"):
            return httpx.Response(200, json=[])
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )
    result = CliRunner().invoke(cli.cli, ["apply", "--plan", str(path)])

    assert result.exit_code == 0, result.output
    assert puts == []
    assert "  create UnitsOfMeasure [KG]" in result.stdout
    assert "plan: 1 create, 0 update, 0 invoke, 0 unchanged, 0 failed" in (
        result.stdout
    )


def test_apply_plan_modes_are_exclusive(wired: Instance, tmp_path: Path) -> None:
    """Gh #55: --plan, --changed-only, --dry-run do not combine."""
    result = CliRunner().invoke(
        cli.cli, ["apply", "--plan", "--changed-only", str(_baseline(tmp_path))]
    )

    assert result.exit_code == 2
    assert "exclusive" in result.output


def _live_rest(
    monkeypatch: pytest.MonkeyPatch, rows: dict[str, list[dict[str, Any]]]
) -> None:
    """RealClient over a fake host: GET <entity> returns rows (wrapped)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Frames/Login.aspx"):
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        for entity, found in rows.items():
            if request.method == "GET" and request.url.path.endswith(f"/{entity}"):
                return httpx.Response(200, json=[wrap(r) for r in found])
        return httpx.Response(204 if "/auth/" in request.url.path else 200, json={})

    monkeypatch.setattr(
        cli,
        "AcumaticaClient",
        lambda inst, **kw: RealClient(inst, transport=httpx.MockTransport(handler)),
    )


def test_diff_output_json_is_one_document_on_stdout(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #57: diff --output json -> parseable stdout, typed drift items, exit 2."""
    path = _baseline(tmp_path)
    _live_rest(monkeypatch, {"UnitsOfMeasure": [{"UOM": "KG", "Description": "kg"}]})
    result = CliRunner().invoke(cli.cli, ["--output", "json", "diff", str(path)])

    assert result.exit_code == 2
    doc = json.loads(result.stdout)
    assert doc["command"] == "diff"
    assert doc["drift"] == 1
    [item] = doc["files"][0]["drifts"]
    assert item == {
        "file": str(path),
        "entity": "UnitsOfMeasure",
        "keys": {"UOM": "KG"},
        "field": "Description",
        "kind": "changed",
        "source": "Kilogram",
        "live": "kg",
        "text": "UnitsOfMeasure [KG].Description: source='Kilogram' live='kg'",
    }
    # progress and the drift dump moved to stderr
    assert f"{path} -> T1" in result.stderr


def test_diff_output_json_missing_record(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _baseline(tmp_path)
    _live_rest(monkeypatch, {"UnitsOfMeasure": []})
    result = CliRunner().invoke(cli.cli, ["--output", "json", "diff", str(path)])

    [item] = json.loads(result.stdout)["files"][0]["drifts"]
    assert (item["kind"], item["field"], item["keys"]) == ("missing", "", {"UOM": "KG"})


def test_apply_output_json_summary(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #57: apply --output json -> counts + errors document."""
    path = _baseline(tmp_path)
    _live_rest(monkeypatch, {"UnitsOfMeasure": []})
    result = CliRunner().invoke(
        cli.cli, ["--output", "json", "apply", "--plan", str(path)]
    )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert doc["command"] == "apply"
    assert doc["mode"] == "plan"
    assert doc["counts"] == {"create": 1}
    assert (doc["ok"], doc["failed"], doc["errors"]) == (1, 0, [])
    assert doc["files"] == [
        {"file": str(path), "entity": "UnitsOfMeasure", "ok": 1, "errors": []}
    ]


def test_run_output_json_lists_outcomes_and_misses(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #57: run --output json -> per-scenario outcome + expectation misses."""
    scenario = tmp_path / "10-a.yaml"
    scenario.write_text("scenario: a\nsteps: []\n")

    def fake_run(
        client: object, sc: object, dry_run: bool = False, misses: Any = None
    ) -> cli.run.Outcome:
        misses.append("GL [1].Balance: expected 1 got 2")
        return cli.run.Outcome.FAILED

    monkeypatch.setattr(cli.run, "run", fake_run)
    result = CliRunner().invoke(cli.cli, ["--output", "json", "run", str(scenario)])

    assert result.exit_code == 1
    doc = json.loads(result.stdout)
    assert doc["counts"] == {"passed": 0, "failed": 1, "skipped": 0, "not_run": 0}
    assert doc["scenarios"] == [
        {
            "file": str(scenario),
            "outcome": "failed",
            "misses": ["GL [1].Balance: expected 1 got 2"],
        }
    ]


def test_state_output_json_reports_views(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gh #57: state --diff --output json -> per-view status document."""
    view = _REPO / "acu-config" / "views" / "10-trial-balance.yaml"

    def fake_views(
        client: object, views: object, *, out_dir: Path, mode: str, report: Any
    ) -> int:
        report.append({"view": str(view), "status": "changed", "differences": ["x"]})
        return 0

    monkeypatch.setattr(cli.state, "run_views", fake_views)
    result = CliRunner().invoke(
        cli.cli, ["--output", "json", "state", "--diff", str(view)]
    )

    assert result.exit_code == 0, result.output
    doc = json.loads(result.stdout)
    assert (doc["command"], doc["mode"], doc["changed"]) == ("state", "diff", 1)
    assert doc["views"][0]["differences"] == ["x"]


def _multi_doc(count: int, expect: str = "") -> str:
    return "---\n".join(
        f"scenario: d{i}\nsteps:\n  - id: s{i}\n    put: UnitsOfMeasure\n"
        f"    record: {{UOM: U{i:03}}}\n{expect}"
        for i in range(count)
    )


def _run_pool_tenant(
    monkeypatch: pytest.MonkeyPatch, fail: str | None = None
) -> tuple[list[str], set[int]]:
    """Wire cli.AcumaticaClient to a mock tenant; log PUT UOMs + sessions used."""
    lock = threading.Lock()
    puts: list[str] = []
    sessions: set[int] = set()
    opened = itertools.count()

    def factory(inst: Instance, **_: object) -> RealClient:
        session = next(opened)

        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith(("/auth/login", "/auth/logout")):
                return httpx.Response(204)
            if path.endswith("/Frames/Login.aspx"):
                return httpx.Response(
                    200, text='<input id="txtSingleCompany" value="T1" />'
                )
            if request.method == "PUT" and path.endswith("/UnitsOfMeasure"):
                uom = json.loads(request.content)["UOM"]["value"]
                time.sleep(0.002)
                with lock:
                    puts.append(uom)
                    sessions.add(session)
                if uom == fail:
                    return httpx.Response(422, json={"message": "bad"})
                return httpx.Response(200, json={"UOM": {"value": uom}})
            return httpx.Response(200, json={"Results": []})  # inquiry probe

        return RealClient(inst, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(cli, "AcumaticaClient", factory)
    return puts, sessions


def test_run_pool_spreads_documents_across_sessions(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V66/gh #71: docs of one multi-doc file run on N sessions, each once."""
    path = tmp_path / "load.yaml"
    path.write_text(_multi_doc(40))
    puts, sessions = _run_pool_tenant(monkeypatch)

    result = CliRunner().invoke(
        cli.cli, ["--output", "json", "run", "--concurrency", "4", str(path)]
    )

    assert result.exit_code == 0, result.output
    assert sorted(puts) == [f"U{i:03}" for i in range(40)]
    assert sessions == {0, 1, 2, 3}
    doc = json.loads(result.stdout)
    assert [s["doc"] for s in doc["scenarios"]] == list(range(1, 41))
    assert doc["counts"]["passed"] == 40


def test_run_pool_inquire_delta_file_stays_serial(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V66: an inquire delta expect keeps the file on one session + info line."""
    path = tmp_path / "load.yaml"
    expect = (
        "expect:\n  - inquire: AccountSummaryInquiry\n    delta: {EndingBalance: 0}\n"
    )
    path.write_text(_multi_doc(6, expect))
    puts, sessions = _run_pool_tenant(monkeypatch)

    result = CliRunner().invoke(cli.cli, ["run", "--concurrency", "3", str(path)])

    assert puts == [f"U{i:03}" for i in range(6)]
    assert sessions == {0}
    assert "inquire delta expect; documents run" in result.stderr


def test_run_pool_step_error_stops_dispatch_lists_not_run(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V66 + gh #58: step error under pool → no new dispatch; rest not run."""
    first, later = tmp_path / "10-load.yaml", tmp_path / "20-next.yaml"
    first.write_text(_multi_doc(100))
    later.write_text(_multi_doc(1))
    puts, _ = _run_pool_tenant(monkeypatch, fail="U000")

    result = CliRunner().invoke(
        cli.cli, ["run", "--concurrency", "2", str(first), str(later)]
    )

    assert result.exit_code == 1
    assert len(puts) < 10  # in-flight finish; nothing new dispatched
    assert f"x {first}#1 [s0]: " in result.stderr
    assert f"not run: {first}#" in result.stderr
    assert f"not run: {later}" in result.stderr
