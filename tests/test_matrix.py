"""Dataset matrix.yaml: load, models, source-merge, config check (V27/T192)."""

from pathlib import Path
from types import TracebackType

import pytest
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import Instance, load_instance
from acumatica_cli.matrix import (
    active_cell,
    assert_matrix_compatible,
    load_matrix,
)
from acumatica_cli.tenant import TenantManager


def _matrix_yaml(
    *,
    cell_id: str = "default",
    erp: str = "26.101.0225",
    default_api: str = "25.200.001",
    base_url: str = "http://acu.test/AcumaticaERP",
    extra_cells: str = "",
) -> str:
    return (
        "cells:\n"
        f'  - id: "{cell_id}"\n'
        f'    erp: "{erp}"\n'
        f'    default_api: "{default_api}"\n'
        f'    base_url: "{base_url}"\n'
        f"{extra_cells}"
    )


class DummyClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.instance: Instance | None = (
            args[0] if args and isinstance(args[0], Instance) else None
        )

    def list_endpoints(self) -> list[tuple[str, str]]:
        return self.entity_root()[0]

    def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
        # default: array shape — no live ERP build (T92 skip path)
        ver = getattr(self.instance, "api_version", "25.200.001")
        return [("Default", ver)], None

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
    )
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_load_matrix_absent_returns_none(data_root: Path) -> None:
    assert load_matrix() is None


def test_load_matrix_match(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101.0225", default_api="25.200.001")
    )
    m = load_matrix()
    assert m is not None
    assert len(m.cells) == 1
    cell = m.cells[0]
    assert cell.id == "default"
    assert cell.erp == "26.101.0225"
    assert cell.default_api == "25.200.001"
    assert cell.base_url == "http://acu.test/AcumaticaERP"


def test_load_matrix_preserves_cell_order(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(
            cell_id="a",
            extra_cells=(
                '  - id: "b"\n'
                '    erp: "26.101.0225"\n'
                '    default_api: "24.200.001"\n'
                '    base_url: "https://b.example/AcumaticaERP"\n'
                '  - id: "c"\n'
                '    erp: "26.101.0225"\n'
                '    default_api: "25.200.001"\n'
                '    base_url: "https://c.example/AcumaticaERP"\n'
            ),
        )
    )
    m = load_matrix()
    assert m is not None
    assert [c.id for c in m.cells] == ["a", "b", "c"]
    assert active_cell(m).id == "a"


def test_load_matrix_duplicate_ids_hard_fail(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(
            cell_id="dup",
            extra_cells=(
                '  - id: "dup"\n'
                '    erp: "26.101.0225"\n'
                '    default_api: "25.200.001"\n'
                '    base_url: "https://other.example/AcumaticaERP"\n'
            ),
        )
    )
    with pytest.raises(SystemExit, match=r"duplicate cell id"):
        load_matrix()


def test_load_matrix_empty_hard_fails(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text("")
    with pytest.raises(SystemExit, match=r"matrix.yaml is empty"):
        load_matrix()


def test_load_matrix_empty_cells_hard_fails(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text("cells: []\n")
    with pytest.raises(SystemExit, match=r"cells"):
        load_matrix()


def test_load_matrix_rejects_default_path(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="Default/25.200.001")
    )
    with pytest.raises(SystemExit, match=r"version half only"):
        load_matrix()


def test_active_cell_unknown_id_names_known(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(
            cell_id="a",
            extra_cells=(
                '  - id: "b"\n'
                '    erp: "26.101.0225"\n'
                '    default_api: "25.200.001"\n'
                '    base_url: "https://b.example/AcumaticaERP"\n'
            ),
        )
    )
    m = load_matrix()
    assert m is not None
    with pytest.raises(
        SystemExit, match=r"unknown matrix cell 'nope'; known ids: a, b"
    ):
        active_cell(m, "nope")


def test_assert_matrix_compatible_invalid_still_fails(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text("")
    with pytest.raises(SystemExit, match=r"matrix.yaml is empty"):
        assert_matrix_compatible(load_instance())


def test_assert_matrix_compatible_present_is_noop_on_versions(
    data_root: Path,
) -> None:
    # T125: source-merge — different default_api no longer mismatch-fails;
    # load_instance already applied default_api as api_version
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="24.200.001")
    )
    inst = load_instance()
    assert inst.api_version == "24.200.001"
    assert_matrix_compatible(inst)  # no SystemExit


def test_assert_matrix_compatible_missing_is_noop(data_root: Path) -> None:
    assert_matrix_compatible(load_instance())


