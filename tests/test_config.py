"""load_instance: global flags over ACU_* environment over code defaults.

pydantic-settings owns resolution (I.cfg): the sole config file is .env
(the discovery sentinel, found by cwd walk-up, optional — V3 lax
discovery), carrying where + secrets as ACU_* vars. Per key the first set
value wins: flag, ACU_* var (process environment over .env), code default.
"""

from pathlib import Path

import pytest

from acumatica_cli import config
from acumatica_cli.config import load_instance, read_env_values

MINIMAL_ENV = """\
ACU_BASE_URL=http://acu.test/AcumaticaERP
ACU_SSH=Administrator@acu.test
ACU_PASSWORD=secret
"""

FULL_ENV = """\
ACU_BASE_URL=https://edge.example/AcumaticaERP/
ACU_SSH=user@jump.example
ACU_TENANT=T1
ACU_API_VERSION=/24.200.001/
ACU_USER=api
ACU_PASSWORD=secret
"""


@pytest.fixture
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake data repo: .env present (the discovery sentinel), cwd inside it.

    Ambient ACU_* vars are scrubbed by the conftest autouse fixture, so the
    .env file and explicit setenv calls are the only environment sources.
    """
    (tmp_path / ".env").write_text(MINIMAL_ENV)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_minimal_env_resolves_code_defaults(data_root: Path) -> None:
    # ACU_BASE_URL + ACU_PASSWORD are required; ACU_SSH optional (V3);
    # the rest are code defaults (I.cfg); install layout = constants, not fields
    inst = load_instance()
    assert inst.base_url == "http://acu.test/AcumaticaERP"
    assert inst.ssh == "Administrator@acu.test"
    assert inst.api_version == "25.200.001"
    assert inst.tenant == ""  # optional, defaults empty
    assert inst.user == "admin"  # ACU_USER default
    assert inst.password == "secret"


def test_env_file_values_override_defaults(data_root: Path) -> None:
    (data_root / ".env").write_text(FULL_ENV)
    inst = load_instance()
    assert inst.base_url == "https://edge.example/AcumaticaERP"  # trailing / stripped
    assert inst.api_version == "24.200.001"  # slashes stripped
    assert inst.ssh == "user@jump.example"
    assert inst.tenant == "T1"
    assert inst.user == "api"


def test_install_layout_values_are_constants(data_root: Path) -> None:
    # T42/I.cfg: install layout = code constants, deliberately not config
    # surface - the verified stock-install values live in config.py
    assert config.ACU_INSTANCE_NAME == "AcumaticaERP"
    assert config.ACU_INSTANCE_PATH == "C:\\Acumatica\\AcumaticaERP"
    assert config.AC_EXE == "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
    assert config.DB_NAME == "AcumaticaDB"


def test_flag_overrides_beat_env_per_key(data_root: Path) -> None:
    # I.cmd precedence: flag, ACU_* env, code default - first set wins per
    # key; field validators still run on flag values (trailing / stripped)
    inst = load_instance(
        {"base_url": "https://flag.example/AcumaticaERP/", "tenant": "FromFlag"}
    )
    assert inst.base_url == "https://flag.example/AcumaticaERP"
    assert inst.tenant == "FromFlag"
    assert inst.ssh == "Administrator@acu.test"  # untouched keys keep .env


def test_process_env_beats_dotenv_per_key(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd: ".env or process" is one layer; within it the process
    # environment wins, matching the pre-T46 load_dotenv(no-override) order
    (data_root / ".env").write_text(FULL_ENV)
    monkeypatch.setenv("ACU_TENANT", "FromProcess")
    inst = load_instance()
    assert inst.tenant == "FromProcess"
    assert inst.ssh == "user@jump.example"  # untouched keys keep .env


def test_flags_only_resolution_without_env_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3 lax discovery: no .env anywhere - flags supply the full config
    monkeypatch.chdir(tmp_path)
    inst = load_instance(
        {
            "base_url": "http://flags.test/AcumaticaERP",
            "ssh": "user@flags.test",
            "password": "flagpw",
        }
    )
    assert inst.base_url == "http://flags.test/AcumaticaERP"
    assert inst.ssh == "user@flags.test"
    assert inst.user == "admin"
    assert inst.password == "flagpw"


def test_process_env_alone_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3/I.env: .env absent - resolution runs on the process environment
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_BASE_URL", "http://env.test/AcumaticaERP")
    monkeypatch.setenv("ACU_SSH", "user@env.test")
    monkeypatch.setenv("ACU_PASSWORD", "envpw")
    inst = load_instance()
    assert inst.base_url == "http://env.test/AcumaticaERP"
    assert inst.ssh == "user@env.test"
    assert inst.password == "envpw"


def test_data_root_found_in_parent_dir(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = data_root / "baseline" / "nested"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert load_instance().base_url == "http://acu.test/AcumaticaERP"


def test_missing_env_file_names_unresolved_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # V3: absence of .env is not the error - the unresolved required
    # values are, named post-merge; ssh is optional (empty default)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "secret")
    with pytest.raises(SystemExit, match="base_url: Field required") as exc_info:
        load_instance()
    assert "ssh: Field required" not in str(exc_info.value)
    assert "no .env found" in str(exc_info.value)


def test_blank_required_key_reads_as_missing(data_root: Path) -> None:
    # V3: a blank ACU_BASE_URL= line is an unresolved key, never an empty
    # address silently accepted
    (data_root / ".env").write_text(
        "ACU_BASE_URL=\nACU_SSH=Administrator@acu.test\nACU_PASSWORD=secret\n"
    )
    with pytest.raises(SystemExit, match="base_url: Field required"):
        load_instance()


def test_missing_ssh_data_plane_resolves(data_root: Path) -> None:
    # T67/V3: hosted path — base_url + password alone resolve; ssh defaults empty
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=secret\n"
    )
    inst = load_instance()
    assert inst.base_url == "http://acu.test/AcumaticaERP"
    assert inst.ssh == ""
    assert inst.password == "secret"


def test_blank_ssh_reads_as_unset(data_root: Path) -> None:
    # T67/V3: blank ACU_SSH= is unset (empty default), not a required-field error
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=\nACU_PASSWORD=secret\n"
    )
    inst = load_instance()
    assert inst.ssh == ""


def test_unknown_acu_vars_are_ignored(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.env: .env and the environment legitimately carry non-config ACU_*
    # vars (ACU_DEBUG) - never a validation error
    (data_root / ".env").write_text(MINIMAL_ENV + "ACU_DEBUG=1\nACU_UNKNOWN=x\n")
    monkeypatch.setenv("ACU_DEBUG", "1")
    assert load_instance().base_url == "http://acu.test/AcumaticaERP"


def test_username_env_var_is_acu_user(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cfg: the username var is ACU_USER (prefix + field name), and an
    # unknown ACU_USERNAME is ignored like any other non-config ACU_* var
    monkeypatch.setenv("ACU_USERNAME", "nope")
    assert load_instance().user == "admin"
    monkeypatch.setenv("ACU_USER", "api")
    assert load_instance().user == "api"


def test_credential_flags_beat_environment(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # I.cmd creds precedence: flag, env, admin default (username only)
    monkeypatch.setenv("ACU_USER", "env-user")
    inst = load_instance({"user": "flag-user", "password": "flag-pw"})
    assert inst.user == "flag-user"
    assert inst.password == "flag-pw"


def test_load_instance_requires_password(data_root: Path) -> None:
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=Administrator@acu.test\n"
    )
    with pytest.raises(SystemExit, match="password not set"):
        load_instance()


def test_blank_password_placeholder_reads_as_unset(data_root: Path) -> None:
    # the scaffolded ACU_PASSWORD= placeholder must raise the same named
    # error as a missing var, never sign in with an empty password
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\n"
        "ACU_SSH=Administrator@acu.test\n"
        "ACU_PASSWORD=\n"
    )
    with pytest.raises(SystemExit, match="password not set"):
        load_instance()


def test_password_from_process_env(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (data_root / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_SSH=Administrator@acu.test\n"
    )
    monkeypatch.setenv("ACU_PASSWORD", "from-env")
    assert load_instance().password == "from-env"


def test_read_env_values_keys_by_field_name(data_root: Path) -> None:
    # config check's probe helper: the same DotEnvSettingsSource parse live
    # resolution uses, keyed by Instance field name (base_url, password)
    values = read_env_values(data_root / ".env")
    assert values["base_url"] == "http://acu.test/AcumaticaERP"
    assert values["password"] == "secret"
