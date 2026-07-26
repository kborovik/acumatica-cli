"""Snapshot engine + CLI - fully offline (T93-T96, V32/V33)."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from click.testing import CliRunner

from acumatica_cli import cli, snapshot
from acumatica_cli.client import AcumaticaClient, wrap
from acumatica_cli.config import Instance

VIEW_ENTITY = """\
name: trial-balance
source:
  entity: Account
  params:
    $filter: "Active eq true"
key: [AccountCD]
capture: [Description, Type]
decimals: 2
"""

VIEW_GI = """\
name: inventory-summary
source:
  gi: LAB5-InventorySummary
  params:
    Period: "072026"
key: [InventoryID, Warehouse]
capture: [OnHand, Available]
decimals: 2
"""

VIEW_INQUIRE = """\
name: trial-balance
source:
  inquire: AccountSummaryInquiry
  params:
    Ledger: ACTUAL
    Period: "072026"
key: [Account]
capture: [Description, BegBalance, EndingBalance]
decimals: 2
"""

VIEW_INQUIRE_INV = """\
name: inventory-summary
source:
  inquire: InventorySummaryInquiry
  params:
    WarehouseID: WH01
key: [InventoryID, LocationID]
capture: [QtyOnHand]
decimals: 2
"""


def _write_view(tmp_path: Path, text: str, name: str = "10-view.yaml") -> Path:
    path = tmp_path / name
    path.write_text(text)
    return path


def test_load_view_entity(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    assert view.name == "trial-balance"
    assert view.source.kind == "entity"
    assert view.source.entity == "Account"
    assert view.key == ["AccountCD"]
    assert view.decimals == 2


def test_load_view_inquire(tmp_path: Path) -> None:
    # T103/V33: inquire: is a first-class source kind
    view = snapshot.load_view(_write_view(tmp_path, VIEW_INQUIRE))
    assert view.source.kind == "inquire"
    assert view.source.inquire == "AccountSummaryInquiry"
    assert view.source.params == {"Ledger": "ACTUAL", "Period": "072026"}
    assert view.source.match is None
    assert view.key == ["Account"]


def test_load_view_inquire_with_match(tmp_path: Path) -> None:
    text = """\
name: trial-balance
source:
  inquire: AccountSummaryInquiry
  params: {Ledger: ACTUAL, Period: "072026"}
  match: {Account: "30000"}
key: [Account]
capture: [EndingBalance]
"""
    view = snapshot.load_view(_write_view(tmp_path, text))
    assert view.source.match == {"Account": "30000"}


def test_load_view_rejects_both_sources(tmp_path: Path) -> None:
    text = """\
name: bad
source:
  gi: X
  entity: Y
key: [A]
capture: [B]
"""
    with pytest.raises(SystemExit, match="exactly one of gi, entity, inquire"):
        snapshot.load_view(_write_view(tmp_path, text))


def test_load_view_rejects_inquire_plus_entity(tmp_path: Path) -> None:
    text = """\
name: bad
source:
  inquire: AccountSummaryInquiry
  entity: Account
key: [A]
capture: [B]
"""
    with pytest.raises(SystemExit, match="exactly one of gi, entity, inquire"):
        snapshot.load_view(_write_view(tmp_path, text))


def test_load_view_rejects_neither_source(tmp_path: Path) -> None:
    text = """\
name: bad
source: {}
key: [A]
capture: [B]
"""
    with pytest.raises(SystemExit, match="exactly one of gi, entity, inquire"):
        snapshot.load_view(_write_view(tmp_path, text))


def test_load_view_match_requires_inquire(tmp_path: Path) -> None:
    text = """\
name: bad
source:
  entity: Account
  match: {AccountCD: "10000"}
