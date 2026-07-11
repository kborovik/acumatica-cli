"""load_instance: global flags over acu.yaml over code defaults.

The file is a flat top-level map = the single target instance, and it is
optional - flags plus environment can supply the full config (V3 lax
discovery). Per key the first set value wins: flag, acu.yaml, code default;
credentials resolve flag over environment.
"""

from pathlib import Path

import pytest

from acumatica_cli import config
from acumatica_cli.config import load_instance

MINIMAL_YAML = """\
base_url: http://acu.test/AcumaticaERP
ssh: Administrator@acu.test
"""

OVERRIDE_YAML = """\
base_url: https://edge.example/AcumaticaERP/
ssh: user@jump.example
api_version: /24.200.001/
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
    # base_url + ssh are the two required keys; the rest are code defaults
    # (I.cfg); install-layout values are constants, not fields (T42)
    inst = load_instance()
    assert inst.base_url == "http://acu.test/AcumaticaERP"
    assert inst.ssh == "Administrator@acu.test"
    assert inst.api_version == "25.200.001"
    assert inst.tenant == ""  # optional, defaults empty
    assert inst.username == "admin"  # ACU_USER default
    assert inst.password == "secret"


def test_explicit_overrides_win_over_defaults(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text(OVERRIDE_YAML)
    inst = load_instance()
    assert inst.base_url == "https://edge.example/AcumaticaERP"  # trailing / stripped
    assert inst.api_version == "24.200.001"  # slashes stripped
    assert inst.ssh == "user@jump.example"
    assert inst.tenant == "T1"


def test_install_layout_values_are_constants(data_root: Path) -> None:
    # T42/I.cfg: install layout = code constants, deliberately not config
    # surface - the verified stock-install values live in config.py
    assert config.ACU_INSTANCE_NAME == "AcumaticaERP"
    assert config.ACU_INSTANCE_PATH == "C:\\Acumatica\\AcumaticaERP"
    assert config.AC_EXE == "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
    assert config.DB_NAME == "AcumaticaDB"


def test_flag_overrides_beat_acu_yaml_per_key(data_root: Path) -> None:
    # I.cmd precedence: flag, acu.yaml, code default - first set wins per
    # key; field validators still run on flag values (trailing / stripped)
    inst = load_instance(
        {"base_url": "https://flag.example/AcumaticaERP/", "tenant": "FromFlag"}
    )
    assert inst.base_url == "https://flag.example/AcumaticaERP"
    assert inst.tenant == "FromFlag"
    assert inst.ssh == "Administrator@acu.test"  # untouched keys keep acu.yaml


def test_flags_only_resolution_without_acu_yaml(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3 lax discovery: no acu.yaml anywhere - flags + environment supply
    # the full config, resolution proceeds
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACU_USER", raising=False)
    monkeypatch.delenv("ACU_PASSWORD", raising=False)
    inst = load_instance(
        {
            "base_url": "http://flags.test/AcumaticaERP",
            "ssh": "user@flags.test",
            "password": "flagpw",
        }
    )
    assert inst.base_url == "http://flags.test/AcumaticaERP"
    assert inst.ssh == "user@flags.test"
    assert inst.username == "admin"
    assert inst.password == "flagpw"


def test_no_acu_yaml_skips_dotenv_load(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3/I.env: .env loads only from the dir of a found acu.yaml - without
    # one, a cwd .env must NOT be read, so the password stays unresolved
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ACU_PASSWORD", raising=False)
    (tmp_path / ".env").write_text("ACU_PASSWORD=must-not-load\n")
    with pytest.raises(SystemExit, match="password not set"):
        load_instance({"base_url": "http://x/AcumaticaERP", "ssh": "u@x"})


def test_credential_flags_beat_environment(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd creds precedence: flag, env, admin default (username only)
    monkeypatch.setenv("ACU_USER", "env-user")
    inst = load_instance({"username": "flag-user", "password": "flag-pw"})
    assert inst.username == "flag-user"
    assert inst.password == "flag-pw"


def test_acu_yaml_credential_keys_rejected(data_root: Path) -> None:
    # V2: acu.yaml never carries secrets - credential keys fail loudly,
    # never a duplicate-kwarg traceback
    (data_root / "acu.yaml").write_text(MINIMAL_YAML + "password: leaked\n")
    with pytest.raises(SystemExit, match="credentials never live in config"):
        load_instance()


def test_data_root_found_in_parent_dir(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = data_root / "baseline" / "nested"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert load_instance().base_url == "http://acu.test/AcumaticaERP"


def test_missing_acu_yaml_names_unresolved_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3: absence of acu.yaml is not the error - the unresolved required
    # values are, named post-merge
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    with pytest.raises(SystemExit, match="base_url: Field required") as exc_info:
        load_instance()
    assert "ssh: Field required" in str(exc_info.value)
    assert "no acu.yaml found" in str(exc_info.value)


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


@pytest.mark.parametrize(
    "line",
    [
        "host: acu.test",
        "scheme: https",
        "ssh_user: root",
        "instance_name: Custom",
        "instance_path: 'D:\\Acumatica'",
        "endpoint: Default/25.200.001",
        "acu_instance_name: Custom",
        "acu_instance_path: 'D:\\Acumatica\\Custom'",
        "ac_exe: 'D:\\Acumatica\\ac.exe'",
        "db_name: CustomDB",
    ],
)
def test_pre_consolidation_keys_rejected(data_root: Path, line: str) -> None:
    # T40/T42 migration signal: every retired key (host derivation,
    # unprefixed renames, endpoint, demoted install-layout fields) fails
    # loudly naming the key via extra="forbid"
    (data_root / "acu.yaml").write_text(MINIMAL_YAML + line + "\n")
    with pytest.raises(SystemExit, match=line.split(":", maxsplit=1)[0]):
        load_instance()


def test_load_instance_requires_base_url(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text("ssh: Administrator@acu.test\n")
    with pytest.raises(SystemExit, match="base_url: Field required"):
        load_instance()


def test_load_instance_requires_ssh(data_root: Path) -> None:
    (data_root / "acu.yaml").write_text("base_url: http://acu.test/AcumaticaERP\n")
    with pytest.raises(SystemExit, match="ssh: Field required"):
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
    with pytest.raises(SystemExit, match="password not set"):
        load_instance()


def test_load_instance_reads_dotenv(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ACU_PASSWORD")
    (data_root / ".env").write_text("ACU_PASSWORD=from-dotenv\n")
    assert load_instance().password == "from-dotenv"
