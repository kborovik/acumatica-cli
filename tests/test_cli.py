"""CLI wiring: exit codes, stream routing, and the top-level error handler.

Pins diff's exit-2-on-drift contract (V9) — no live instance, no SSH.
"""

import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest
import yaml
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import INIT_TEMPLATES, Instance, load_instance
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
endpoint: Bootstrap/1.3.0
key: TermsID
records:
  - TermsID: NET30
"""

SETUP_YAML = """\
action: GenerateCalendar
entity: MasterCalendar
endpoint: Bootstrap/1.3.0
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


@pytest.fixture
def create_env(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """A data repo + monkeypatched chain that records every create step."""
    calls: list[str] = []
    # features.yaml is package-build config: it must reach publish() as the
    # feature list (V2)
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "features.yaml").write_text(FEATURES_YAML)
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
    # the ordered pipeline from docs/ac-exe.md + docs/rest-api.md (T45):
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
    # dataset set (docs/ac-exe.md) - a non-member errors naming the allowed
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
    result = CliRunner().invoke(cli.cli, ["extract", "--help"])

    assert result.exit_code == 0
    assert "inverse of apply" in result.output
    assert "config/" in result.output
    assert "seed_catalog.yaml" in result.output


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
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "features.yaml").write_text(FEATURES_YAML)
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
    # V27/T125: api pin is not env — never emit ACU_API_VERSION
    assert "ACU_API_VERSION" not in result.output
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
    # I.cmd config init / V28/T108: single full seed under config/ + scenario/
    # (T82: project.xml always scaffolded; T113: config/views observer);
    # runs where V3 discovery finds no .env (tmp_path has none up-tree)
    repo = tmp_path / "repo"
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--host", "erp.test", str(repo)]
    )

    assert result.exit_code == 0
    expected = [
        ".env",
        ".gitignore",
        "target.yaml",
        "README.md",
        "config/baseline/10-subaccounts.yaml",
        "config/baseline/20-accounts.yaml",
        "config/baseline/40-ledger.yaml",
        "config/baseline/50-gl-preferences.yaml",
        "config/baseline/60-ledger-company.yaml",
        "config/baseline/90-uoms.yaml",
        "config/bootstrap/company.yaml",
        "config/bootstrap/credit-terms.yaml",
        "config/bootstrap/features.yaml",
        "config/bootstrap/project.xml",
        "config/setup/10-financial-year.yaml",
        "config/setup/20-master-calendar.yaml",
        "config/setup/30-open-periods.yaml",
        "config/master/20-in-preferences.yaml",
        "config/views/10-trial-balance.yaml",
        "scenario/10-seed-capital.yaml",
        "scenario/20-buy.yaml",
        "scenario/30-build.yaml",
        "scenario/40-sell.yaml",
    ]
    for rel in expected:
        assert (repo / rel).is_file(), rel
    assert not (repo / "bootstrap").exists()
    assert not (repo / "master").exists()
    assert not (repo / "snapshot").exists()
    writes = [ln for ln in result.output.splitlines() if ln.startswith("write ")]
    assert len(writes) == len(INIT_TEMPLATES)
    # T81/T82: scaffolded contract is Bootstrap/1.3.0 full company
    assert (
        'name="Bootstrap" version="1.3.0"'
        in (repo / "config" / "bootstrap" / "project.xml").read_text()
    )
    target = (repo / "target.yaml").read_text()
    assert "default_api:" in target
    assert "erp:" in target
    # --host substitutes into ACU_BASE_URL only; ACU_SSH + ACU_API_VERSION
    # omitted (T124/T125)
    env = (repo / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.test/AcumaticaERP" in env
    assert not any(ln.startswith("ACU_SSH=") for ln in env.splitlines())
    assert not any(ln.startswith("ACU_API_VERSION=") for ln in env.splitlines())
    # T104/V28/V33: TB is EndingBalance-class inquire, not roster Account
    tb = (repo / "config" / "views" / "10-trial-balance.yaml").read_text()
    assert "inquire: AccountSummaryInquiry" in tb
    assert "EndingBalance" in tb
    assert "entity: Account" not in tb
    assert "erp.example.com" not in env
    # T107/V33: inventory-summary not golden
    assert not (repo / "config" / "views" / "20-inventory-summary.yaml").exists()


def test_config_init_defaults_to_cwd_with_placeholder_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config init: <dir> optional (cwd), --host optional (placeholder);
    # ACU_SSH line omitted — resolve-time default from base_url host (T124)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["config", "init"])

    assert result.exit_code == 0
    env = (tmp_path / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.example.com/AcumaticaERP" in env
    assert not any(ln.startswith("ACU_SSH=") for ln in env.splitlines())


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

    applied = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
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
    assert "config/master/" in applied.output or "INPreferences" in applied.output
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
        (tmp_path / "config" / "bootstrap" / "features.yaml").read_text()
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


def test_config_init_template_set_is_reference_closed(tmp_path: Path) -> None:
    # V22/V29: org-referencing templates share company AcctCD
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    company = yaml.safe_load(
        (tmp_path / "config" / "bootstrap" / "company.yaml").read_text()
    )
    acct_cd = company["records"][0]["AcctCD"]
    ledger_link = yaml.safe_load(
        (tmp_path / "config" / "baseline" / "60-ledger-company.yaml").read_text()
    )
    assert ledger_link["records"][0]["OrganizationID"] == acct_cd
    open_periods = yaml.safe_load(
        (tmp_path / "config" / "setup" / "30-open-periods.yaml").read_text()
    )
    assert open_periods["record"]["OrganizationID"] == acct_cd
    in_prefs = yaml.safe_load(
        (tmp_path / "config" / "master" / "20-in-preferences.yaml").read_text()
    )
    cash = yaml.safe_load(
        (tmp_path / "config" / "master" / "63-cash-account.yaml").read_text()
    )
    assert in_prefs["records"][0]["TransitBranchID"] == acct_cd
    assert cash["records"][0]["BranchID"] == acct_cd


def test_config_init_prints_next_step_cmds(tmp_path: Path) -> None:
    # V28/T108: post-scaffold stdout names rebuild cmds with config/ + scenario/
    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    assert "next:" in result.output
    assert "acu bootstrap" in result.output
    assert "acu apply config/" in result.output
    assert "acu run scenario/" in result.output
    assert "acu diff config/" in result.output


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


def test_default_seed_dirs_include_master_when_present(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T77: SEED_DIRS order bootstrap, baseline, setup, master — only existing
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

    result = CliRunner().invoke(cli.cli, ["diff"])

    assert result.exit_code == 0
    assert [s.replace("\\", "/") for s in seen] == [
        "bootstrap/x.yaml",
        "baseline/x.yaml",
        "setup/x.yaml",
        "master/x.yaml",
    ]


def test_default_seed_dirs_prefer_config_over_root(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T84/V30: dual layout → only config/<name>/, never merge root trees
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

    result = CliRunner().invoke(cli.cli, ["diff"])

    assert result.exit_code == 0, result.output
    assert seen == ["config/bootstrap/cfg.yaml", "config/baseline/cfg.yaml"]
    assert not any("root.yaml" in s for s in seen)


def test_seed_dirs_exclude_inventory_and_findings() -> None:
    """V35/T130: inventory/ + findings/ are dual-reader trees, never SEED_DIRS."""
    assert "inventory" not in cli.SEED_DIRS
    assert "findings" not in cli.SEED_DIRS
    assert cli.SEED_DIRS == ("bootstrap", "baseline", "setup", "master")


def test_default_seed_dirs_ignore_inventory_and_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V35/T130: bare defaults prefer config/ SEED_DIRS; ignore dual-reader trees."""
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    seed = "entity: UnitsOfMeasure\nkey: UnitID\nrecords:\n- UnitID: HOUR\n"
    (tmp_path / "config" / "baseline").mkdir(parents=True)
    (tmp_path / "config" / "baseline" / "uoms.yaml").write_text(seed)
    # dual-reader trees co-located at data-repo root (engagement output)
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
    monkeypatch.chdir(tmp_path)

    dirs = [str(p).replace("\\", "/") for p in cli.default_seed_dirs()]
    assert dirs == ["config/baseline"]
    assert not any("inventory" in d or "findings" in d for d in dirs)

    expanded = [
        str(p).replace("\\", "/") for p in cli.expand_files(cli.default_seed_dirs())
    ]
    assert expanded == ["config/baseline/uoms.yaml"]
    assert not any("inventory" in p or "findings" in p for p in expanded)


def test_bare_apply_diff_ignore_inventory_findings(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V35/T130: bare apply/diff never load inventory/ or findings/ YAML."""
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
    result = CliRunner().invoke(cli.cli, ["diff"])
    assert result.exit_code == 0, result.output
    assert seen_diff == ["config/baseline/uoms.yaml"]
    assert "inventory" not in result.output
    assert "findings" not in result.output

    seen_apply: list[str] = []
    monkeypatch.setattr(
        cli.seed,
        "apply",
        lambda client, baseline, dry_run=False: (
            seen_apply.append(str(baseline.path).replace("\\", "/")) or 1
        ),
    )
    result = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert seen_apply == ["config/baseline/uoms.yaml"]
    assert "inventory" not in result.output
    assert "findings" not in result.output


def test_only_inventory_findings_not_default_seed_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V35/T130: dual-reader trees alone never satisfy bare apply/diff defaults."""
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=x\n"
    )
    (tmp_path / "inventory").mkdir()
    (tmp_path / "inventory" / "summary.yaml").write_text("tables: {}\n")
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "summary.yaml").write_text("findings: {}\n")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit, match=r"none of the seed directories exist"):
        cli.default_seed_dirs()


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
    assert lines[2].startswith("warn target: no target.yaml under ")
    assert lines[3] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[4] == "ok endpoints (Default/25.200.001 present)"
    assert lines[5] == "ok ssh (Administrator@acu.test)"
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
    assert lines[2] == "skip target (no data root)"
    assert lines[3] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[4] == "ok endpoints (Default/25.200.001 present)"
    assert lines[5] == "ok ssh (user@acu.test)"
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
    assert lines[2].startswith("warn target: no target.yaml under ")
    assert lines[3] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[4] == "ok endpoints (Default/25.200.001 present)"
    assert lines[5] == "skip ssh (ACU_SSH not set)"
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
    # I.cmd config check: the discovery probe is walk-up + parse +
    # ACU_BASE_URL, the primary identity key since T40
    (tmp_path / ".env").write_text("ACU_SSH=Administrator@acu.test\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "fail discovery:" in result.output
    assert "missing required key ACU_BASE_URL (or --url)" in result.output


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


def test_diff_defaults_to_scaffolded_dirs(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T44/I.cmd: FILES omitted -> the existing init-scaffolded dirs at the
    # data-repo root (V3 walk-up), in fixed order
    _seed_repo(tmp_path)
    monkeypatch.chdir(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(
        cli.seed, "diff", lambda client, baseline: seen.append(baseline.path.name) or []
    )

    result = CliRunner().invoke(cli.cli, ["diff"])

    assert result.exit_code == 0
    assert seen == ["terms.yaml", "uoms.yaml", "calendar.yaml"]
    assert "+ no drift on T1 (http://acu.test/AcumaticaERP, 3 file(s))" in result.stderr


def test_bare_diff_without_seed_dirs_errors(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T44/V9: an empty default would make a bare run a silent no-op - the
    # error names the expected dirs, exit 1
    (tmp_path / ".env").write_text("ACU_BASE_URL=http://acu.test/AcumaticaERP\n")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(cli.cli, ["diff"])

    assert result.exit_code == 1
    assert "none of the seed directories exist" in result.output
    assert "bootstrap/, baseline/, setup/, master/" in result.output


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


def test_bare_apply_matches_explicit_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T44/T108: bare `acu apply --dry-run` over a scaffolded repo plans
    # exactly what naming the config/ umbrella plans (V30 prefer config/)
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    bare = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
    explicit = CliRunner().invoke(cli.cli, ["apply", "--dry-run", "config/"])

    assert bare.exit_code == 0
    plans = [line for line in bare.output.splitlines() if "would " in line]
    explicit_plans = [line for line in explicit.output.splitlines() if "would " in line]
    assert plans
    assert plans == explicit_plans


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
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
    result = CliRunner().invoke(cli.cli, ["schema"])

    assert result.exit_code == 0
    assert (tmp_path / "schemas" / "swagger-Default-25.200.001.json").exists()


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
    msg = cli._format_failure(  # pyright: ignore[reportPrivateUsage]
        exc, target="https://erp.example.com/AcumaticaERP"
    )
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
