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
    with pytest.raises(SystemExit, match="put, action, get are exclusive"):
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


GET_SCENARIO = """\
scenario: get-capture
steps:
  - id: batch
    put: JournalTransaction
    record: { Module: GL, Hold: false }
    capture: { BatchNbr: batch_nbr }
  - id: read
    get:
      entity: JournalTransaction
      keys: [GL, "${batch_nbr}"]
      expand: [Details]
    capture:
      Status: batch_status
      Details[0].Account: first_account
"""


def test_get_step_captures_paths(tmp_path: Path, instance: Instance) -> None:
    """T66: get fetches under $expand and capture walks dotted/indexed paths."""

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/JournalTransaction") and request.method == "PUT":
            return httpx.Response(
                200, json=wrap({"Module": "GL", "BatchNbr": "000042"})
            )
        if request.url.path.endswith("/JournalTransaction/GL/000042"):
            assert request.url.params["$expand"] == "Details"
            return httpx.Response(
                200,
                json={
                    "BatchNbr": {"value": "000042"},
                    "Status": {"value": "Posted"},
                    "Details": [
                        {"Account": {"value": "10100"}, "id": "g1"},
                        {"Account": {"value": "30000"}, "id": "g2"},
                    ],
                },
            )
        return httpx.Response(200, json={})

    scenario = run.load_scenario(_write(tmp_path, GET_SCENARIO))
    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    client.poll_interval = 0.0
    # reach into the engine: run the steps and inspect the variable set
    variables: dict[str, object] = {}
    for step in scenario.steps:
        run._run_step(client, step, variables)  # pyright: ignore[reportPrivateUsage]
    assert variables == {
        "batch_nbr": "000042",
        "batch_status": "Posted",
        "first_account": "10100",
    }


FILTER_GET_SCENARIO = """\
scenario: filter-capture
steps:
  - id: find-bill
    get:
      entity: Bill
      filter: "VendorRef eq 'PO-000001'"
      top: 1
      orderby: ReferenceNbr desc
    capture: { ReferenceNbr: bill_nbr }
"""


def test_get_step_filter_captures_first_row(
    tmp_path: Path, instance: Instance
) -> None:
    """List GET via filter finds auto-created docs (CreateBill VendorRef)."""

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Bill") and request.method == "GET":
            assert request.url.params["$filter"] == "VendorRef eq 'PO-000001'"
            assert request.url.params["$top"] == "1"
            assert request.url.params["$orderby"] == "ReferenceNbr desc"
            return httpx.Response(
                200,
                json=[
                    {
                        "ReferenceNbr": {"value": "000099"},
                        "VendorRef": {"value": "PO-000001"},
                    }
                ],
            )
        return httpx.Response(200, json={})

    scenario = run.load_scenario(_write(tmp_path, FILTER_GET_SCENARIO))
    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    variables: dict[str, object] = {}
    for step in scenario.steps:
        run._run_step(client, step, variables)  # pyright: ignore[reportPrivateUsage]
    assert variables == {"bill_nbr": "000099"}


def test_get_step_requires_keys_or_filter(tmp_path: Path) -> None:
    text = """\
scenario: bad-get
steps:
  - id: bad
    get: { entity: Bill }
"""
    with pytest.raises(SystemExit, match="exactly one of keys, filter"):
        run.load_scenario(_write(tmp_path, text))


def test_get_step_capture_path_errors_name_the_path(
    tmp_path: Path, instance: Instance
) -> None:
    text = GET_SCENARIO.replace("Details[0].Account", "Details[9].Account")
    scenario = run.load_scenario(_write(tmp_path, text))

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(200, json=wrap({"BatchNbr": "000042"}))
        return httpx.Response(
            200,
            json={
                "Status": {"value": "Posted"},
                "Details": [{"Account": {"value": "10100"}, "id": "g1"}],
            },
        )

    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    variables: dict[str, object] = {}
    run._run_step(client, scenario.steps[0], variables)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match=r"Details\[9\]\.Account.*out of range"):
        run._run_step(client, scenario.steps[1], variables)  # pyright: ignore[reportPrivateUsage]


