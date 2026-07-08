"""CLI wiring: exit codes, stream routing, and the top-level error handler.

Pins diff's exit-1-on-drift contract — no live instance, no SSH.
"""

import sys
from pathlib import Path
from types import TracebackType
from typing import Any

import pytest
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import Instance
from acumatica_cli.tenant import Tenant, TenantManager

BASELINE = """\
entity: UnitsOfMeasure
key: UOM
records:
  - UOM: KG
    Description: Kilogram
"""


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
    monkeypatch.setattr(cli, "load_instance", lambda name: instance)
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    return instance


def _baseline(tmp_path: Path) -> Path:
    path = tmp_path / "uoms.yaml"
    path.write_text(BASELINE)
    return path


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
    assert "Tenants on test" in result.output
    assert "Company" in result.output


def test_bootstrap_publishes_and_reports_endpoint(
    wired: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []

    def fake_publish(client: Any, **kwargs: Any) -> str:
        seen.append(client)
        return "published"

    monkeypatch.setattr(cli.bootstrap, "publish", fake_publish)
    result = CliRunner().invoke(cli.cli, ["bootstrap"])

    assert result.exit_code == 0
    assert len(seen) == 1
    assert "✓ acu-bootstrap published on test/T1 (endpoint Bootstrap/1.0.0)" in (
        result.stderr
    )


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
    assert "✓ no drift on test/T1 (1 file(s))" in result.stderr


def test_diff_directory_expands_to_yaml_files(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "uoms.yaml").write_text(BASELINE)
    (tmp_path / "terms.yaml").write_text(BASELINE)
    (tmp_path / "notes.txt").write_text("not a baseline")
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [])
    result = CliRunner().invoke(cli.cli, ["diff", str(tmp_path)])

    assert result.exit_code == 0
    assert "✓ no drift on test/T1 (2 file(s))" in result.stderr


def test_apply_empty_directory_errors(wired: Instance, tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["apply", str(tmp_path)])

    assert result.exit_code == 1
    assert "no *.yaml files in directory" in result.output


def test_diff_drift_exits_one_with_lines_on_stdout(
    wired: Instance, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drift = "UnitsOfMeasure [KG].Description: source='Kilogram' live='kg'"
    monkeypatch.setattr(cli.seed, "diff", lambda client, baseline: [drift])
    result = CliRunner().invoke(cli.cli, ["diff", str(_baseline(tmp_path))])

    # the load-bearing contract: drift -> exit 1
    assert result.exit_code == 1
    assert "✗ DRIFT on test/T1:" in result.stderr
    assert drift in result.output


def test_schema_writes_swagger_to_out_dir(wired: Instance, tmp_path: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["schema", "-o", str(tmp_path / "dump")])

    out_file = tmp_path / "dump" / "swagger-Default-25.200.001.json"
    assert result.exit_code == 0
    assert out_file.read_bytes() == SWAGGER
    assert f"{out_file} ({len(SWAGGER)} bytes)" in result.output


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
    assert "✗ remote command failed (255)" in capsys.readouterr().err


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
