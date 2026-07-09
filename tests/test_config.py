"""load_instance: layered defaults over acu.yaml, environment merging.

The file is a flat top-level map = the single target instance. host is the
only required key; everything else is a code default (transcribed from
docs/ac-exe.md + docs/rest-api.md) that an explicit acu.yaml key overrides.
"""

from pathlib import Path

import pytest

from acumatica_cli.config import load_instance

MINIMAL_YAML = """\
host: acu.test
"""

OVERRIDE_YAML = """\
host: acu.test
base_url: https://edge.example/AcumaticaERP/
endpoint: /Custom/1.0.0/
ssh: user@jump.example
ac_exe: 'D:\\Acumatica\\ac.exe'
instance_name: Custom
instance_path: 'D:\\Acumatica\\Custom'
db_name: CustomDB
tenant: T1
"""


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake data root: acu.yaml present, cwd inside it."""
    (tmp_path / "acu.yaml").write_text(MINIMAL_YAML)
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACU_USER", raising=False)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    return tmp_path


def test_minimal_config_resolves_code_defaults(data_root: Path) -> None:
    # host is the one required key; the rest are verified-install conventions
    inst = load_instance()
    assert inst.base_url == "http://acu.test/AcumaticaERP"
    assert inst.ssh == "Administrator@acu.test"
    assert inst.instance_name == "AcumaticaERP"
    assert inst.instance_path == "C:\\Acumatica\\AcumaticaERP"
    assert inst.ac_exe == "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
    assert inst.db_name == "AcumaticaDB"
    assert inst.endpoint == "Default/25.200.001"
    assert inst.tenant == ""  # optional, defaults empty
    assert inst.username == "admin"  # ACU_USER default
    assert inst.password == "secret"


def test_explicit_overrides_win_over_defaults(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text(OVERRIDE_YAML)
    inst = load_instance()
    assert inst.base_url == "https://edge.example/AcumaticaERP"  # trailing / stripped
    assert inst.endpoint == "Custom/1.0.0"  # slashes stripped
    assert inst.ssh == "user@jump.example"
    assert inst.ac_exe == "D:\\Acumatica\\ac.exe"
    assert inst.instance_path == "D:\\Acumatica\\Custom"
    assert inst.db_name == "CustomDB"
    assert inst.tenant == "T1"


def test_data_root_found_in_parent_dir(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = data_root / "baseline" / "nested"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert load_instance().host == "acu.test"


def test_missing_acu_yaml_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=r"acu\.yaml not found"):
        load_instance()


def test_empty_config_errors(data_root: Path) -> None:
    # yaml.safe_load returns None for an empty file - hard error, not a crash
    (data_root / "acu.yaml").write_text("")
    with pytest.raises(SystemExit, match="expected a mapping"):
        load_instance()


def test_non_mapping_config_errors(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(SystemExit, match="expected a mapping"):
        load_instance()


def test_nested_legacy_config_rejected(data_root: Path) -> None:
    # the pre-flatten format (default_instance + instances.<name>) must fail
    # loudly, naming the offending keys - extra="forbid" is the migration signal
    (data_root / "acu.yaml").write_text(
        "default_instance: test\n\ninstances:\n  test:\n    host: acu.test\n"
    )
    with pytest.raises(SystemExit, match="default_instance"):
        load_instance()


def test_load_instance_requires_host(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text("tenant: T1\n")
    with pytest.raises(SystemExit, match="host: Field required"):
        load_instance()


def test_load_instance_rejects_unknown_field(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text(MINIMAL_YAML + "db_nmae: typo\n")
    with pytest.raises(SystemExit, match="db_nmae"):
        load_instance()


def test_load_instance_honors_acu_user(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACU_USER", "api")
    assert load_instance().username == "api"


def test_load_instance_requires_password(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACU_PASSWORD")
    with pytest.raises(SystemExit, match="ACU_PASSWORD not set"):
        load_instance()


def test_load_instance_reads_dotenv(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACU_PASSWORD")
    (data_root / ".env").write_text("ACU_PASSWORD=from-dotenv\n")
    assert load_instance().password == "from-dotenv"
