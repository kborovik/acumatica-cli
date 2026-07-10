"""CLI wiring: exit codes, stream routing, and the top-level error handler.

Pins diff's exit-2-on-drift contract (V9) — no live instance, no SSH.
"""

import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
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
endpoint: Bootstrap/1.3.0
key: TermsID
records:
  - TermsID: NET30
"""

FEATURES_YAML = "- MultiCompany\n- Multicurrency\n"


SWAGGER = b'{"openapi": "3.0.1"}'


class DummyClient:
    """Stands in for AcumaticaClient where no HTTP call should happen."""

    def __init__(self, instance: Instance, **kwargs: Any):
        self.instance = instance

    def swagger(self) -> bytes:
        return SWAGGER

    def __enter__(self) -> "DummyClient":
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
    monkeypatch.setattr(cli, "load_instance", lambda host=None: instance)
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
    assert "Tenants on acu.test" in result.output
    assert "Company" in result.output


@pytest.fixture
def provision_env(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """A data repo + monkeypatched chain that records every provisioning step."""
    calls: list[str] = []
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "credit-terms.yaml").write_text(BOOTSTRAP_YAML)
    # features.yaml is package-build config: it must reach publish() as the
    # feature list (V2) and never enter the apply/diff seed sweep
    (tmp_path / "bootstrap" / "features.yaml").write_text(FEATURES_YAML)
    (tmp_path / "baseline").mkdir()
    (tmp_path / "baseline" / "uoms.yaml").write_text(BASELINE)
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path)
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
            calls.append("publish:" + ",".join(k["features"])) or "published"
        ),
    )
    monkeypatch.setattr(
        cli.seed,
        "apply",
        lambda client, baseline, dry_run=False: (
            calls.append(f"apply:{baseline.path.name}") or len(baseline.records)
        ),
    )
    monkeypatch.setattr(
        cli.seed,
        "diff",
        lambda client, baseline: calls.append(f"diff:{baseline.path.name}") or [],
    )
    return calls


def test_provision_chains_create_bootstrap_apply_diff(provision_env: list[str]) -> None:
    result = CliRunner().invoke(
        cli.cli, ["provision", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 0
    # the ordered pipeline from docs/rest-api.md, bootstrap YAML before
    # baseline; the post-publish recycle reloads the feature slot (the
    # publish's own restart caches it before the plugin's insert commits);
    # the drift check covers everything applied, bootstrap YAML included
    # (a PUT that answers 200 can persist nothing - T3)
    assert provision_env == [
        "create",
        "recycle",
        "init:Scratch",
        "publish:MultiCompany,Multicurrency",
        "recycle",
        "init:Scratch",
        "apply:credit-terms.yaml",
        "apply:uoms.yaml",
        "diff:credit-terms.yaml",
        "diff:uoms.yaml",
    ]
    # every session targets the provisioned tenant, not the config default
    assert "+ no drift on acu.test/Scratch (2 file(s))" in result.stderr


def test_provision_skips_create_when_tenant_exists(
    provision_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = Tenant(
        company_id=3, company_cd="Company3", login_name="Scratch", company_type="Custom"
    )
    monkeypatch.setattr(TenantManager, "list", lambda self: [tenant])
    result = CliRunner().invoke(
        cli.cli, ["provision", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 0
    assert provision_env == [
        "publish:MultiCompany,Multicurrency",
        "recycle",
        "init:Scratch",
        "apply:credit-terms.yaml",
        "apply:uoms.yaml",
        "diff:credit-terms.yaml",
        "diff:uoms.yaml",
    ]
    assert "skipping create" in result.stderr


def test_provision_recycles_even_when_already_published(
    provision_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = Tenant(
        company_id=3, company_cd="Company3", login_name="Scratch", company_type="Custom"
    )
    monkeypatch.setattr(TenantManager, "list", lambda self: [tenant])
    monkeypatch.setattr(
        cli.bootstrap,
        "publish",
        lambda client, **k: provision_env.append("publish") or "already published",
    )
    result = CliRunner().invoke(
        cli.cli, ["provision", "--id", "3", "--login", "Scratch"]
    )

    # resume path: the recycle stays — a publish interrupted before its
    # recycle would otherwise leave the feature slot cached pre-plugin
    assert result.exit_code == 0
    assert provision_env == [
        "publish",
        "recycle",
        "init:Scratch",
        "apply:credit-terms.yaml",
        "apply:uoms.yaml",
        "diff:credit-terms.yaml",
        "diff:uoms.yaml",
    ]


def test_provision_drift_exits_two(
    provision_env: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    drift = "UnitsOfMeasure [KG].Description: source='Kilogram' live='kg'"
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [drift])
    result = CliRunner().invoke(
        cli.cli, ["provision", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 2
    assert "x DRIFT on acu.test/Scratch:" in result.stderr
    assert drift in result.output


def test_provision_requires_a_baseline_dir(
    provision_env: list[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "data_root", lambda: tmp_path / "empty")
    result = CliRunner().invoke(
        cli.cli, ["provision", "--id", "3", "--login", "Scratch"]
    )

    assert result.exit_code == 1
    assert "nothing to provision" in result.output
    assert provision_env == []


def test_bootstrap_cmd_is_gone(wired: Instance) -> None:
    # T8: bootstrap.publish() stays a module; resumable provision is the
    # recovery route — the standalone command must not exist
    result = CliRunner().invoke(cli.cli, ["bootstrap"])

    assert result.exit_code != 0
    assert "No such command" in result.output


def test_config_show_emits_yaml_without_credentials(wired: Instance) -> None:
    # I.cfg: same load_instance path as live cmds, credentials never printed
    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert "host: acu.test" in result.output
    assert "base_url: http://acu.test/AcumaticaERP" in result.output
    assert "ssh: user@acu.test" in result.output
    assert "password" not in result.output
    assert "username" not in result.output
    assert "pw" not in result.output


def test_config_show_round_trips_through_load_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cfg: `acu config show > acu.yaml` is a valid config - reloading it
    # resolves to the identical instance (the whole point of the YAML emit)
    (tmp_path / "acu.yaml").write_text("host: acu.test\ntenant: T1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.delenv("ACU_USER", raising=False)
    original = load_instance()

    result = CliRunner().invoke(cli.cli, ["config", "show"])
    assert result.exit_code == 0

    regenerated = tmp_path / "regenerated"
    regenerated.mkdir()
    (regenerated / "acu.yaml").write_text(result.output)
    monkeypatch.chdir(regenerated)
    assert load_instance() == original


def test_config_init_scaffolds_data_repo(tmp_path: Path) -> None:
    # I.cmd config init: 7-file template set into a created-if-absent dir;
    # runs where V3 discovery finds no acu.yaml (tmp_path has none up-tree)
    repo = tmp_path / "repo"
    result = CliRunner().invoke(
        cli.cli, ["config", "init", "--host", "erp.test", str(repo)]
    )

    assert result.exit_code == 0
    expected = [
        "acu.yaml",
        ".env",
        ".gitignore",
        "baseline/uoms.yaml",
        "bootstrap/company.yaml",
        "bootstrap/credit-terms.yaml",
        "bootstrap/features.yaml",
    ]
    for rel in expected:
        assert (repo / rel).is_file(), rel
    assert (
        len([ln for ln in result.output.splitlines() if ln.startswith("write ")]) == 7
    )
    acu_yaml = (repo / "acu.yaml").read_text()
    assert "host: erp.test" in acu_yaml
    assert "erp.example.com" not in acu_yaml


def test_config_init_defaults_to_cwd_with_placeholder_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config init: <dir> optional (cwd), --host optional (placeholder)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["config", "init"])

    assert result.exit_code == 0
    assert "host: erp.example.com" in (tmp_path / "acu.yaml").read_text()


def test_config_init_rerun_skips_and_never_overwrites(tmp_path: Path) -> None:
    # I.cmd config init: per-file skip-if-exists - `skip <file> (exists)`,
    # exit 0, zero mutations
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])
    (tmp_path / "acu.yaml").write_text("host: hand.edited\n")

    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert len(lines) == 7
    assert all(ln.startswith("skip ") and ln.endswith(" (exists)") for ln in lines)
    assert (tmp_path / "acu.yaml").read_text() == "host: hand.edited\n"


def test_config_init_writes_no_secrets(tmp_path: Path) -> None:
    # V2: .env = placeholder credentials only; acu.yaml = where, never
    # secrets; .env kept out of git by the scaffolded .gitignore
    CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])

    env = (tmp_path / ".env").read_text()
    assert "ACU_USER=admin" in env
    assert "ACU_PASSWORD=\n" in env
    gitignore = (tmp_path / ".gitignore").read_text()
    assert ".env" in gitignore
    assert "schemas/" in gitignore
    # the acu.yaml comment may NAME the env vars; no credential keys allowed
    acu_yaml = (tmp_path / "acu.yaml").read_text()
    assert "password:" not in acu_yaml
    assert "username:" not in acu_yaml


def test_config_init_scaffold_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T26 verify: empty dir -> init --host -> config show succeeds and
    # apply --dry-run parses every seed template (features.yaml skipped)
    CliRunner().invoke(cli.cli, ["config", "init", "--host", "erp.test", str(tmp_path)])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)

    shown = CliRunner().invoke(cli.cli, ["config", "show"])
    assert shown.exit_code == 0
    assert "host: erp.test" in shown.output

    applied = CliRunner().invoke(
        cli.cli, ["apply", "--dry-run", "bootstrap", "baseline"]
    )
    assert applied.exit_code == 0
    assert "would PUT Company [COMPANY]" in applied.output
    assert "would PUT CreditTerms [NET30]" in applied.output
    assert "would PUT UnitsOfMeasure [HOUR]" in applied.output
    assert applied.output.count("(dry run)") == 3


@pytest.fixture
def check_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A real data repo for config check: acu.yaml + .env, password in .env only.

    load_dotenv mutates os.environ in place; the setenv-then-delenv pair makes
    monkeypatch record the pre-test state so the .env-sourced value never
    leaks into later tests.
    """
    (tmp_path / "acu.yaml").write_text("host: acu.test\ntenant: T1\n")
    (tmp_path / ".env").write_text("ACU_PASSWORD=secret\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "shadow")
    monkeypatch.delenv("ACU_PASSWORD")
    monkeypatch.delenv("ACU_USER", raising=False)
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
        def __enter__(self) -> "RecordingClient":
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
    assert lines[0].endswith("acu.yaml)")
    assert lines[1] == "ok secrets (ACU_PASSWORD set)"
    assert lines[2] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[3] == "ok ssh (Administrator@acu.test)"
    assert calls == ["enter", "exit", "ping"]


def test_config_check_discovery_fail_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3: no acu.yaml up-tree -> fail discovery, exit 1, later probes never run
    probes: list[str] = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "AcumaticaClient", lambda *a, **k: probes.append("rest"))
    monkeypatch.setattr(TenantManager, "ping", lambda self: probes.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("fail discovery: acu.yaml not found")
    assert probes == []


def test_config_check_secrets_fail_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd config check: discovery/secrets fail stops - no live probes run
    (tmp_path / "acu.yaml").write_text("host: acu.test\ntenant: T1\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACU_PASSWORD", raising=False)
    probes: list[str] = []
    monkeypatch.setattr(cli, "AcumaticaClient", lambda *a, **k: probes.append("rest"))
    monkeypatch.setattr(TenantManager, "ping", lambda self: probes.append("ping"))

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    lines = result.output.splitlines()
    assert lines[0].startswith("ok discovery (")
    assert lines[1].startswith("fail secrets: ACU_PASSWORD not set")
    assert len(lines) == 2
    assert probes == []


def test_config_check_rest_fail_still_probes_ssh(
    check_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T27 verify: wrong password -> REST fail while ssh still reports, exit 1;
    # a tenant-guard refusal (V5) surfaces the same way - __enter__ raises
    class RefusingClient(DummyClient):
        def __enter__(self) -> "RefusingClient":
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


def test_global_host_flag_rederives_urls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd: --host swaps the acu.yaml host before the Instance is built,
    # so base_url/ssh re-derive; resolution runs through the same
    # load_instance path config show prints (I.cfg)
    (tmp_path / "acu.yaml").write_text("host: acu.test\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    monkeypatch.delenv("ACU_USER", raising=False)

    result = CliRunner().invoke(cli.cli, ["--host", "edge.example", "config", "show"])

    assert result.exit_code == 0
    assert "host: edge.example" in result.output
    assert "base_url: http://edge.example/AcumaticaERP" in result.output
    assert "ssh: Administrator@edge.example" in result.output


def test_global_host_flag_rejected_after_subcommand(wired: Instance) -> None:
    # V16: globals valid only before the subcommand
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
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(_baseline(tmp_path))])

    assert result.exit_code == 0
    assert "+ no drift on acu.test/T1 (1 file(s))" in result.stderr


def test_diff_directory_expands_to_yaml_files(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uoms.yaml").write_text(BASELINE)
    (tmp_path / "terms.yaml").write_text(BASELINE)
    (tmp_path / "notes.txt").write_text("not a baseline")
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ no drift on acu.test/T1 (2 file(s))" in result.stderr


def test_directory_expansion_skips_features_yaml(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """features.yaml is package-build config (I.data), never a seed file."""
    (tmp_path / "uoms.yaml").write_text(BASELINE)
    (tmp_path / "features.yaml").write_text(FEATURES_YAML)
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(tmp_path)])

    assert result.exit_code == 0
    assert "+ no drift on acu.test/T1 (1 file(s))" in result.stderr


def test_apply_empty_directory_errors(wired: Instance, tmp_path: Path) -> None:
    # a lone features.yaml does not make a directory seedable either
    (tmp_path / "features.yaml").write_text(FEATURES_YAML)
    result = CliRunner().invoke(cli.cli, ["apply", str(tmp_path)])

    assert result.exit_code == 1
    assert "no seed *.yaml files in directory" in result.output


def test_diff_drift_exits_two_with_lines_on_stdout(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drift = "UnitsOfMeasure [KG].Description: source='Kilogram' live='kg'"
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [drift])
    result = CliRunner().invoke(cli.cli, ["diff", str(_baseline(tmp_path))])

    # the load-bearing contract (V9): exit 0 ok, 1 error, 2 drift
    assert result.exit_code == 2
    assert "x DRIFT on acu.test/T1:" in result.stderr
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