key: [AccountCD]
capture: [Description]
"""
    with pytest.raises(SystemExit, match="match requires inquire"):
        snapshot.load_view(_write_view(tmp_path, text))


def test_format_cell_fixed_point() -> None:
    # T96: float formats collapse to fixed-point strings
    assert snapshot.format_cell(54192.0, 2) == "54192.00"
    assert snapshot.format_cell(54192, 2) == "54192.00"
    assert snapshot.format_cell("54192.00", 2) == "54192.00"
    assert snapshot.format_cell("54192.000000000001", 2) == "54192.00"
    assert snapshot.format_cell(54192.006, 2) == "54192.01"


def test_project_rows_allowlist_and_sort(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    live = [
        {
            "AccountCD": "20000",
            "Description": "AP",
            "Type": "Liability",
            "Extra": "noise",
        },
        {
            "AccountCD": "10000",
            "Description": "Cash",
            "Type": "Asset",
            "ServerOnly": 1,
        },
    ]
    rows = snapshot.project_rows(live, view)
    assert [r["AccountCD"] for r in rows] == ["10000", "20000"]
    assert "Extra" not in rows[0]
    assert "ServerOnly" not in rows[1]
    assert set(rows[0]) == {"AccountCD", "Description", "Type"}


def test_project_rows_key_collision(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    live = [
        {"AccountCD": "10000", "Description": "A", "Type": "Asset"},
        {"AccountCD": "10000", "Description": "B", "Type": "Asset"},
    ]
    with pytest.raises(SystemExit, match="key collision"):
        snapshot.project_rows(live, view)


def test_render_byte_identical_twice(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    live = [
        {"AccountCD": "10100", "Description": "Checking", "Type": "Asset"},
        {"AccountCD": "11000", "Description": "AR", "Type": "Asset"},
    ]
    rows = snapshot.project_rows(live, view)
    obs = snapshot.Observation(view=view.name, erp="26.101.0225", rows=rows)
    a = snapshot.render_observation(obs)
    b = snapshot.render_observation(obs)
    assert a == b
    assert "rows:\n  - {" in a
    # one row per line
    assert a.count("\n  - {") == 2


def test_render_order_shuffle_stable(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    a = [
        {"AccountCD": "20000", "Description": "AP", "Type": "Liability"},
        {"AccountCD": "10000", "Description": "Cash", "Type": "Asset"},
    ]
    b = list(reversed(a))
    ra = snapshot.render_observation(
        snapshot.Observation(
            view=view.name, erp="26.101.0225", rows=snapshot.project_rows(a, view)
        )
    )
    rb = snapshot.render_observation(
        snapshot.Observation(
            view=view.name, erp="26.101.0225", rows=snapshot.project_rows(b, view)
        )
    )
    assert ra == rb


def test_compare_observations_detects_change(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    rows = snapshot.project_rows(
        [{"AccountCD": "10000", "Description": "Cash", "Type": "Asset"}], view
    )
    live = snapshot.Observation(view=view.name, erp="26.101.0225", rows=rows)
    disk = snapshot.Observation(
        view=view.name,
        erp="26.101.0225",
        rows=snapshot.project_rows(
            [{"AccountCD": "10000", "Description": "CashX", "Type": "Asset"}], view
        ),
    )
    drifts = snapshot.compare_observations(live, disk)
    assert drifts
    assert snapshot.compare_observations(live, live) == []


def test_validate_gi_params_fail_closed() -> None:
    meta = """\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="LAB5-InventorySummary">
        <Property Name="InventoryID" Type="Edm.String"/>
        <Property Name="Period" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""
    snapshot.validate_gi_params(meta, "LAB5-InventorySummary", {"Period": "072026"})
    with pytest.raises(SystemExit, match="unknown params"):
        snapshot.validate_gi_params(
            meta, "LAB5-InventorySummary", {"Period": "072026", "Bogus": "1"}
        )


def test_validate_gi_params_empty_metadata_with_params() -> None:
    with pytest.raises(SystemExit, match="no discoverable parameter"):
        snapshot.validate_gi_params("<root/>", "X", {"Period": "1"})


def test_dry_run_no_http(tmp_path: Path) -> None:
    path = _write_view(tmp_path, VIEW_ENTITY)
    view = snapshot.load_view(path)
    code = snapshot.run_views(None, [view], out_dir=tmp_path / "state", mode="dry")
    assert code == 0
    assert not (tmp_path / "state").exists()


