"""V27: leftover matrix.yaml ignored; --cell gone; env pin is sole config."""

import importlib
import re
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


def test_docs_user_role_membership_persist() -> None:
    """T229/V12/V19/V53: docs name AssignUser persist; drop T189 limit + Selected."""
    repo = Path(__file__).resolve().parents[1]
    demo = (repo / "docs" / "demo-seed.md").read_text()
    changelog = (repo / "CHANGELOG.md").read_text()
    readme = (repo / "README.md").read_text()
    templates = (repo / "src" / "acumatica_cli" / "templates" / "README.md").read_text()
    assert "membership limit (T189)" not in demo
    assert "Selected: true" not in demo
    assert "AssignUser" in demo
    assert "92-role-users.yaml" in demo
    assert "UsersInRoles" in demo
    assert "RolesByUser" in demo
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "gh #35" in changelog
    assert "AssignUser" in changelog
    assert "92-role-users" in readme
    assert "92-role-users" in templates


def test_docs_drop_root_check() -> None:
    """T232/V12/V19/V47: human-facing docs compose cold rebuild; no acu check verb."""
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text()
    templates = (repo / "src" / "acumatica_cli" / "templates" / "README.md").read_text()
    changelog = (repo / "CHANGELOG.md").read_text()
    assert "acu check --yes" not in readme
    assert "check [--yes]" not in readme
    assert "| `check` |" not in readme
    assert "then `apply` then `run` then `diff`" in readme
    assert "acu check --yes" not in templates
    assert "tenant create" in templates
    # V19 promote empties Unreleased; persist note lives in the versioned section.
    assert "`acu check` (V47):** dropped" in changelog
    assert "`tenant create` then" in changelog


def test_docs_env_sole_config() -> None:
    """T219/V12/V27: human-facing docs pin via .env, not matrix.yaml."""
    repo = Path(__file__).resolve().parents[1]
    readme = (repo / "README.md").read_text()
    rest = (repo / "docs" / "rest-api.md").read_text()
    demo = (repo / "docs" / "demo-seed.md").read_text()
    for text in (readme, rest, demo):
        assert "[--cell" not in text
        assert "check --all" not in text
        assert "ACU_API_VERSION" in text
    assert "| `matrix.yaml` |" not in readme
    assert "check [--all]" not in readme
    assert "--strict" not in readme
    assert "default_api" not in readme
    assert "There is no `ACU_API_VERSION`" not in rest
    assert "matrix.yaml `default_api`" not in demo
    assert "matrix.yaml default_api" not in demo


# L1 extract/inventory/reconcile after optional globals.
# `acu survey extract` is not a hit.
_L1_SURVEY_VERB = re.compile(
    r"\bacu(?:\s+(?:--[a-z-]+(?:\s+\S+)?))*\s+(extract|inventory|reconcile)\b"
)


def _unreleased(changelog: str) -> str:
    marker = "## Unreleased"
    start = changelog.index(marker) + len(marker)
    rest = changelog[start:]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def test_docs_survey_group() -> None:
    """T235/V15/V48/V19: docs nest extract/inventory/reconcile under survey."""
    repo = Path(__file__).resolve().parents[1]
    scoped = [repo / "README.md"]
    scoped.extend(sorted((repo / "docs").glob("*.md")))
    scoped.extend(
        sorted((repo / "src" / "acumatica_cli" / "templates").rglob("README.md"))
    )
    hits: list[str] = []
    for path in scoped:
        text = path.read_text()
        for m in _L1_SURVEY_VERB.finditer(text):
            rel = path.relative_to(repo).as_posix()
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}: {m.group(0)}")
    assert hits == [], "L1 extract/inventory/reconcile CLI:\n" + "\n".join(hits)

    readme = (repo / "README.md").read_text()
    assert re.search(r"^├── survey", readme, re.M)
    assert "`survey extract`" in readme
    assert "`survey inventory`" in readme
    assert "`survey reconcile`" in readme
    assert re.search(r"^├── extract", readme, re.M) is None
    assert re.search(r"^├── inventory", readme, re.M) is None
    assert re.search(r"^├── reconcile", readme, re.M) is None

    unreleased = _unreleased((repo / "CHANGELOG.md").read_text())
    assert "`acu survey`" in unreleased
    assert "extract" in unreleased
    assert "inventory" in unreleased
    assert "reconcile" in unreleased
    assert _L1_SURVEY_VERB.search(unreleased) is None
