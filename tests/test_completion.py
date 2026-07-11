"""--completion script emit + dynamic value completion (T55).

Pins the I.cmd --completion contract and V23: the whole completion path
is local-only - completion fires per keystroke, so the script emit and
every dynamic value callback must never resolve an instance or touch
REST/SSH. The autouse fixture makes any live-path touch a hard failure.
"""

import pytest
from click.shell_completion import ShellComplete
from click.testing import CliRunner

from acumatica_cli import cli
from acumatica_cli.tenant import TenantManager


@pytest.fixture(autouse=True)
def nothing_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """V23: any instance resolution or live-plane construction fails loud."""

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("completion touched a live path (V23)")

    monkeypatch.setattr(cli, "load_instance", boom)
    monkeypatch.setattr(cli, "AcumaticaClient", boom)
    monkeypatch.setattr(TenantManager, "__init__", boom)


def _completions(args: list[str], incomplete: str) -> list[str]:
    """Drive click's real completion machinery, as a shell keystroke would."""
    comp = ShellComplete(cli.cli, {}, "acu", "_ACU_COMPLETE")
    return [item.value for item in comp.get_completions(args, incomplete)]


def test_completion_explicit_shell_emits_script() -> None:
    # I.cmd: `acu --completion fish` prints the click fish script on stdout
    # and exits 0 - eager, like --version (V16); enabling = sourcing it
    result = CliRunner().invoke(cli.cli, ["--completion", "fish"])

    assert result.exit_code == 0
    assert "_ACU_COMPLETE=fish_complete" in result.stdout
    assert result.output.isascii()  # V9: ASCII-only every path


def test_completion_detects_shell_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # I.cmd: <shell> omitted -> the $SHELL basename picks the dialect
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/zsh")
    result = CliRunner().invoke(cli.cli, ["--completion"])

    assert result.exit_code == 0
    assert "#compdef acu" in result.stdout


def test_completion_undetectable_shell_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    # I.cmd/V9: $SHELL unset and no <shell> -> exit 1, naming the supported set
    monkeypatch.delenv("SHELL", raising=False)
    result = CliRunner().invoke(cli.cli, ["--completion"])

    assert result.exit_code == 1
    assert "supported: bash, zsh, fish" in result.output


def test_completion_unsupported_shell_errors() -> None:
    # I.cmd/V9: a shell click has no dialect for -> exit 1, naming the set
    result = CliRunner().invoke(cli.cli, ["--completion", "powershell"])

    assert result.exit_code == 1
    assert "'powershell'" in result.output
    assert "supported: bash, zsh, fish" in result.output


def test_completion_rejected_after_subcommand() -> None:
    # V16: globals valid only before the subcommand
    result = CliRunner().invoke(cli.cli, ["config", "show", "--completion"])

    assert result.exit_code != 0
    assert "No such option" in result.output


def test_extract_only_completes_manifest_entity_names() -> None:
    # I.cmd: --only values come from the packaged extract manifest - package
    # data (V23), matching the manifest's entity spelling exactly
    values = _completions(["extract", "--only"], "")

    assert "Company" in values
    assert "UnitsOfMeasure" in values
    assert _completions(["extract", "--only"], "Led") == ["Ledger", "LedgerCompany"]


def test_apply_and_diff_path_args_complete_as_files() -> None:
    # I.cmd: FILES completion is click-native off the Path type - the shell
    # completes filesystem paths locally, no CLI-side candidate list
    comp = ShellComplete(cli.cli, {}, "acu", "_ACU_COMPLETE")
    for command in ("apply", "diff"):
        items = comp.get_completions([command], "")
        assert [item.type for item in items] == ["file"], command
