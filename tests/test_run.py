"""Scenario parsing and the run engine — fully offline (T62).

Live responses ride an AcumaticaClient over httpx.MockTransport, so
steps, capture/interpolation, waits, and expectations execute through
the real client (wrap, invoke poll, _checked).
"""

import json
from pathlib import Path

import httpx
import pytest

from acumatica_cli import run
from acumatica_cli.client import AcumaticaClient, wrap
from acumatica_cli.config import Instance

SCENARIO = """\
scenario: smoke
description: one GL batch, released, revenue delta checked
steps:
  - id: batch
    put: JournalTransaction
    record:
      Module: GL
      Hold: false
      Details:
        - { Account: '10100', DebitAmount: 125.0 }
        - { Account: '30000', CreditAmount: 125.0 }
    capture: { BatchNbr: batch_nbr }
  - id: release
    action: { entity: JournalTransaction, name: ReleaseJournalTransaction }
    record: { Module: GL, BatchNbr: "${batch_nbr}" }
    wait:
      entity: JournalTransaction
      keys: [GL, "${batch_nbr}"]
      until: { Status: Posted }
      timeout: 5
expect:
  - get: { entity: JournalTransaction, keys: [GL, "${batch_nbr}"] }
    fields: { Status: Posted }
  - inquire: AccountSummaryInquiry
    parameters: { Ledger: ACTUAL, Period: "062026" }
    match: { Account: "30000" }
    delta: { EndingBalance: 125.0 }
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "scenario.yaml"
    path.write_text(text)
    return path


def test_load_scenario_parses(tmp_path: Path) -> None:
    scenario = run.load_scenario(_write(tmp_path, SCENARIO))
    assert scenario.scenario == "smoke"
    assert [s.id for s in scenario.steps] == ["batch", "release"]
    assert scenario.steps[0].capture == {"BatchNbr": "batch_nbr"}
    assert scenario.expect[1].delta == {"EndingBalance": 125.0}


def test_load_scenario_rejects_two_ops(tmp_path: Path) -> None:
    text = SCENARIO.replace(
        "    action: { entity: JournalTransaction, name: ReleaseJournalTransaction }",
        "    put: X\n"
        "    action: { entity: JournalTransaction, name: ReleaseJournalTransaction }",
    )
    with pytest.raises(SystemExit, match="put and action are exclusive"):
        run.load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_duplicate_step_ids(tmp_path: Path) -> None:
    text = SCENARIO.replace("id: release", "id: batch")
    with pytest.raises(SystemExit, match="duplicate step id 'batch'"):
        run.load_scenario(_write(tmp_path, text))


def test_load_scenario_rejects_expect_without_kind(tmp_path: Path) -> None:
    text = SCENARIO + "  - fields: { Status: Posted }\n"
    with pytest.raises(SystemExit, match="exactly one of get, inquire"):
        run.load_scenario(_write(tmp_path, text))


def test_subst_whole_token_keeps_type() -> None:
    subst = run._subst  # pyright: ignore[reportPrivateUsage]
    variables = {"nbr": "000012", "qty": 5}
    assert subst("${qty}", variables) == 5
    assert subst("batch ${nbr}!", variables) == "batch 000012!"
    assert subst({"k": ["${nbr}"]}, variables) == {"k": ["000012"]}


def test_subst_unknown_variable_is_hard_error() -> None:
    subst = run._subst  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(SystemExit, match=r"unknown scenario variable '\$\{nope\}'"):
        subst("${nope}", {})


class Server:
    """Scriptable fake: routes by (method, path suffix); records requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.batch_status = "Posted"
        self.summary_balance = 100.0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/JournalTransaction") and request.method == "PUT":
            return httpx.Response(
                200, json=wrap({"Module": "GL", "BatchNbr": "000042"})
            )
        if path.endswith("/ReleaseJournalTransaction"):
            self.summary_balance += 125.0
            return httpx.Response(204)
        if path.endswith("/JournalTransaction/GL/000042"):
            return httpx.Response(
                200, json=wrap({"BatchNbr": "000042", "Status": self.batch_status})
            )
        if path.endswith("/AccountSummaryInquiry"):
            return httpx.Response(
                200,
                json={
                    "Results": [
                        wrap(
                            {"Account": "30000", "EndingBalance": self.summary_balance}
                        ),
                        wrap({"Account": "10100", "EndingBalance": 999.0}),
                    ]
                },
            )
        return httpx.Response(200, json={})


def _client(instance: Instance, server: Server) -> AcumaticaClient:
    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    client.poll_interval = 0.0
    return client


def test_run_executes_steps_and_expectations(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole engine offline: put + capture, action, wait, both expects.

    The inquiry snapshot rides BEFORE the first step (V4 delta semantics):
    the release bumps the fake's balance by 125, and the expectation holds
    against the delta, not the absolute - re-runnable on a warm tenant.
    """
    scenario = run.load_scenario(_write(tmp_path, SCENARIO))
    server = Server()
    ok = run.run(_client(instance, server), scenario)
    out = capsys.readouterr().out
    assert ok
    assert "+ JournalTransaction [GL, 000042].Status = 'Posted'" in out
    assert "+ AccountSummaryInquiry [Account=30000].EndingBalance delta +125" in out
    # the batch PUT carried the detail list bare (wrap recursion, T60)
    put = next(r for r in server.requests if r.url.path.endswith("/JournalTransaction"))
    body = json.loads(put.content)
    assert isinstance(body["Details"], list)
    # the release wait polled the key URL with the captured number
    assert any(r.url.path.endswith("/GL/000042") for r in server.requests)


def test_run_reports_expectation_miss(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = run.load_scenario(_write(tmp_path, SCENARIO))
    server = Server()
    original = server.__class__.__call__

    def no_bump(self: Server, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ReleaseJournalTransaction"):
            self.requests.append(request)
            return httpx.Response(204)  # release without the balance bump
        return original(self, request)

    server.__class__ = type("Quiet", (Server,), {"__call__": no_bump})
    ok = run.run(_client(instance, server), scenario)
    assert not ok
    assert "x AccountSummaryInquiry [Account=30000].EndingBalance" in (
        capsys.readouterr().out
    )


def test_run_wait_timeout_names_last_state(tmp_path: Path, instance: Instance) -> None:
    text = SCENARIO.replace("timeout: 5", "timeout: 0.01")
    scenario = run.load_scenario(_write(tmp_path, text))
    server = Server()
    server.batch_status = "Balanced"  # never reaches Posted
    with pytest.raises(RuntimeError, match=r"wait timed out.*Status='Balanced'"):
        run.run(_client(instance, server), scenario)


def test_run_dry_run_makes_no_http(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    scenario = run.load_scenario(_write(tmp_path, SCENARIO))
    assert run.run(None, scenario, dry_run=True)
    out = capsys.readouterr().out
    assert "would PUT JournalTransaction [batch]" in out
    assert "would invoke JournalTransaction/ReleaseJournalTransaction" in out
    assert "would check AccountSummaryInquiry [Account=30000]" in out


def test_run_capture_missing_field_is_error(tmp_path: Path, instance: Instance) -> None:
    text = SCENARIO.replace(
        "capture: { BatchNbr: batch_nbr }", "capture: { NoSuchField: batch_nbr }"
    )
    scenario = run.load_scenario(_write(tmp_path, text))
    with pytest.raises(RuntimeError, match="capture field 'NoSuchField'"):
        run.run(_client(instance, Server()), scenario)