def test_step_rejects_two_ops_with_get(tmp_path: Path) -> None:
    text = GET_SCENARIO.replace(
        "    get:", "    put: X\n    record: { A: 1 }\n    get:"
    )
    with pytest.raises(SystemExit, match="exclusive"):
        run.load_scenario(_write(tmp_path, text))


ONCE_SCENARIO = """\
scenario: seed-capital
once: true
present:
  inquire: AccountSummaryInquiry
  parameters: { Ledger: ACTUAL, Period: "072026" }
  match: { Account: "30000" }
  when:
    EndingBalance: { gte: 50000 }
steps:
  - id: seed
    put: JournalTransaction
    record:
      Module: GL
      Hold: false
      Details:
        - { Account: '10100', DebitAmount: 50000.0 }
        - { Account: '30000', CreditAmount: 50000.0 }
    capture: { BatchNbr: je }
expect:
  - get: { entity: JournalTransaction, keys: [GL, "${je}"] }
    fields: { Status: Balanced }
"""


def test_load_once_requires_present(tmp_path: Path) -> None:
    text = """\
scenario: bad-once
once: true
steps: []
"""
    with pytest.raises(SystemExit, match="once: true requires present"):
        run.load_scenario(_write(tmp_path, text))


def test_load_present_requires_once(tmp_path: Path) -> None:
    text = """\
scenario: bad-present
present:
  inquire: AccountSummaryInquiry
  when: { EndingBalance: { gte: 1 } }
steps: []
"""
    with pytest.raises(SystemExit, match="present requires once: true"):
        run.load_scenario(_write(tmp_path, text))


def test_load_empty_steps_stub(tmp_path: Path) -> None:
    # V28 30-build empty stub is a valid scenario
    text = """\
scenario: build
description: empty stub
steps: []
"""
    scenario = run.load_scenario(_write(tmp_path, text))
    assert scenario.steps == []
    assert scenario.expect == []


def test_once_skip_when_present(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    # T86/V4: warm capital — present probe true → skip, no put, no expect
    puts: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT" and request.url.path.endswith(
            "/AccountSummaryInquiry"
        ):
            return httpx.Response(
                200,
                json={
                    "Results": [
                        wrap({"Account": "30000", "EndingBalance": 50000.0}),
                    ]
                },
            )
        if request.method == "PUT" and request.url.path.endswith("/JournalTransaction"):
            puts.append(request.url.path)
            return httpx.Response(200, json=wrap({"BatchNbr": "000001"}))
        return httpx.Response(200, json={})

    scenario = run.load_scenario(_write(tmp_path, ONCE_SCENARIO))
    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    ok = run.run(client, scenario)
    assert ok is True
    assert puts == []  # no JE put — skip path
    out = capsys.readouterr().out
    assert "skip " in out and "(once: already present)" in out


def test_once_runs_when_absent(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    # cold path: EndingBalance 0 < gte 50000 → steps + expects run

    def server(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PUT" and path.endswith("/AccountSummaryInquiry"):
            return httpx.Response(
                200,
                json={
                    "Results": [
                        wrap({"Account": "30000", "EndingBalance": 0.0}),
                    ]
                },
            )
        if request.method == "PUT" and path.endswith("/JournalTransaction"):
            return httpx.Response(200, json=wrap({"BatchNbr": "000042"}))
        if "JournalTransaction" in path and request.method == "GET":
            return httpx.Response(200, json=wrap({"Status": "Balanced"}))
        return httpx.Response(200, json={})

    scenario = run.load_scenario(_write(tmp_path, ONCE_SCENARIO))
    client = AcumaticaClient(instance, transport=httpx.MockTransport(server))
    ok = run.run(client, scenario)
    assert ok is True
    out = capsys.readouterr().out
    assert "put JournalTransaction" in out
    assert "(once: already present)" not in out


def test_once_dry_run_annotates(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    scenario = run.load_scenario(_write(tmp_path, ONCE_SCENARIO))
    assert run.run(None, scenario, dry_run=True) is True
    out = capsys.readouterr().out
    assert "once: present AccountSummaryInquiry" in out
    assert "would PUT JournalTransaction" in out
