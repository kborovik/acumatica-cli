"""E2E harness: live target is repo-root .env, not ambient ACU_*."""

from pathlib import Path

import pytest

from tests.e2e.conftest import acu_binary, env_without_acu, load_instance_from_dotenv


def test_acu_binary_is_venv_not_framework() -> None:
    path = acu_binary()
    assert path.is_file()
    assert path.name == "acu"
    assert "Cellar" not in path.parts
    assert "Frameworks" not in path.parts


def test_env_without_acu_drops_acu_keys() -> None:
    src = {"PATH": "/bin", "ACU_BASE_URL": "http://ambient.example/X", "HOME": "/tmp"}
    assert env_without_acu(src) == {"PATH": "/bin", "HOME": "/tmp"}


def test_load_instance_from_dotenv_ignores_ambient(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gmake e2e follows the copied .env even if the shell has ACU_* set."""
    monkeypatch.setenv("ACU_BASE_URL", "http://ambient.example/AcumaticaERP")
    monkeypatch.setenv("ACU_PASSWORD", "ambient")
    monkeypatch.setenv("ACU_TENANT", "AMBIENT")
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://from-file.example/AcumaticaERP\n"
        "ACU_PASSWORD=filepw\n"
        "ACU_TENANT=FILETENANT\n",
        encoding="utf-8",
    )
    inst = load_instance_from_dotenv(tmp_path)
    assert inst.base_url == "http://from-file.example/AcumaticaERP"
    assert inst.password == "filepw"
    assert inst.tenant == "FILETENANT"
