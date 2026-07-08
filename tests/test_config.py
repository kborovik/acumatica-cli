"""load_instance: acu.toml + environment merging, credential requirements."""

from pathlib import Path

import pytest

from acumatica_cli.config import load_instance

ACU_TOML = """\
default_instance = "test"

[instances.test]
base_url = "http://acu.test/AcumaticaERP/"
endpoint = "/Default/25.200.001/"
ssh = "user@acu.test"
ac_exe = 'C:\\Acumatica\\ac.exe'
instance_name = "AcumaticaERP"
instance_path = 'C:\\Acumatica\\AcumaticaERP'
db_name = "AcuDB"
"""


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake data root: acu.toml present, cwd inside it."""
    (tmp_path / "acu.toml").write_text(ACU_TOML)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACU_USER", raising=False)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    return tmp_path


def test_load_instance_merges_toml_and_env(data_root: Path) -> None:
    inst = load_instance("test")
    assert inst.base_url == "http://acu.test/AcumaticaERP"  # trailing / stripped
    assert inst.endpoint == "Default/25.200.001"  # slashes stripped
    assert inst.tenant == ""  # optional, defaults empty
    assert inst.username == "admin"  # ACU_USER default
    assert inst.password == "secret"


def test_load_instance_uses_default_instance(data_root: Path) -> None:
    assert load_instance().name == "test"


def test_data_root_found_in_parent_dir(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = data_root / "baseline" / "nested"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert load_instance("test").name == "test"


def test_missing_acu_toml_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=r"acu\.toml not found"):
        load_instance("test")


def test_load_instance_rejects_unknown_name(data_root: Path) -> None:
    with pytest.raises(SystemExit, match=r"no \[instances\.nope\] \(known: test\)"):
        load_instance("nope")


def test_load_instance_rejects_missing_field(data_root: Path) -> None:
    text = ACU_TOML.replace('db_name = "AcuDB"\n', "")
    (data_root / "acu.toml").write_text(text)
    with pytest.raises(SystemExit, match="db_name: Field required"):
        load_instance("test")


def test_load_instance_rejects_unknown_field(data_root: Path) -> None:
    (data_root / "acu.toml").write_text(ACU_TOML + 'db_nmae = "typo"\n')
    with pytest.raises(SystemExit, match="db_nmae"):
        load_instance("test")


def test_load_instance_honors_acu_user(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACU_USER", "api")
    assert load_instance("test").username == "api"


def test_load_instance_requires_password(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACU_PASSWORD")
    with pytest.raises(SystemExit, match="ACU_PASSWORD not set"):
        load_instance("test")


def test_load_instance_reads_dotenv(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACU_PASSWORD")
    (data_root / ".env").write_text("ACU_PASSWORD=from-dotenv\n")
    assert load_instance("test").password == "from-dotenv"
