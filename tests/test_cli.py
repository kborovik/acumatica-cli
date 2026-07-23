"""CLI wiring: exit codes, stream routing, and the top-level error handler.

Pins diff's exit-2-on-drift contract (V9) — no live instance, no SSH.
"""

import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import Instance, load_instance
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
endpoint: Bootstrap/1.0.0
key: TermsID
records:
  - TermsID: NET30
"""

SETUP_YAML = """\
action: GenerateCalendar
entity: MasterCalendar
endpoint: Bootstrap/1.0.0
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
        return [("Default", self.instance.api_version)]

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
    assert "http://acu.test/AcumaticaERP" in result.output
    assert "Company" in result.output


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
    # whole init + bootstrap chain, not just the recycle
    result = CliRunner().invoke(
        cli.cli, ["tenant", "create", "--id", "3", "--login", "Scratch", "--no-init"]
    )

    assert result.exit_code == 0
    assert create_env == ["create"]
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
    assert create_env == [
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


def test_provision_cmd_is_gone(wired: Instance) -> None:
    # T45: tenant create chains the bootstrap publish itself; the separate
    # provision command must not exist
    result = CliRunner().invoke(cli.cli, ["provision"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_bootstrap_publishes_without_ssh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T68/I.cmd: hosted path — ACU_SSH unset; publish is pure REST; recycle
    # is skipped with a warning (feature slot may stay stale until restart)
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
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
    # I.cmd config init: 16-file template set into a created-if-absent dir
    # (T82: bootstrap/project.xml always scaffolded); runs where V3
    # discovery finds no .env (tmp_path has none up-tree)
    repo = tmp_path / "repo"
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--host", "erp.test", str(repo)]
    )

    assert result.exit_code == 0
    expected = [
        ".env",
        ".gitignore",
        "target.yaml",
        "baseline/10-subaccounts.yaml",
        "baseline/20-accounts.yaml",
        "baseline/40-ledger.yaml",
        "baseline/50-gl-preferences.yaml",
        "baseline/60-ledger-company.yaml",
        "baseline/90-uoms.yaml",
        "bootstrap/company.yaml",
        "bootstrap/credit-terms.yaml",
        "bootstrap/features.yaml",
        "bootstrap/project.xml",
        "setup/10-financial-year.yaml",
        "setup/20-master-calendar.yaml",
        "setup/30-open-periods.yaml",
    ]
    for rel in expected:
        assert (repo / rel).is_file(), rel
    assert (
        len([ln for ln in result.output.splitlines() if ln.startswith("write ")]) == 16
    )
    # T81/T82: scaffolded contract is Bootstrap/1.0.0 full company
    assert (
        'name="Bootstrap" version="1.0.0"'
        in (repo / "bootstrap" / "project.xml").read_text()
    )
    target = (repo / "target.yaml").read_text()
    assert "default_api:" in target
    assert "erp:" in target
    # --host substitutes into both scaffolded address values (I.cmd config init)
    env = (repo / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.test/AcumaticaERP" in env
    assert "ACU_SSH=Administrator@erp.test" in env
    assert "erp.example.com" not in env


def test_config_init_defaults_to_cwd_with_placeholder_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config init: <dir> optional (cwd), --host optional (placeholder)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["config", "init"])

    assert result.exit_code == 0
    env = (tmp_path / ".env").read_text()
    assert "ACU_BASE_URL=http://erp.example.com/AcumaticaERP" in env
    assert "ACU_SSH=Administrator@erp.example.com" in env


def test_config_init_rerun_skips_and_never_overwrites(tmp_path: Path) -> None:
    # I.cmd config init: per-file skip-if-exists - `skip <file> (exists)`,
    # exit 0, zero mutations; next-step banner still prints after skips
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])
    (tmp_path / ".env").write_text("ACU_BASE_URL=http://hand.edited/X\n")

    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    skip_lines = [ln for ln in result.output.splitlines() if ln.startswith("skip ")]
    assert len(skip_lines) == 16
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


