"""V27: leftover matrix.yaml ignored; --cell gone; check is single-instance."""

import importlib
from pathlib import Path
from types import TracebackType

import pytest
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.config import Instance, load_instance
from acumatica_cli.tenant import TenantManager


class DummyClient:
    def __init__(self, *args: object, **kwargs: object) -> None:
        self.instance: Instance | None = (
            args[0] if args and isinstance(args[0], Instance) else None
        )

    def list_endpoints(self) -> list[tuple[str, str]]:
        return self.entity_root()[0]

    def entity_root(self) -> tuple[list[tuple[str, str]], str | None]:
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


def test_matrix_module_is_gone() -> None:
    """T218/V27: no matrix loader — package has no acumatica_cli.matrix."""
    with pytest.raises(ModuleNotFoundError, match=r"acumatica_cli\.matrix"):
        importlib.import_module("acumatica_cli.matrix")


def test_cell_flag_is_gone(data_root: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["--cell", "x", "config", "show"])
    assert result.exit_code != 0
    assert "No such option" in result.output


def test_check_all_flag_is_gone(data_root: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["check", "--all", "--yes", "--tenant", "T1"])
    assert result.exit_code != 0
    assert "No such option" in result.output
    help_result = CliRunner().invoke(cli.cli, ["check", "--help"])
    assert help_result.exit_code == 0
    assert "--all" not in help_result.output


def test_leftover_matrix_yaml_does_not_source_pin(data_root: Path) -> None:
    (data_root / "matrix.yaml").write_text(
        "cells:\n"
        '  - id: "default"\n'
        '    erp: "26.101.0225"\n'
        '    default_api: "24.200.001"\n'
        '    base_url: "https://cell.example/AcumaticaERP"\n'
    )
    inst = load_instance()
    assert inst.api_version == "25.200.001"
    assert inst.base_url == "http://acu.test/AcumaticaERP"


def test_missing_env_does_not_fall_back_to_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T218/V27: leftover matrix.yaml cannot supply pin+where with no .env."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "matrix.yaml").write_text(
        "cells:\n"
        '  - id: "default"\n'
        '    erp: "26.101.0225"\n'
        '    default_api: "24.200.001"\n'
        '    base_url: "http://cell.test/AcumaticaERP"\n'
    )
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    with pytest.raises(SystemExit, match="base_url: Field required"):
        load_instance()