class _Server:
    """Minimal MockTransport router for entity + OData GI + inquire PUT."""

    def __init__(self) -> None:
        self.entity_rows: list[dict[str, Any]] = []
        self.gi_rows: list[dict[str, Any]] = []
        # T105: per-inquiry Results (AccountSummaryInquiry, InventorySummaryInquiry, …)
        self.inquire_by_entity: dict[str, list[dict[str, Any]]] = {}
        self.inquire_results: list[dict[str, Any]] = []
        self.last_inquire_entity: str | None = None
        self.last_inquire_body: dict[str, Any] | None = None
        self.last_inquire_expand: str | None = None
        self.metadata = """\
<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="LAB5-InventorySummary">
        <Property Name="InventoryID" Type="Edm.String"/>
        <Property Name="Warehouse" Type="Edm.String"/>
        <Property Name="OnHand" Type="Edm.Decimal"/>
        <Property Name="Available" Type="Edm.Decimal"/>
        <Property Name="Period" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""
        self.build = "26.101.0225"

    def _inquire_rows_for(self, entity: str) -> list[dict[str, Any]]:
        if entity in self.inquire_by_entity:
            return self.inquire_by_entity[entity]
        # backward-compat single-bucket used by older TB-only tests
        return self.inquire_results

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        # T103/T105: contract inquiry PUT $expand=Results (any inquiry entity)
        if request.method == "PUT" and "/entity/Default/" in path:
            entity = path.rstrip("/").rsplit("/", 1)[-1]
            if entity.endswith("Inquiry") or entity in self.inquire_by_entity:
                self.last_inquire_entity = entity
                self.last_inquire_body = json.loads(request.content.decode())
                self.last_inquire_expand = request.url.params.get("$expand")
                rows = self._inquire_rows_for(entity)
                return httpx.Response(
                    200,
                    json={
                        "Results": [wrap(r) for r in rows],
                    },
                )
        routes: list[tuple[bool, httpx.Response]] = [
            (
                path.endswith("/entity/auth/login"),
                httpx.Response(204),
            ),
            (
                path.endswith("/entity/auth/logout"),
                httpx.Response(204),
            ),
            (
                path.endswith("/Frames/Login.aspx"),
                httpx.Response(200, text='<input id="txtSingleCompany" value="T1" />'),
            ),
            (
                path.endswith("/entity"),
                httpx.Response(
                    200,
                    json={
                        "endpoints": [{"name": "Default", "version": "25.200.001"}],
                        "version": {"acumaticaBuildVersion": self.build},
                    },
                ),
            ),
            (
                "/entity/Default/" in path and path.rstrip("/").endswith("Account"),
                httpx.Response(200, json=[wrap(r) for r in self.entity_rows]),
            ),
            (
                path.endswith("/$metadata"),
                httpx.Response(
                    200,
                    text=self.metadata,
                    headers={"content-type": "application/xml"},
                ),
            ),
            (
                "/api/odata/gi/" in path,
                httpx.Response(200, json={"value": self.gi_rows}),
            ),
        ]
        for matched, response in routes:
            if matched:
                return response
        return httpx.Response(404, json={"message": f"unhandled {path}"})


@pytest.fixture
def server() -> _Server:
    return _Server()


@pytest.fixture
def make_client(instance: Instance, server: _Server) -> Callable[[], AcumaticaClient]:
    """Fresh client per use - context exit closes the httpx transport."""

    def _make() -> AcumaticaClient:
        return AcumaticaClient(instance, transport=httpx.MockTransport(server.handler))

    return _make


def test_entity_backend_capture(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    server.entity_rows = [
        {"AccountCD": "10100", "Description": "Checking", "Type": "Asset"},
        {"AccountCD": "11000", "Description": "AR", "Type": "Asset"},
    ]
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert obs.erp == "26.101.0225"
    assert len(obs.rows) == 2
    assert obs.rows[0]["AccountCD"] == "10100"
    text = snapshot.render_observation(obs)
    # second capture byte-identical
    with make_client() as client:
        obs2 = snapshot.capture_view(client, view)
    assert snapshot.render_observation(obs2) == text


def test_gi_backend_params_and_rows(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    server.gi_rows = [
        {
            "InventoryID": "GW-EDGE",
            "Warehouse": "MAIN",
            "OnHand": 10,
            "Available": 10,
            "Noise": "x",
        }
    ]
    view = snapshot.load_view(_write_view(tmp_path, VIEW_GI, "20-inv.yaml"))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert obs.rows[0]["OnHand"] == "10.00"
    assert "Noise" not in obs.rows[0]


def test_gi_unknown_param_exits(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    text = VIEW_GI.replace('Period: "072026"', 'Period: "072026"\n    Bogus: x')
    view = snapshot.load_view(_write_view(tmp_path, text, "20-inv.yaml"))
    with make_client() as client, pytest.raises(SystemExit, match="unknown params"):
        snapshot.capture_view(client, view)


def test_inquire_backend_capture(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    # T103/V33: PUT AccountSummaryInquiry $expand=Results → project rows
    server.inquire_results = [
        {
            "Account": "30000",
            "Description": "Owner Capital",
            "BegBalance": 0,
            "EndingBalance": 50000.0,
            "Noise": "drop-me",
        },
        {
            "Account": "10100",
            "Description": "Checking",
            "BegBalance": 0.0,
            "EndingBalance": 50000,
        },
    ]
    view = snapshot.load_view(_write_view(tmp_path, VIEW_INQUIRE))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert server.last_inquire_expand == "Results"
    # params pinned in YAML become the value-wrapped PUT body
    assert server.last_inquire_body is not None
    assert server.last_inquire_body["Ledger"]["value"] == "ACTUAL"
    assert server.last_inquire_body["Period"]["value"] == "072026"
    # key sort + fixed-point money (V32)
    assert [r["Account"] for r in obs.rows] == ["10100", "30000"]
    assert obs.rows[0]["EndingBalance"] == "50000.00"
    assert obs.rows[1]["EndingBalance"] == "50000.00"
    assert "Noise" not in obs.rows[1]


def test_inquire_match_filter(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    # T103/V33: optional match keeps only matching Results rows
    server.inquire_results = [
        {"Account": "10100", "EndingBalance": 50000},
        {"Account": "30000", "EndingBalance": 50000},
        {"Account": "11000", "EndingBalance": 0},
    ]
    text = """\