def test_config_init_scaffold_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T26 verify: empty dir -> init --host -> config show succeeds and
    # apply --dry-run parses every seed template (features.yaml skipped);
    # T39 extends the round-trip over setup/ action files
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    shown = CliRunner().invoke(cli.cli, ["config", "show"])
    assert shown.exit_code == 0
    assert "ACU_BASE_URL=http://erp.test/AcumaticaERP" in shown.output

    applied = CliRunner().invoke(
        cli.cli, ["apply", "--dry-run", "bootstrap", "baseline", "setup"]
    )
    assert applied.exit_code == 0
    assert "would PUT Company [COMPANY]" in applied.output
    assert "would PUT CreditTerms [NET30]" in applied.output
    assert "would PUT Subaccount [000000]" in applied.output
    assert "would PUT Account [32000]" in applied.output
    assert "would PUT Account [33000]" in applied.output
    assert "would PUT Ledger [ACTUAL]" in applied.output
    assert "would PUT GLPreferences [32000]" in applied.output
    assert "would PUT LedgerCompany [ACTUAL, COMPANY]" in applied.output
    assert "would PUT UnitsOfMeasure [HOUR]" in applied.output
    assert "would invoke GeneratePeriods" in applied.output
    assert "would invoke GenerateCalendar" in applied.output
    assert "would invoke ProcessAll" in applied.output
    assert applied.output.count("(dry run)") == 11
    # V22: numbered prefixes encode apply order - subaccounts before
    # accounts before ledger before GL preferences (which references
    # accounts 32000/33000) before the org-ledger link, uoms last; the
    # setup/ action chain follows the whole baseline (financial year
    # before calendar generation before period activation)
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
    # V22 feature closure (B15): the scaffolded features.yaml must enable
    # every feature the shipped baseline templates require - the Subaccount
    # template PUTs against feature-gated GL203000, so SubAccount must be
    # in the list or a scaffolded apply 403s at the first baseline file
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    features = yaml.safe_load((tmp_path / "bootstrap" / "features.yaml").read_text())
    assert "SubAccount" in features
    # the built-in six stay - dropping one starves bootstrap itself
    for name in [
        "FinancialModule",
        "FinancialStandard",
        "DistributionModule",
        "Inventory",
        "Branch",
        "MultiCompany",
    ]:
        assert name in features


def test_config_init_template_set_is_reference_closed(tmp_path: Path) -> None:
    # V22 reference closure (B16 sibling of B15): every OrganizationID an
    # org-referencing template carries must be the organization the shipped
    # set itself creates - bootstrap/company.yaml's AcctCD - or a scaffolded
    # apply 422s on an org that does not exist
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    company = yaml.safe_load((tmp_path / "bootstrap" / "company.yaml").read_text())
    acct_cd = company["records"][0]["AcctCD"]
    ledger_link = yaml.safe_load(
        (tmp_path / "baseline" / "60-ledger-company.yaml").read_text()
    )
    assert ledger_link["records"][0]["OrganizationID"] == acct_cd
    open_periods = yaml.safe_load(
        (tmp_path / "setup" / "30-open-periods.yaml").read_text()
    )
    assert open_periods["record"]["OrganizationID"] == acct_cd


def test_config_init_prints_next_step_cmds(tmp_path: Path) -> None:
    # T77/I.cmd: post-scaffold stdout names rebuild cmds (bootstrap/apply/diff)
    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    assert "next:" in result.output
    assert "acu bootstrap" in result.output
    assert "acu apply" in result.output
    assert "acu diff" in result.output


def test_config_init_unknown_flavor_rejected(tmp_path: Path) -> None:
    # V16: flavor is click.Choice over INIT_FLAVORS — free string never accepted
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--flavor", "nope", str(tmp_path)]
    )

    assert result.exit_code != 0
    assert "Invalid value for '--flavor'" in result.output


