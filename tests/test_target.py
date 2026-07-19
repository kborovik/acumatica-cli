"""Dataset target.yaml: load, gate, config check warn/strict (V27)."""

from pathlib import Path
from types import TracebackType

import pytest
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import load_instance
from acumatica_cli.target import assert_target_compatible, load_target
from acumatica_cli.tenant import TenantManager


class DummyClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.instance = args[0] if args else None

    def list_endpoints(self) -> list[tuple[str, str]]:
        ver = getattr(self.instance, "api_version", "25.200.001")
        return [("Default", ver)]

    def __enter__(self) -> DummyClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        return None


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
        "ACU_API_VERSION=25.200.001\n"
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_target_absent_returns_none(data_root: Path) -> None:
    assert load_target() is None


def test_load_target_match(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101.0225"\ndefault_api: "25.200.001"\n'
    )
    t = load_target()
    assert t is not None
    assert t.erp == "26.101.0225"
    assert t.default_api == "25.200.001"


def test_load_target_empty_hard_fails(data_root: Path) -> None:
    (data_root / "target.yaml").write_text("")
    with pytest.raises(SystemExit, match=r"target.yaml is empty"):
        load_target()


def test_load_target_rejects_default_path(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "Default/25.200.001"\n'
    )
    with pytest.raises(SystemExit, match=r"version half only"):
        load_target()


def test_assert_target_compatible_mismatch(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "24.200.001"\n'
    )
    inst = load_instance()
    with pytest.raises(SystemExit, match=r"Default API version mismatch"):
        assert_target_compatible(inst)


def test_assert_target_compatible_match(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "25.200.001"\n'
    )
    assert_target_compatible(load_instance())


def test_assert_target_compatible_missing_is_noop(data_root: Path) -> None:
    assert_target_compatible(load_instance())


def test_config_check_ok_target(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101.0225"\ndefault_api: "25.200.001"\n'
    )
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    assert (
        "ok target (default_api=25.200.001 matches configured; "
        "erp=26.101.0225 claimed)"
    ) in result.output


def test_config_check_strict_missing_target(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check", "--strict"])

    assert result.exit_code == 1
    assert "fail target: no target.yaml under " in result.output


def test_config_check_mismatch_fails(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "24.200.001"\n'
    )
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "fail target: dataset default_api=24.200.001" in result.output


def test_apply_gates_on_target_mismatch(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "24.200.001"\n'
    )
    (data_root / "baseline").mkdir()
    (data_root / "baseline" / "uom.yaml").write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n"
    )
    entered: list[str] = []

    class TrackingClient(DummyClient):
        def __enter__(self) -> TrackingClient:
            entered.append("enter")
            return self

    monkeypatch.setattr(cli, "AcumaticaClient", TrackingClient)

    result = CliRunner().invoke(cli.cli, ["apply", "baseline/uom.yaml"])

    assert result.exit_code != 0
    assert "Default API version mismatch" in result.output
    assert entered == []  # gate before HTTP


def test_config_show_surfaces_target(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101.0225"\ndefault_api: "25.200.001"\n'
    )
    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert "# target.yaml: erp=26.101.0225 default_api=25.200.001" in result.output


def test_config_show_notes_mismatch_still_ok(data_root: Path) -> None:
    (data_root / "target.yaml").write_text(
        'erp: "26.101"\ndefault_api: "24.200.001"\n'
    )
    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert "# warn: default_api=24.200.001" in result.output