def test_config_check_ok_matrix(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101.0225", default_api="25.200.001")
    )
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    assert (
        "ok matrix (cell=default; "
        "api_version from default_api=25.200.001; erp=26.101.0225 claimed)"
    ) in result.output
    # T92: no build id on DummyClient → still skip after endpoints
    assert "ok endpoints (Default/25.200.001 present)" in result.output
    assert "skip erp (live probe not available; claimed 26.101.0225)" in result.output
    # order: matrix → rest → endpoints → erp (live id needs REST session)
    lines = result.output.splitlines()
    assert lines.index(
        "ok matrix (cell=default; "
        "api_version from default_api=25.200.001; erp=26.101.0225 claimed)"
    ) < lines.index("ok rest (http://acu.test/AcumaticaERP, tenant T1)")
    assert lines.index("ok endpoints (Default/25.200.001 present)") < lines.index(
        "skip erp (live probe not available; claimed 26.101.0225)"
    )


def test_config_check_ok_erp_from_wrapper(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T92: 26.x wrapper build id major.minor-matches claimed erp
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101.0225", default_api="25.200.001")
    )

    class WrapperClient(DummyClient):
        def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
            ver = getattr(self.instance, "api_version", "25.200.001")
            return [("Default", ver)], "26.101.0225"

    monkeypatch.setattr(cli, "AcumaticaClient", WrapperClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    assert "ok erp (26.101.0225 matches claimed 26.101.0225)" in result.output
    assert "skip erp" not in result.output


def test_config_check_erp_major_minor_match(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T76/T92: patch may differ; major.minor is the gate
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="25.200.001")
    )

    class WrapperClient(DummyClient):
        def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
            ver = getattr(self.instance, "api_version", "25.200.001")
            return [("Default", ver)], "26.101.0225"

    monkeypatch.setattr(cli, "AcumaticaClient", WrapperClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    assert "ok erp (26.101.0225 matches claimed 26.101)" in result.output


def test_config_check_erp_mismatch_fails(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="25.200.001", default_api="25.200.001")
    )

    class WrapperClient(DummyClient):
        def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
            ver = getattr(self.instance, "api_version", "25.200.001")
            return [("Default", ver)], "26.101.0225"

    monkeypatch.setattr(cli, "AcumaticaClient", WrapperClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 1
    assert "fail erp: live 26.101.0225 vs claimed 25.200.001" in result.output


def test_config_check_strict_missing_matrix(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check", "--strict"])

    assert result.exit_code == 1
    assert "fail matrix: no matrix.yaml under " in result.output


def test_config_check_matrix_sources_api_version(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T125: non-default default_api is sourced, not a mismatch fail
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="24.200.001")
    )
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(cli.cli, ["config", "check"])

    assert result.exit_code == 0
    assert (
        "ok matrix (cell=default; "
        "api_version from default_api=24.200.001; erp=26.101 claimed)"
    ) in result.output
    assert "ok endpoints (Default/24.200.001 present)" in result.output


def test_config_check_flag_override_notes_source(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T125: --api-version ad-hoc override still ok; line notes flag source
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="25.200.001")
    )
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)

    result = CliRunner().invoke(
        cli.cli, ["--api-version", "24.200.001", "config", "check"]
    )

    assert result.exit_code == 0
    assert (
        "ok matrix (cell=default; "
        "api_version=24.200.001 from --api-version; "
        "default_api=25.200.001; erp=26.101 claimed)"
    ) in result.output


def test_apply_sources_matrix_api_version(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # T125: apply no longer dual-source mismatch-fails; uses matrix pin
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="24.200.001")
    )
    (data_root / "baseline").mkdir()
    (data_root / "baseline" / "uom.yaml").write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nrecords:\n  - UOM: KG\n"
    )
    seen: list[str] = []

    class TrackingClient(DummyClient):
        def __enter__(self) -> TrackingClient:
            inst = self.instance
            assert inst is not None
            seen.append(inst.api_version)
            return self

        def put(self, *args: object, **kwargs: object) -> dict[str, object]:
            return {}

        def get(self, *args: object, **kwargs: object) -> list[object]:
            return []

    monkeypatch.setattr(cli, "AcumaticaClient", TrackingClient)

    result = CliRunner().invoke(cli.cli, ["apply", "baseline/uom.yaml"])

    # may fail for other reasons after gate; gate itself must not mismatch
    assert "Default API version mismatch" not in result.output
    assert seen == ["24.200.001"]


def test_config_show_surfaces_matrix(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101.0225", default_api="25.200.001")
    )
    result = CliRunner().invoke(cli.cli, ["config", "show"])

    assert result.exit_code == 0
    assert (
        "# matrix.yaml cell=default: erp=26.101.0225 "
        "default_api=25.200.001 base_url=http://acu.test/AcumaticaERP"
    ) in result.output
    assert (
        "# api_version=25.200.001 (from matrix cell default default_api)"
    ) in result.output
    assert "ACU_API_VERSION" not in result.output


def test_config_show_notes_flag_override(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        _matrix_yaml(erp="26.101", default_api="25.200.001")
    )
    result = CliRunner().invoke(
        cli.cli, ["--api-version", "24.200.001", "config", "show"]
    )

    assert result.exit_code == 0
    assert (
        "# api_version=24.200.001 (from --api-version; "
        "matrix cell default default_api=25.200.001)"
    ) in result.output
    assert "ACU_API_VERSION" not in result.output