def test_config_init_flavor_distribution_scaffolds_demo_seed(tmp_path: Path) -> None:
    # T77/T78/T82 V28: --flavor distribution ships master + scenario + README;
    # both flavors share Bootstrap/1.0.0 project.xml (not a second identity)
    result = CliRunner().invoke(
        cli.cli,
        [
            "config",
            "init",
            "--flavor",
            "distribution",
            "--host",
            "erp.test",
            str(tmp_path),
        ],
    )

    assert result.exit_code == 0
    assert (tmp_path / "bootstrap" / "project.xml").is_file()
    assert (
        'name="Bootstrap" version="1.0.0"'
        in (tmp_path / "bootstrap" / "project.xml").read_text()
    )
    assert (tmp_path / "master" / "20-in-preferences.yaml").is_file()
    assert (tmp_path / "scenario" / "buy-sell.yaml").is_file()
    assert (tmp_path / "README.md").is_file()
    assert "acu run scenario/" in result.output
    writes = [ln for ln in result.output.splitlines() if ln.startswith("write ")]
    assert len(writes) > 16
    # finance-minimal shares project.xml; never gains master/ or scenario/
    bare = tmp_path / "bare"
    CliRunner().invoke(cli.cli, ["config", "init", str(bare)])
    assert not (bare / "master").exists()
    assert (bare / "bootstrap" / "project.xml").is_file()
    assert (
        'name="Bootstrap" version="1.0.0"'
        in (bare / "bootstrap" / "project.xml").read_text()
    )
    assert not (bare / "scenario").exists()


def test_config_init_distribution_org_cd_consistent(tmp_path: Path) -> None:
    # V29: single org-CD placeholder across company, ledger link, periods,
    # inventory transit branch, cash BranchID
    CliRunner().invoke(
        cli.cli, ["config", "init", "--flavor", "distribution", str(tmp_path)]
    )

    company = yaml.safe_load((tmp_path / "bootstrap" / "company.yaml").read_text())
    org = company["records"][0]["AcctCD"]
    ledger = yaml.safe_load(
        (tmp_path / "baseline" / "60-ledger-company.yaml").read_text()
    )
    periods = yaml.safe_load((tmp_path / "setup" / "30-open-periods.yaml").read_text())
    in_prefs = yaml.safe_load(
        (tmp_path / "master" / "20-in-preferences.yaml").read_text()
    )
    cash = yaml.safe_load((tmp_path / "master" / "63-cash-account.yaml").read_text())
    assert ledger["records"][0]["OrganizationID"] == org
    assert periods["record"]["OrganizationID"] == org
    assert in_prefs["records"][0]["TransitBranchID"] == org
    assert cash["records"][0]["BranchID"] == org


def test_config_init_distribution_feature_closed(tmp_path: Path) -> None:
    # V22 feature closure for distribution masters (Warehouse, kits, …)
    CliRunner().invoke(
        cli.cli, ["config", "init", "--flavor", "distribution", str(tmp_path)]
    )
    features = yaml.safe_load((tmp_path / "bootstrap" / "features.yaml").read_text())
    for name in [
        "Inventory",
        "DistributionModule",
        "Warehouse",
        "WarehouseLocation",
        "KitAssemblies",
        "SubAccount",
    ]:
        assert name in features


def test_config_init_distribution_dry_run_parses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T78: every packaged seed file loads; bare apply default includes master/
    CliRunner().invoke(
        cli.cli,
        [
            "config",
            "init",
            "--flavor",
            "distribution",
            "--host",
            "erp.test",
            str(tmp_path),
        ],
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    applied = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
    assert applied.exit_code == 0, applied.output
    assert "would PUT Company" in applied.output
    assert "would PUT Warehouse" in applied.output or "Warehouse" in applied.output
    assert "master/" in applied.output or any(
        "INPreferences" in ln or "Warehouse" in ln for ln in applied.output.splitlines()
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


def test_config_check_skips_ssh_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T67/I.cmd: ACU_SSH optional — unset → skip ssh line, never fail; REST
    # still probed; exit 0 when non-skipped probes pass
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
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
    # T67/I.cmd: tenant cmds hard-error naming ACU_SSH before any remote
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=secret\n"
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
    # T44 verify leg, offline: bare `acu apply --dry-run` over a scaffolded
    # repo plans exactly what naming the three dirs plans
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    bare = CliRunner().invoke(cli.cli, ["apply", "--dry-run"])
    explicit = CliRunner().invoke(
        cli.cli, ["apply", "--dry-run", "bootstrap", "baseline", "setup"]
    )

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