def test_config_init_never_writes_matrix_yaml(tmp_path: Path) -> None:
    """T218/V27/V28: config init never scaffolds matrix.yaml."""
    result = CliRunner().invoke(cli.cli, ["config", "init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "matrix.yaml").exists()
    env = (tmp_path / ".env").read_text()
    assert "ACU_API_VERSION=25.200.001" in env
    assert "ACU_BASE_URL=" in env


def test_config_show_emits_api_version(data_root: Path) -> None:
    result = CliRunner().invoke(cli.cli, ["config", "show"])
    assert result.exit_code == 0
    assert "ACU_API_VERSION=25.200.001" in result.output
    assert "matrix.yaml" not in result.output


def test_config_show_flag_overrides_api_version(data_root: Path) -> None:
    result = CliRunner().invoke(
        cli.cli, ["--api-version", "24.200.001", "config", "show"]
    )
    assert result.exit_code == 0
    assert "ACU_API_VERSION=24.200.001" in result.output


def test_config_check_has_no_matrix_line(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "AcumaticaClient", DummyClient)
    monkeypatch.setattr(TenantManager, "ping", lambda self: None)
    result = CliRunner().invoke(cli.cli, ["config", "check"])
    assert result.exit_code == 0
    assert "matrix" not in result.output
    lines = result.output.splitlines()
    assert lines[0].startswith("ok discovery (")
    assert lines[1] == "ok secrets (ACU_PASSWORD set)"
    assert lines[2] == "ok rest (http://acu.test/AcumaticaERP, tenant T1)"
    assert lines[3] == "ok endpoints (Default/25.200.001 present)"
    assert lines[4] == "ok ssh (Administrator@acu.test)"


def test_apply_uses_env_pin_not_leftover_matrix(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / "matrix.yaml").write_text(
        "cells:\n"
        '  - id: "default"\n'
        '    erp: "26.101"\n'
        '    default_api: "24.200.001"\n'
        '    base_url: "http://acu.test/AcumaticaERP"\n'
    )
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
        "ACU_API_VERSION=23.200.001\n"
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
    assert "Default API version mismatch" not in result.output
    assert seen == ["23.200.001"]


def test_check_does_not_require_matrix(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(self: TenantManager) -> list[object]:
        raise RuntimeError("ssh down")

    monkeypatch.setattr(TenantManager, "list", boom)
    result = CliRunner().invoke(cli.cli, ["check", "--yes", "--tenant", "T1"])
    assert "matrix.yaml not found" not in result.output
    assert result.exit_code != 0


def test_check_requires_tenant(data_root: Path) -> None:
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_PASSWORD=secret\n"
    )
    result = CliRunner().invoke(cli.cli, ["check", "--yes"])
    assert result.exit_code != 0
    assert "tenant not set" in result.output


def test_check_requires_ssh(data_root: Path) -> None:
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=\n"
        "ACU_TENANT=T1\n"
        "ACU_PASSWORD=secret\n"
    )
    result = CliRunner().invoke(cli.cli, ["check", "--yes", "--tenant", "T1"])
    assert result.exit_code != 0
    assert "ACU_SSH not set" in result.output


class _FakeTenant:
    """Minimal tenant row for lifecycle mock."""

    def __init__(self, company_id: int, login_name: str) -> None:
        self.company_id = company_id
        self.login_name = login_name
        self.company_cd = login_name
        self.company_type = ""


class _LifeClient(DummyClient):
    """REST client stub: apply put + clean get_list for diff."""

    def put(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {}

    def get_list(self, *args: object, **kwargs: object) -> list[object]:
        return [{"UOM": {"value": "KG"}}]

    def get(self, *args: object, **kwargs: object) -> dict[str, object]:
        return {"UOM": {"value": "KG"}}


def _patch_lifecycle_ssh(
    monkeypatch: pytest.MonkeyPatch, store: list[_FakeTenant]
) -> None:
    """Wire TenantManager + bootstrap/login stubs for offline acu check."""

    def list_tenants(self: TenantManager) -> list[_FakeTenant]:
        del self
        return list(store)

    def create(
        self: TenantManager, company_id: int, login_name: str, *a: object
    ) -> str:
        del self, a
        store.append(_FakeTenant(company_id, login_name))
        return "created"

    def delete(
        self: TenantManager,
        company_id: int | None = None,
        *,
        login_name: str | None = None,
    ) -> str:
        del self, company_id
        store[:] = [t for t in store if t.login_name != login_name]
        return "deleted"

    def set_cd(self: TenantManager, company_id: int, company_cd: str) -> bool:
        del self, company_id, company_cd
        return False

    monkeypatch.setattr(TenantManager, "list", list_tenants)
    monkeypatch.setattr(TenantManager, "create", create)
    monkeypatch.setattr(TenantManager, "delete", delete)
    monkeypatch.setattr(TenantManager, "set_company_cd", set_cd)
    monkeypatch.setattr(TenantManager, "recycle_app_pool", lambda self: None)
    monkeypatch.setattr(
        cli.firstlogin, "initialize_admin_password", lambda *a, **k: "ok"
    )
    monkeypatch.setattr(cli.bootstrap, "publish", lambda *a, **k: "published")
    monkeypatch.setattr(cli, "AcumaticaClient", _LifeClient)


def test_check_lifecycle_mock_green(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # offline mock — pre-clean→create→apply→run→diff; leave tenant (V47)
    (data_root / "config" / "baseline").mkdir(parents=True)
    (data_root / "config" / "baseline" / "uom.yaml").write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nendpoint: default\nrecords:\n  - UOM: KG\n"
    )
    (data_root / "scenario").mkdir()
    (data_root / "scenario" / "10-stub.yaml").write_text("scenario: stub\nsteps: []\n")
    store: list[_FakeTenant] = []
    _patch_lifecycle_ssh(monkeypatch, store)

    result = CliRunner().invoke(cli.cli, ["check", "--yes", "--tenant", "T1"])

    assert result.exit_code == 0, result.output
    assert "check http://acu.test/AcumaticaERP" in result.output
    assert "check: green" in result.output
    assert "left for" in result.output
    assert "inspection" in result.output
    assert len(store) == 1
    assert store[0].login_name == "T1"


def test_check_ignores_leftover_multi_cell_matrix(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V47: leftover matrix.yaml cells are never walked; one .env instance."""
    (data_root / "matrix.yaml").write_text(
        "cells:\n"
        '  - id: "a"\n'
        '    erp: "26.101"\n'
        '    default_api: "24.200.001"\n'
        '    base_url: "http://cell-a.test/AcumaticaERP"\n'
        '  - id: "b"\n'
        '    erp: "26.101"\n'
        '    default_api: "23.200.001"\n'
        '    base_url: "http://cell-b.test/AcumaticaERP"\n'
    )
    (data_root / "config" / "baseline").mkdir(parents=True)
    (data_root / "config" / "baseline" / "uom.yaml").write_text(
        "entity: UnitsOfMeasure\nkey: UOM\nendpoint: default\nrecords:\n  - UOM: KG\n"
    )
    (data_root / "scenario").mkdir()
    (data_root / "scenario" / "10-stub.yaml").write_text("scenario: stub\nsteps: []\n")
    store: list[_FakeTenant] = []
    _patch_lifecycle_ssh(monkeypatch, store)

    result = CliRunner().invoke(cli.cli, ["check", "--yes", "--tenant", "T1"])

    assert result.exit_code == 0, result.output
    assert result.output.count("check http://acu.test/AcumaticaERP") == 1
    assert "cell-a.test" not in result.output
    assert "cell-b.test" not in result.output
    assert "24.200.001" not in result.output
    assert "23.200.001" not in result.output
    assert len(store) == 1
