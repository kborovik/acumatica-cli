"""Output helpers: stream routing, grep-able prefixes, TTY vs piped rendering.

pytest's capture is not a TTY, so the default consoles exercise the piped
(agent-facing) rendering; TTY rendering is forced with force_terminal.
"""

import io

import pytest
from rich.console import Console

from acumatica_cli import output


def test_data_goes_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    output.data("42 record(s)")
    captured = capsys.readouterr()
    assert captured.out == "42 record(s)\n"
    assert captured.err == ""


def test_status_helpers_go_to_stderr_with_prefixes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output.info("working")
    output.success("done")
    output.warn("careful")
    output.error("failed")
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "working\n✓ done\n! careful\n✗ failed\n"


def test_piped_output_has_no_ansi(capsys: pytest.CaptureFixture[str]) -> None:
    output.error("boom")
    assert "\x1b[" not in capsys.readouterr().err


def test_markup_in_payload_is_not_interpreted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    output.data("PUT Currency [CAD]")
    assert capsys.readouterr().out == "PUT Currency [CAD]\n"


def test_table_piped_is_plain_columns(capsys: pytest.CaptureFixture[str]) -> None:
    output.table("Tenants on test", ("ID", "Login"), [("2", "Company")])
    out = capsys.readouterr().out
    # narrow tables wrap their title to the table width — match a fragment
    assert "Tenants on" in out
    assert "ID" in out
    assert "Company" in out
    assert "╭" not in out
    assert "│" not in out


def test_table_tty_draws_box(monkeypatch: pytest.MonkeyPatch) -> None:
    buffer = io.StringIO()
    monkeypatch.setattr(output, "out", Console(file=buffer, force_terminal=True))
    output.table("Tenants on test", ("ID", "Login"), [("2", "Company")])
    assert "╭" in buffer.getvalue()


def test_step_piped_prints_plain_line(capsys: pytest.CaptureFixture[str]) -> None:
    entered = False
    with output.step("recycling app pool"):
        entered = True
    assert entered
    assert capsys.readouterr().err == "recycling app pool\n"