name: trial-balance
source:
  inquire: AccountSummaryInquiry
  params: {Ledger: ACTUAL, Period: "072026"}
  match: {Account: "30000"}
key: [Account]
capture: [EndingBalance]
decimals: 2
"""
    view = snapshot.load_view(_write_view(tmp_path, text))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert len(obs.rows) == 1
    assert obs.rows[0]["Account"] == "30000"
    assert obs.rows[0]["EndingBalance"] == "50000.00"


def test_inquire_empty_results(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    server.inquire_results = []
    view = snapshot.load_view(_write_view(tmp_path, VIEW_INQUIRE))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert obs.rows == []


def test_inquire_inventory_qty_fixed_point(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    # T105/V33: InventorySummaryInquiry → QtyOnHand fixed-point strings
    server.inquire_by_entity["InventorySummaryInquiry"] = [
        {
            "InventoryID": "GW-EDGE",
            "LocationID": "MAIN",
            "QtyOnHand": 10.0,
            "Noise": "drop",
        },
        {
            "InventoryID": "GW-CELL",
            "LocationID": "MAIN",
            "QtyOnHand": 5,
        },
    ]
    view = snapshot.load_view(_write_view(tmp_path, VIEW_INQUIRE_INV, "20-inv.yaml"))
    with make_client() as client:
        obs = snapshot.capture_view(client, view)
    assert server.last_inquire_entity == "InventorySummaryInquiry"
    assert server.last_inquire_expand == "Results"
    assert server.last_inquire_body is not None
    assert server.last_inquire_body["WarehouseID"]["value"] == "WH01"
    assert [r["InventoryID"] for r in obs.rows] == ["GW-CELL", "GW-EDGE"]
    assert obs.rows[0]["QtyOnHand"] == "5.00"
    assert obs.rows[1]["QtyOnHand"] == "10.00"
    assert "Noise" not in obs.rows[1]


def test_packaged_inquire_views_write_state_fixed_point(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    """T105/T107/V32/V33: packaged golden TB → state/ with EndingBalance.

    Inventory-summary is not a packaged golden this pass (B25); engine still
    covers QtyOnHand via fixture tests. Offline mock inquire for
    AccountSummaryInquiry; write observation; warm assert-unchanged green.
    """
    from importlib import resources

    pkg = resources.files("acumatica_cli") / "templates" / "config" / "snapshot"
    tb_src = (pkg / "10-trial-balance.yaml").read_text(encoding="utf-8")
    assert not (pkg / "20-inventory-summary.yaml").is_file()
    views_dir = tmp_path / "config" / "snapshot"
    views_dir.mkdir(parents=True)
    (views_dir / "10-trial-balance.yaml").write_text(tb_src)

    server.inquire_by_entity["AccountSummaryInquiry"] = [
        {
            "Account": "10100",
            "Description": "Checking",
            "BegBalance": 0,
            "DebitTotal": 50000.0,
            "CreditTotal": 3090.0,
            "EndingBalance": 46910.0,
        },
        {
            "Account": "30000",
            "Description": "Owner Capital",
            "BegBalance": 0,
            "DebitTotal": 0,
            "CreditTotal": 50000,
            "EndingBalance": 50000.0,
        },
    ]

    views = [
        snapshot.load_view(views_dir / "10-trial-balance.yaml"),
    ]
    out = tmp_path / "state"
    with make_client() as client:
        code = snapshot.run_views(client, views, out_dir=out, mode="write")
    assert code == 0

    tb_path = out / "trial-balance.yaml"
    assert tb_path.is_file()
    assert not (out / "inventory-summary.yaml").exists()

    tb = snapshot.load_observation(tb_path)
    # EndingBalance-class fixed-point (not bare float / int)
    ending = [r["EndingBalance"] for r in tb.rows]
    assert ending == ["46910.00", "50000.00"]
    assert all(isinstance(v, str) and "." in v for v in ending)

    # warm re-capture unchanged (V4/V32 idempotence of observation path)
    with make_client() as client:
        code = snapshot.run_views(client, views, out_dir=out, mode="assert")
    assert code == 0


def test_run_views_write_and_assert(
    tmp_path: Path,
    make_client: Callable[[], AcumaticaClient],
    server: _Server,
) -> None:
    server.entity_rows = [
        {"AccountCD": "10100", "Description": "Checking", "Type": "Asset"},
    ]
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    out = tmp_path / "state"
    with make_client() as client:
        code = snapshot.run_views(client, [view], out_dir=out, mode="write")
    assert code == 0
    dest = out / "trial-balance.yaml"
    assert dest.is_file()
    body = dest.read_text()
    assert "view: trial-balance" in body
    assert "10100" in body

    with make_client() as client:
        code = snapshot.run_views(client, [view], out_dir=out, mode="assert")
    assert code == 0

    server.entity_rows = [
        {"AccountCD": "10100", "Description": "Checking MOD", "Type": "Asset"},
    ]
    with make_client() as client:
        code = snapshot.run_views(client, [view], out_dir=out, mode="assert")
    assert code == 2

    with make_client() as client:
        code = snapshot.run_views(client, [view], out_dir=out, mode="diff")
    assert code == 0  # --diff: change is fine


def test_cli_snapshot_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, instance: Instance
) -> None:
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=pw\n"
    )
    # T100/V32: bare default hard-cut config/snapshot/ (not root snapshot/)
    snap = tmp_path / "config" / "snapshot"
    snap.mkdir(parents=True)
    (snap / "10-tb.yaml").write_text(VIEW_ENTITY)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("ACU_PASSWORD", "pw")
    monkeypatch.setenv("ACU_TENANT", "T1")

    class Dummy:
        def __init__(self, *a: Any, **k: Any) -> None:
            raise AssertionError("dry-run must not open a client")

    monkeypatch.setattr(cli, "AcumaticaClient", Dummy)
    # pass_instance still resolves Instance — dry-run path after that
    result = CliRunner().invoke(
        cli.cli,
        [
            "--url",
            "http://acu.test/AcumaticaERP",
            "--password",
            "pw",
            "--tenant",
            "T1",
            "snapshot",
            "--dry-run",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "would capture" in result.output
    assert "(dry run)" in result.output


def test_cli_snapshot_missing_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=pw\n"
    )
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli.cli,
        [
            "--url",
            "http://acu.test/AcumaticaERP",
            "--password",
            "pw",
            "--tenant",
            "T1",
            "snapshot",
            "--dry-run",
        ],
    )
    assert result.exit_code == 1
    assert "snapshot directory does not exist" in result.output
    # T100/T102: missing-dir names hard-cut config/snapshot (not root snapshot/)
    assert "config/snapshot" in result.output or "config\\snapshot" in result.output


def test_cli_snapshot_no_root_snapshot_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T100/V32: root snapshot/ alone is not a bare-default fallback."""
    (tmp_path / ".env").write_text(
        "ACU_BASE_URL=http://acu.test/AcumaticaERP\nACU_PASSWORD=pw\n"
    )
    legacy = tmp_path / "snapshot"
    legacy.mkdir()
    (legacy / "10-tb.yaml").write_text(VIEW_ENTITY)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(
        cli.cli,
        [
            "--url",
            "http://acu.test/AcumaticaERP",
            "--password",
            "pw",
            "--tenant",
            "T1",
            "snapshot",
            "--dry-run",
        ],
    )
    assert result.exit_code == 1
    assert "snapshot directory does not exist" in result.output


def test_observation_roundtrip_parse(tmp_path: Path) -> None:
    view = snapshot.load_view(_write_view(tmp_path, VIEW_ENTITY))
    rows = snapshot.project_rows(
        [{"AccountCD": "10100", "Description": "Checking", "Type": "Asset"}], view
    )
    obs = snapshot.Observation(view=view.name, erp="26.101.0225", rows=rows)
    path = tmp_path / "state" / "trial-balance.yaml"
    snapshot.write_observation(path, obs)
    loaded = snapshot.load_observation(path)
    assert loaded.view == obs.view
    assert loaded.erp == obs.erp
    assert loaded.rows == obs.rows
