"""Baseline parsing, normalization, and the apply/diff logic.

Live records are served by an AcumaticaClient over httpx.MockTransport, so
apply/diff run through the real client (wrap, $filter, _checked) offline.
"""

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from acumatica_cli import seed
from acumatica_cli.client import AcumaticaClient, wrap
from acumatica_cli.config import Instance

BASELINE = """\
entity: UnitsOfMeasure
key: UOM
records:
  - UOM: KG
    Description: Kilogram
  - UOM: HOUR
    Description: Hour
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "baseline.yaml"
    path.write_text(text)
    return path


def _baseline(tmp_path: Path, text: str) -> seed.BaselineFile:
    parsed = seed.load_baseline(_write(tmp_path, text))
    assert isinstance(parsed, seed.BaselineFile)
    return parsed


def test_load_baseline_parses_string_key(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path, BASELINE)
    assert baseline.entity == "UnitsOfMeasure"
    assert baseline.keys == ["UOM"]
    assert [r["UOM"] for r in baseline.records] == ["KG", "HOUR"]


def test_load_baseline_accepts_key_list(tmp_path: Path) -> None:
    text = BASELINE.replace("key: UOM", "key: [UOM, Description]")
    assert _baseline(tmp_path, text).keys == ["UOM", "Description"]


def test_load_baseline_rejects_missing_field(tmp_path: Path) -> None:
    text = BASELINE.replace("entity: UnitsOfMeasure\n", "")
    with pytest.raises(SystemExit, match="entity: Field required"):
        seed.load_baseline(_write(tmp_path, text))


def test_load_baseline_rejects_unknown_field(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="typo"):
        seed.load_baseline(_write(tmp_path, BASELINE + "typo: oops\n"))


def test_load_baseline_rejects_record_without_key(tmp_path: Path) -> None:
    text = BASELINE.replace("  - UOM: HOUR\n", "  - UOM2: HOUR\n")
    with pytest.raises(SystemExit, match=r"records\[1\] missing key field 'UOM'"):
        seed.load_baseline(_write(tmp_path, text))


def test_load_baseline_parses_endpoint_override(tmp_path: Path) -> None:
    text = BASELINE + "endpoint: Bootstrap/1.4.0\n"
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "Bootstrap/1.4.0"


LEDGER_LINK_YAML = """\
entity: LedgerCompany
key: [LedgerCD, OrganizationID]
endpoint: Bootstrap/1.4.0
records:
  - LedgerCD: ACTUAL
    OrganizationID: PRODUCTS
  - LedgerCD: ACTUAL
    OrganizationID: SERVICES
"""


def test_load_baseline_accepts_records_distinct_on_second_key_field(
    tmp_path: Path,
) -> None:
    # the multi-org shape (B21): records share LedgerCD, the pair is unique
    baseline = _baseline(tmp_path, LEDGER_LINK_YAML)
    assert len(baseline.records) == 2


def test_load_baseline_rejects_duplicate_key_tuple(tmp_path: Path) -> None:
    """V25/B21: a file whose declared key does not identify each record.

    The hard error names the entity and the first duplicated tuple - the
    hand-authored sibling of extract's row failure (an under-keyed file
    diffs as permanent false drift; apply collapses the dups to one PUT).
    """
    text = LEDGER_LINK_YAML.replace(
        "OrganizationID: SERVICES", "OrganizationID: PRODUCTS"
    )
    with pytest.raises(
        SystemExit,
        match=r"LedgerCompany.*records\[1\] duplicates key tuple \[ACTUAL, PRODUCTS\]",
    ):
        seed.load_baseline(_write(tmp_path, text))


def test_load_baseline_rejects_duplicate_single_key(tmp_path: Path) -> None:
    text = BASELINE.replace("UOM: HOUR", "UOM: KG")
    with pytest.raises(SystemExit, match=r"records\[1\] duplicates key tuple \[KG\]"):
        seed.load_baseline(_write(tmp_path, text))


AMBIGUOUS_YAML = """\
entity: Company
key: AcctCD
records:
  - AcctCD: COMPANY
"""


def test_bootstrap_entities_parsed_from_packaged_template() -> None:
    # V2/T81: the ambiguous set comes from the active contract (packaged
    # full company fallback), never a hand-list - parity pinned so a
    # template edit surfaces offline.
    assert seed.BOOTSTRAP_ENDPOINT == "Bootstrap/1.4.0"
    assert {
        "Company",
        "CreditTerms",
        "Currency",
        "LedgerCompany",
        "FinancialYearSettings",
        "MasterCalendar",
        "CompanyCalendar",
        "CompanyPeriod",
        "ManagePeriods",
        "GLPreferences",
        "INPreferences",
        "ReasonCode",
        "APPreferences",
        "ARPreferences",
        "SOPreferences",
        "POPreferences",
        "AvailabilityCalculationRule",
        "PostingClass",
        "CAPreferences",
        "VendorClass",
        "StatementCycle",
        "Warehouse",
        "OrderType",
        "CashAccount",
        "Role",
        "User",
        "NumberingSequence",
    } == seed.BOOTSTRAP_ENTITIES


def test_load_baseline_rejects_bootstrap_entity_without_endpoint(
    tmp_path: Path,
) -> None:
    """V20: entity the active Bootstrap contract serves + no endpoint: = hard error.

    The error names both endpoints; a silent Default-endpoint PUT would hit
    a different screen than the author meant (B8 class).
    """
    with pytest.raises(
        SystemExit,
        match=r"endpoint: default.*Bootstrap/1\.4\.0.*'bootstrap' \| 'default'",
    ):
        seed.load_baseline(_write(tmp_path, AMBIGUOUS_YAML))


def test_load_baseline_bootstrap_entity_explicit_endpoint_passes(
    tmp_path: Path,
) -> None:
    # V20: explicit endpoint: disambiguates - either target is legitimate
    for endpoint in ("Bootstrap/1.4.0", "Default/25.200.001", "default"):
        text = AMBIGUOUS_YAML + f"endpoint: {endpoint}\n"
        assert seed.load_baseline(_write(tmp_path, text)).endpoint == endpoint


def test_load_baseline_resolves_symbolic_bootstrap(tmp_path: Path) -> None:
    """Symbolic endpoint: bootstrap resolves to the active package version."""
    text = AMBIGUOUS_YAML + "endpoint: bootstrap\n"
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "Bootstrap/1.4.0"


def test_load_baseline_keeps_symbolic_default(tmp_path: Path) -> None:
    """V20: endpoint: default stays symbolic at load (resolved at HTTP time)."""
    text = AMBIGUOUS_YAML + "endpoint: default\n"
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "default"


def test_resolve_endpoint_default_needs_api_version() -> None:
    with pytest.raises(SystemExit, match=r"endpoint: default requires"):
        seed.resolve_endpoint("default")
    assert seed.resolve_endpoint("default", api_version="24.200.001") == (
        "Default/24.200.001"
    )


def test_apply_symbolic_default_tracks_configured_api_version(
    tmp_path: Path, instance: Instance
) -> None:
    """V20: symbolic default hits Default/<Instance.api_version>, not a pin."""
    text = BASELINE + "endpoint: default\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    versioned = instance.model_copy(update={"api_version": "24.200.001"})
    recorder = Recorder({"/UnitsOfMeasure": _live({"UOM": "KG"})})

    seed.apply(_client(versioned, recorder), baseline)

    paths = {r.url.path for r in recorder.requests}
    assert paths == {"/AcumaticaERP/entity/Default/24.200.001/UnitsOfMeasure"}


def test_active_bootstrap_rejects_data_repo_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Present data-repo project.xml hard-errors (V2/V21 package SoT)."""
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / ".env").write_text("ACU_BASE_URL=https://example.com\n")
    (tmp_path / "bootstrap" / "project.xml").write_text(
        """\
<Customization level="" description="data-repo contract" product-version="26.101">
  <EntityEndpoint>
    <Endpoint xmlns="http://www.acumatica.com/entity/maintenance/5.31"
              name="Bootstrap" version="1.4.0" systemContractVersion="4">
      <TopLevelEntity name="OnlyInDataRepo" screen="CS000000">
        <Fields><Field name="ID" type="StringValue" /></Fields>
      </TopLevelEntity>
    </Endpoint>
  </EntityEndpoint>
</Customization>
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit, match=r"package SoT.*project\.xml"):
        seed.active_bootstrap()


def test_active_bootstrap_package_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No data-repo project.xml → packaged endpoint (T178/T180)."""
    (tmp_path / ".env").write_text("ACU_BASE_URL=https://example.com\n")
    monkeypatch.chdir(tmp_path)
    name, entities = seed.active_bootstrap()
    assert name == "Bootstrap/1.4.0"
    assert entities == seed.BOOTSTRAP_ENTITIES
    text = (
        "entity: Company\nkey: AcctCD\nendpoint: bootstrap\n"
        "records:\n  - AcctCD: MAIN\n"
    )
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "Bootstrap/1.4.0"


def test_apply_and_diff_target_endpoint_override(
    tmp_path: Path, instance: Instance
) -> None:
    text = BASELINE + "endpoint: Bootstrap/1.4.0\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder({"/UnitsOfMeasure": _live({"UOM": "KG"})})

    seed.apply(_client(instance, recorder), baseline)
    seed.diff(_client(instance, recorder), baseline)

    paths = {r.url.path for r in recorder.requests}
    assert paths == {"/AcumaticaERP/entity/Bootstrap/1.4.0/UnitsOfMeasure"}


def test_norm_folds_booleans_and_strips() -> None:
    norm = seed._norm  # pyright: ignore[reportPrivateUsage]
    assert norm(True) == "true"
    assert norm("True") == "True"  # strings are NOT case-folded
    assert norm("  x  ") == "x"
    assert norm(1) == norm(1.0)  # numbers compare by value (T13)


def test_filter_for_joins_keys() -> None:
    record = {"UOM": "KG", "ToUOM": "G"}
    filter_for = seed._filter_for  # pyright: ignore[reportPrivateUsage]
    assert filter_for(record, ["UOM", "ToUOM"]) == "UOM eq 'KG' and ToUOM eq 'G'"


KIT_YAML = """\
entity: KitSpecification
key: [KitInventoryID, RevisionID]
detail_keys: { StockComponents: ComponentID }
records:
  - KitInventoryID: GW-EDGE
    RevisionID: V1
    StockComponents:
      - { ComponentID: MB-CM4, ComponentQty: 1 }
      - { ComponentID: PSU-12V, ComponentQty: 1 }
"""


def test_load_baseline_parses_detail_keys(tmp_path: Path) -> None:
    baseline = _baseline(tmp_path, KIT_YAML)
    assert baseline.detail_keys == {"StockComponents": "ComponentID"}
    assert len(baseline.records[0]["StockComponents"]) == 2


def test_load_baseline_rejects_list_field_without_detail_key(tmp_path: Path) -> None:
    # T60, the V25 sibling: diff cannot match detail rows without a key
    text = KIT_YAML.replace("detail_keys: { StockComponents: ComponentID }\n", "")
    with pytest.raises(SystemExit, match=r"StockComponents is a detail list"):
        seed.load_baseline(_write(tmp_path, text))


def test_load_baseline_rejects_duplicate_detail_key(tmp_path: Path) -> None:
    text = KIT_YAML.replace("ComponentID: PSU-12V", "ComponentID: MB-CM4")
    with pytest.raises(
        SystemExit, match=r"StockComponents\[1\] duplicates detail key \[MB-CM4\]"
    ):
        seed.load_baseline(_write(tmp_path, text))


def test_load_baseline_rejects_detail_row_missing_key_field(tmp_path: Path) -> None:
    text = KIT_YAML.replace("ComponentID: PSU-12V, ", "")
    with pytest.raises(
        SystemExit, match=r"StockComponents\[1\] missing detail key field"
    ):
        seed.load_baseline(_write(tmp_path, text))


def _kit_live(*components: dict[str, Any], extra_fields: bool = True) -> httpx.Response:
    rows = []
    for c in components:
        row: dict[str, Any] = {k: {"value": v} for k, v in c.items()}
        if extra_fields:
            # server-derived detail fields the source never claims
            row["LineNbr"] = {"value": 1}
            row["id"] = "row-guid"
        rows.append(row)
    return httpx.Response(
        200,
        json=[
            {
                "KitInventoryID": {"value": "GW-EDGE"},
                "RevisionID": {"value": "V1"},
                "StockComponents": rows,
            }
        ],
    )


def test_diff_details_clean_when_order_differs(
    tmp_path: Path, instance: Instance
) -> None:
    # order-insensitive: live rows permuted vs source, extra server-derived
    # detail fields (LineNbr, id) ignored - source-side comparison only
    baseline = seed.load_baseline(_write(tmp_path, KIT_YAML))
    recorder = Recorder(
        {
            "/KitSpecification": _kit_live(
                {"ComponentID": "PSU-12V", "ComponentQty": 1.0},
                {"ComponentID": "MB-CM4", "ComponentQty": 1.0},
            )
        }
    )
    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_details_reports_missing_extra_and_changed(
    tmp_path: Path, instance: Instance
) -> None:
    """T60/V4: the record owns its detail list.

    Missing source row, changed sub-field, and - unlike top-level records -
    an extra live row all drift.
    """
    baseline = seed.load_baseline(_write(tmp_path, KIT_YAML))
    recorder = Recorder(
        {
            "/KitSpecification": _kit_live(
                {"ComponentID": "MB-CM4", "ComponentQty": 2.0},
                {"ComponentID": "SD-32GB", "ComponentQty": 1.0},
            )
        }
    )
    drifts = seed.diff(_client(instance, recorder), baseline)
    assert (
        "KitSpecification [GW-EDGE, V1].StockComponents[MB-CM4].ComponentQty: "
        "source=1 live=2.0" in drifts
    )
    assert (
        "KitSpecification [GW-EDGE, V1].StockComponents[PSU-12V]: "
        "missing on tenant" in drifts
    )
    assert (
        "KitSpecification [GW-EDGE, V1].StockComponents[SD-32GB]: "
        "extra on tenant" in drifts
    )
    assert len(drifts) == 3


def test_apply_put_carries_unwrapped_detail_list(
    tmp_path: Path, instance: Instance
) -> None:
    # the PUT body must carry the T50-proven shape: rows wrapped, the
    # list itself bare; record absent live -> rows travel id-less
    baseline = seed.load_baseline(_write(tmp_path, KIT_YAML))
    recorder = Recorder()
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads(recorder.requests[-1].content)
    assert body["StockComponents"][0]["ComponentID"] == {"value": "MB-CM4"}
    assert isinstance(body["StockComponents"], list)
    assert "id" not in body["StockComponents"][0]


def test_apply_injects_live_detail_row_ids(tmp_path: Path, instance: Instance) -> None:
    """T60/V4: re-apply matches live detail rows by id, never re-inserts.

    The contract API matches detail rows by row GUID only (live-verified:
    an id-less re-PUT 500s "Component Item must be unique"). Matched
    source rows gain the live id; live rows the source no longer claims
    ride along as {id, delete: true} - apply converges what diff flags.
    """
    baseline = seed.load_baseline(_write(tmp_path, KIT_YAML))
    live_record = {
        "KitInventoryID": {"value": "GW-EDGE"},
        "RevisionID": {"value": "V1"},
        "StockComponents": [
            {
                "ComponentID": {"value": "MB-CM4"},
                "ComponentQty": {"value": 1.0},
                "id": "guid-mb",
            },
            {
                "ComponentID": {"value": "OBSOLETE"},
                "ComponentQty": {"value": 1.0},
                "id": "guid-old",
            },
        ],
    }
    recorder = Recorder({"/KitSpecification": httpx.Response(200, json=[live_record])})
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads(recorder.requests[-1].content)
    rows = {
        r["ComponentID"]["value"]: r
        for r in body["StockComponents"]
        if "ComponentID" in r
    }
    assert rows["MB-CM4"]["id"] == "guid-mb"  # matched -> update, id bare
    assert "id" not in rows["PSU-12V"]  # new row -> insert
    deletes = [r for r in body["StockComponents"] if r.get("delete") is True]
    assert deletes == [{"id": "guid-old", "delete": True}]


def test_filter_for_key_literals_follow_scalar_type() -> None:
    """T61: filter literals type by YAML scalar - never string-quote non-strings.

    A quoted 'false' against an Edm.Boolean field answers 500 "binary
    operator with incompatible types" (surfaced by INPreferences keyed
    HoldEntry); numeric Edm types are the same class. Strings stay quoted
    so numeric-looking codes ('000000') keep their leading zeros.
    """
    filter_for = seed._filter_for  # pyright: ignore[reportPrivateUsage]
    assert filter_for({"HoldEntry": False}, ["HoldEntry"]) == "HoldEntry eq false"
    assert filter_for({"HoldEntry": True}, ["HoldEntry"]) == "HoldEntry eq true"
    assert filter_for({"DayDue00": 30}, ["DayDue00"]) == "DayDue00 eq 30"
    assert filter_for({"SubID": "000000"}, ["SubID"]) == "SubID eq '000000'"


class Recorder:
    """Canned per-entity responses; records every request."""

    def __init__(self, respond: dict[str, httpx.Response] | None = None):
        self.requests: list[httpx.Request] = []
        self.respond = respond or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.respond.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx.Response(200, json={})


def _client(instance: Instance, recorder: Recorder) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(recorder))


def _live(*records: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json=[wrap(r) for r in records])


def test_apply_puts_every_record(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = Recorder()

    n, errors = seed.apply(_client(instance, recorder), baseline)

    assert n == 2
    assert errors == []
    assert [r.method for r in recorder.requests] == ["PUT", "PUT"]
    assert "PUT UnitsOfMeasure [KG]" in capsys.readouterr().out


def test_apply_dry_run_makes_no_calls(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = Recorder()

    n, errors = seed.apply(_client(instance, recorder), baseline, dry_run=True)

    assert n == 2
    assert errors == []
    assert recorder.requests == []
    assert "would PUT UnitsOfMeasure [KG]" in capsys.readouterr().out


class SessionRecorder:
    """MockTransport that serves auth + canned entity responses for re-login tests.

    Auth paths (login/logout/landed-tenant probe) always 204/200 so
    ``AcumaticaClient`` can open and bounce sessions; entity paths use
    ``respond`` like the plain Recorder.
    """

    def __init__(
        self,
        respond: dict[str, httpx.Response] | None = None,
        landed: str = "T1",
    ):
        self.requests: list[httpx.Request] = []
        self.respond = respond or {}
        self.landed = landed

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/auth/login") or path.endswith("/auth/logout"):
            return httpx.Response(204)
        if path.endswith("/Frames/Login.aspx"):
            # attribute order matches live probe regex (id then value)
            return httpx.Response(
                200,
                text=(
                    '<input name="ctl00$phUser$txtSingleCompany" type="hidden" '
                    f'id="txtSingleCompany" value="{self.landed}" />'
                ),
            )
        for suffix, response in self.respond.items():
            if path.endswith(suffix):
                return response
        return httpx.Response(200, json={})


def _session_client(instance: Instance, recorder: SessionRecorder) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(recorder))


COMPANY_YAML = """\
entity: Company
key: AcctCD
endpoint: Bootstrap/1.4.0
records:
  - AcctCD: LAB5
    OrganizationName: Lab Five
"""


def test_apply_company_relogins_once_per_session(
    tmp_path: Path, instance: Instance
) -> None:
    """V5/B24: first successful Company PUT → one logout+login; warm re-PUT skips."""
    baseline = seed.load_baseline(_write(tmp_path, COMPANY_YAML))
    recorder = SessionRecorder()
    with _session_client(instance, recorder) as client:
        seed.apply(client, baseline)
        seed.apply(client, baseline)  # warm re-apply: Company already exists

    methods_paths = [(r.method, r.url.path) for r in recorder.requests]
    # enter: login + probe; first Company PUT + relogin (logout+login+probe);
    # second apply: PUT only (refresh_after_company no-op); exit: logout
    assert methods_paths == [
        ("POST", "/AcumaticaERP/entity/auth/login"),
        ("GET", "/AcumaticaERP/Frames/Login.aspx"),
        ("PUT", "/AcumaticaERP/entity/Bootstrap/1.4.0/Company"),
        ("POST", "/AcumaticaERP/entity/auth/logout"),
        ("POST", "/AcumaticaERP/entity/auth/login"),
        ("GET", "/AcumaticaERP/Frames/Login.aspx"),
        ("PUT", "/AcumaticaERP/entity/Bootstrap/1.4.0/Company"),
        ("POST", "/AcumaticaERP/entity/auth/logout"),
    ]


def test_apply_company_dry_run_skips_relogin(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, COMPANY_YAML))
    recorder = SessionRecorder()
    with _session_client(instance, recorder) as client:
        seed.apply(client, baseline, dry_run=True)
    # enter login+probe, exit logout — no Company PUT, no mid-session bounce
    assert [r.method for r in recorder.requests] == ["POST", "GET", "POST"]


def test_apply_retries_once_on_branch_empty(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """V5/B24: optional hardening — one re-login + retry on branch-empty 500."""
    text = """\
entity: INPreferences
key: HoldEntry
endpoint: Bootstrap/1.4.0
records:
  - HoldEntry: false
    TransitBranchID: LAB5
"""
    baseline = seed.load_baseline(_write(tmp_path, text))
    branch_empty = httpx.Response(
        500,
        json={
            "exceptionMessage": (
                "Error: An error occurred during processing of the field "
                "Transit Branch value LAB5 TransitBranchID: 'Branch' cannot be empty."
            )
        },
    )
    ok = httpx.Response(200, json={})
    puts: list[httpx.Response] = [branch_empty, ok]

    class RetryRecorder(SessionRecorder):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "PUT" and request.url.path.endswith("/INPreferences"):
                self.requests.append(request)
                return puts.pop(0)
            return super().__call__(request)

    recorder = RetryRecorder()
    with _session_client(instance, recorder) as client:
        seed.apply(client, baseline)

    put_count = sum(1 for r in recorder.requests if r.method == "PUT")
    logout_count = sum(
        1 for r in recorder.requests if r.url.path.endswith("/auth/logout")
    )
    assert put_count == 2
    # enter login, mid-retry relogin (logout+login), exit logout
    assert logout_count == 2
    assert "re-login and retry INPreferences" in capsys.readouterr().err


def test_apply_non_company_no_relogin(tmp_path: Path, instance: Instance) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = SessionRecorder()
    with _session_client(instance, recorder) as client:
        seed.apply(client, baseline)
    # enter login+probe, two PUTs, exit logout — no mid-session bounce
    assert [r.method for r in recorder.requests] == [
        "POST",
        "GET",
        "PUT",
        "PUT",
        "POST",
    ]


def test_diff_clean_when_live_matches(tmp_path: Path, instance: Instance) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = Recorder()
    # every filter gets the KG record back: KG is clean, HOUR drifts per field
    recorder.respond["/UnitsOfMeasure"] = _live(
        {"UOM": "KG", "Description": "Kilogram"}
    )

    drifts = seed.diff(_client(instance, recorder), baseline)

    assert drifts == [
        "UnitsOfMeasure [HOUR].UOM: source='HOUR' live='KG'",
        "UnitsOfMeasure [HOUR].Description: source='Hour' live='Kilogram'",
    ]
    filters = [r.url.params["$filter"] for r in recorder.requests]
    assert filters == ["UOM eq 'KG'", "UOM eq 'HOUR'"]


def test_diff_flags_missing_record(tmp_path: Path, instance: Instance) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = Recorder({"/UnitsOfMeasure": httpx.Response(200, json=[])})

    drifts = seed.diff(_client(instance, recorder), baseline)

    assert drifts == [
        "UnitsOfMeasure [KG]: missing on tenant",
        "UnitsOfMeasure [HOUR]: missing on tenant",
    ]


def test_diff_flags_field_not_returned(tmp_path: Path, instance: Instance) -> None:
    text = BASELINE.replace("  - UOM: HOUR\n    Description: Hour\n", "")
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder({"/UnitsOfMeasure": _live({"UOM": "KG"})})

    drifts = seed.diff(_client(instance, recorder), baseline)

    assert drifts == ["UnitsOfMeasure [KG].Description: not returned by endpoint"]


USER_PASSWORD_YAML = """\
entity: User
key: Username
endpoint: bootstrap
records:
  - Username: soadmin
    FirstName: SO
    Password: secret-once
    PasswordNeverExpires: true
"""


def test_apply_puts_password_when_present_in_seed(
    tmp_path: Path, instance: Instance
) -> None:
    """V39/T147: Password is write-only — virgin PUT carries it when seed has it."""
    baseline = seed.load_baseline(_write(tmp_path, USER_PASSWORD_YAML))
    # empty list → user missing → virgin create keeps Password
    recorder = Recorder({"/User": httpx.Response(200, json=[])})
    seed.apply(_client(instance, recorder), baseline)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert body["Password"] == {"value": "secret-once"}
    assert body["Username"] == {"value": "soadmin"}


def test_apply_omits_password_when_absent_from_seed(
    tmp_path: Path, instance: Instance
) -> None:
    text = USER_PASSWORD_YAML.replace("    Password: secret-once\n", "")
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder()
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads(recorder.requests[-1].content)
    assert "Password" not in body
    assert body["Username"] == {"value": "soadmin"}


def test_apply_warm_strips_password_when_user_exists(
    tmp_path: Path, instance: Instance
) -> None:
    """V39/T148: warm re-apply drops Password — identity PUT does not reset it."""
    baseline = seed.load_baseline(_write(tmp_path, USER_PASSWORD_YAML))
    recorder = Recorder(
        {
            "/User": _live(
                {
                    "Username": "soadmin",
                    "FirstName": "SO",
                    "PasswordNeverExpires": True,
                }
            )
        }
    )
    seed.apply(_client(instance, recorder), baseline)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert "Password" not in body
    assert body["Username"] == {"value": "soadmin"}
    assert body["FirstName"] == {"value": "SO"}


ROLE_USER_YAML = {
    "role": """\
entity: Role
key: Rolename
endpoint: bootstrap
records:
  - Rolename: SO Admin
    Descr: Sales order administration
""",
    "user": """\
entity: User
key: Username
endpoint: bootstrap
detail_keys:
  Roles: Rolename
records:
  - Username: soadmin
    FirstName: SO
    LastName: Admin
    Email: soadmin@example.com
    IsApproved: true
    PasswordNeverExpires: true
    PasswordChangeOnNextLogin: false
    Roles:
      - Rolename: SO Admin
        Selected: true
""",
}


def test_apply_role_then_user_membership_virgin(
    tmp_path: Path, instance: Instance
) -> None:
    """T148/V22: Role PUT then User PUT with membership; virgin no password."""
    role_path = tmp_path / "90-roles.yaml"
    role_path.write_text(ROLE_USER_YAML["role"])
    user_path = tmp_path / "91-users.yaml"
    user_path.write_text(ROLE_USER_YAML["user"])
    role = seed.load_baseline(role_path)
    user = seed.load_baseline(user_path)
    assert isinstance(role, seed.BaselineFile)
    assert isinstance(user, seed.BaselineFile)
    # empty Role/User lists → virgin creates
    recorder = Recorder(
        {
            "/Role": httpx.Response(200, json=[]),
            "/User": httpx.Response(200, json=[]),
        }
    )
    client = _client(instance, recorder)
    seed.apply(client, role)
    seed.apply(client, user)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    assert len(puts) == 2
    assert puts[0].url.path.endswith("/Role")
    role_body = json.loads(puts[0].content)
    assert role_body["Rolename"] == {"value": "SO Admin"}
    assert puts[1].url.path.endswith("/User")
    user_body = json.loads(puts[1].content)
    assert user_body["Username"] == {"value": "soadmin"}
    assert "Password" not in user_body
    assert user_body["Roles"] == [
        {"Rolename": {"value": "SO Admin"}, "Selected": {"value": True}}
    ]


def test_apply_user_membership_warm_idempotent_no_password(
    tmp_path: Path, instance: Instance
) -> None:
    """T148: warm User re-apply injects Roles ids; package seed has no Password."""
    user_path = tmp_path / "91-users.yaml"
    user_path.write_text(ROLE_USER_YAML["user"])
    user = seed.load_baseline(user_path)
    live_user = {
        "Username": {"value": "soadmin"},
        "FirstName": {"value": "SO"},
        "LastName": {"value": "Admin"},
        "Email": {"value": "soadmin@example.com"},
        "IsApproved": {"value": True},
        "PasswordNeverExpires": {"value": True},
        "PasswordChangeOnNextLogin": {"value": False},
        "Roles": [
            {
                "Rolename": {"value": "SO Admin"},
                "Selected": {"value": True},
                "id": "guid-so-admin",
            },
            {
                "Rolename": {"value": "Administrator"},
                "Selected": {"value": False},
                "id": "guid-admin",
            },
        ],
    }
    recorder = Recorder({"/User": httpx.Response(200, json=[live_user])})
    seed.apply(_client(instance, recorder), user)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert "Password" not in body
    # matched membership keeps live id; extra live role row deleted (V4 list owns)
    roles = body["Roles"]
    so = next(r for r in roles if r.get("Rolename", {}).get("value") == "SO Admin")
    assert so["id"] == "guid-so-admin"
    assert so["Selected"] == {"value": True}
    deletes = [r for r in roles if r.get("delete") is True]
    assert deletes == [{"id": "guid-admin", "delete": True}]


def test_package_master_role_before_user_v22_order() -> None:
    """T148/V22: package 90-roles sorts before 91-users (Role before User)."""
    root = Path(__file__).resolve().parents[1] / "src" / "acumatica_cli" / "templates"
    master = sorted((root / "config/master").glob("*.yaml"))
    names = [p.name for p in master]
    assert "90-roles.yaml" in names
    assert "91-users.yaml" in names
    assert names.index("90-roles.yaml") < names.index("91-users.yaml")
    # expand_files leaf-dir order matches alpha (V22 sole order within dir)
    from acumatica_cli.cli import expand_files

    expanded = expand_files((root / "config/master",))
    entities = [seed.load_baseline(p).entity for p in expanded]
    assert entities.index("Role") < entities.index("User")


def test_diff_ignores_password_fields(tmp_path: Path, instance: Instance) -> None:
    """V39/T147: seed Password vs missing/hash live is not drift."""
    baseline = seed.load_baseline(_write(tmp_path, USER_PASSWORD_YAML))
    # live has identity fields but no Password (or a hash) — either is fine
    recorder = Recorder(
        {
            "/User": _live(
                {
                    "Username": "soadmin",
                    "FirstName": "SO",
                    "PasswordNeverExpires": True,
                    "Password": "hash-not-seedable",
                    "b64__Password": "YmFk",
                }
            )
        }
    )
    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_still_flags_non_password_user_fields(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, USER_PASSWORD_YAML))
    recorder = Recorder(
        {
            "/User": _live(
                {
                    "Username": "soadmin",
                    "FirstName": "Other",
                    "PasswordNeverExpires": True,
                }
            )
        }
    )
    drifts = seed.diff(_client(instance, recorder), baseline)
    assert drifts == ["User [soadmin].FirstName: source='SO' live='Other'"]
    assert not any("Password" in d for d in drifts)


NUMBERING_BOUNDS_YAML = """\
entity: NumberingSequence
key: NumberingID
endpoint: bootstrap
records:
  - NumberingID: BATCH
    StartNbr: '000000'
    EndNbr: '999999'
    WarnNbr: '999990'
    NbrStep: 1
    StartDate: '1900-01-01'
"""

NUMBERING_WITH_LASTNBR_YAML = """\
entity: NumberingSequence
key: NumberingID
endpoint: bootstrap
records:
  - NumberingID: BATCH
    StartNbr: '000000'
    EndNbr: '999999'
    WarnNbr: '999990'
    NbrStep: 1
    StartDate: '1900-01-01'
    LastNbr: '000025'
"""


def test_apply_bounds_without_lastnbr(tmp_path: Path, instance: Instance) -> None:
    """V40/T152: apply of bounds-only seed never requires LastNbr."""
    baseline = seed.load_baseline(_write(tmp_path, NUMBERING_BOUNDS_YAML))
    recorder = Recorder({"/NumberingSequence": httpx.Response(200, json=[])})
    seed.apply(_client(instance, recorder), baseline)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert body["NumberingID"] == {"value": "BATCH"}
    assert body["StartNbr"] == {"value": "000000"}
    assert body["EndNbr"] == {"value": "999999"}
    assert body["WarnNbr"] == {"value": "999990"}
    assert body["NbrStep"] == {"value": 1}
    assert "LastNbr" not in body


def test_apply_strips_lastnbr_when_present_in_seed(
    tmp_path: Path, instance: Instance
) -> None:
    """V40/T152: hand-authored LastNbr never PUTs — live counters stay put."""
    baseline = seed.load_baseline(_write(tmp_path, NUMBERING_WITH_LASTNBR_YAML))
    recorder = Recorder({"/NumberingSequence": httpx.Response(200, json=[])})
    seed.apply(_client(instance, recorder), baseline)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert "LastNbr" not in body
    assert body["NumberingID"] == {"value": "BATCH"}
    assert body["StartNbr"] == {"value": "000000"}


def test_diff_ignores_lastnbr_fields(tmp_path: Path, instance: Instance) -> None:
    """V40/T152: seed LastNbr vs advanced live counter is not drift."""
    baseline = seed.load_baseline(_write(tmp_path, NUMBERING_WITH_LASTNBR_YAML))
    recorder = Recorder(
        {
            "/NumberingSequence": _live(
                {
                    "NumberingID": "BATCH",
                    "StartNbr": "000000",
                    "EndNbr": "999999",
                    "WarnNbr": "999990",
                    "NbrStep": 1,
                    "StartDate": "1900-01-01",
                    "LastNbr": "000099",  # advanced beyond seed — not drift
                }
            )
        }
    )
    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_ignores_live_lastnbr_when_seed_omits(
    tmp_path: Path, instance: Instance
) -> None:
    """V40/T152: package bounds seed vs live with LastNbr is clean."""
    baseline = seed.load_baseline(_write(tmp_path, NUMBERING_BOUNDS_YAML))
    recorder = Recorder(
        {
            "/NumberingSequence": _live(
                {
                    "NumberingID": "BATCH",
                    "StartNbr": "000000",
                    "EndNbr": "999999",
                    "WarnNbr": "999990",
                    "NbrStep": 1,
                    "StartDate": "1900-01-01",
                    "LastNbr": "000042",
                }
            )
        }
    )
    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_still_flags_numbering_bounds_drift(
    tmp_path: Path, instance: Instance
) -> None:
    """V40/T152: bounds fields still compare; only LastNbr is ignored."""
    baseline = seed.load_baseline(_write(tmp_path, NUMBERING_BOUNDS_YAML))
    recorder = Recorder(
        {
            "/NumberingSequence": _live(
                {
                    "NumberingID": "BATCH",
                    "StartNbr": "000000",
                    "EndNbr": "500000",  # drifted bound
                    "WarnNbr": "999990",
                    "NbrStep": 1,
                    "StartDate": "1900-01-01",
                    "LastNbr": "000099",
                }
            )
        }
    )
    drifts = seed.diff(_client(instance, recorder), baseline)
    assert drifts == ["NumberingSequence [BATCH].EndNbr: source='999999' live='500000'"]
    assert not any("LastNbr" in d for d in drifts)


def _package_template(rel: str) -> Path:
    return (
        Path(__file__).resolve().parents[1]
        / "src"
        / "acumatica_cli"
        / "templates"
        / rel
    )


def test_package_in_preferences_apply_body_includes_deepen_fields(
    instance: Instance,
) -> None:
    """T157/V41: package INPreferences PUT carries numbering + policy fields."""
    baseline = seed.load_baseline(
        _package_template("config/master/20-in-preferences.yaml")
    )
    assert isinstance(baseline, seed.BaselineFile)
    recorder = Recorder({"/INPreferences": httpx.Response(200, json=[])})
    seed.apply(_client(instance, recorder), baseline)
    puts = [r for r in recorder.requests if r.method == "PUT"]
    body = json.loads(puts[-1].content)
    assert body["HoldEntry"] == {"value": False}
    assert body["BatchNumberingID"] == {"value": "BATCH"}
    assert body["ReceiptNumberingID"] == {"value": "INRECEIPT"}
    assert body["IssueNumberingID"] == {"value": "INISSUE"}
    assert body["AdjustmentNumberingID"] == {"value": "INADJUST"}
    assert body["KitAssemblyNumberingID"] == {"value": "INKITASSY"}
    assert body["AutoPost"] == {"value": True}
    assert body["SummPost"] == {"value": False}
    assert body["NegQty"] == {"value": False}
    assert body["RequireControlTotal"] == {"value": True}
    # V41: order-dependent FKs never claimed
    assert "DfltLotSerClassID" not in body
    assert "TransitSiteID" not in body


def test_package_gl_preferences_apply_body_includes_deepen_fields(
    instance: Instance,
) -> None:
    """T157/V41: package GLPreferences PUT carries batch numbering + policy."""
    baseline = seed.load_baseline(
        _package_template("config/baseline/50-gl-preferences.yaml")
    )
    assert isinstance(baseline, seed.BaselineFile)
    recorder = Recorder({"/GLPreferences": httpx.Response(200, json=[])})
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads([r for r in recorder.requests if r.method == "PUT"][-1].content)
    assert body["RetEarnAccountID"] == {"value": "32000"}
    assert body["YtdNetIncAccountID"] == {"value": "33000"}
    assert body["BatchNumberingID"] == {"value": "BATCH"}
    assert body["AutoPostOption"] == {"value": True}
    assert body["HoldEntry"] == {"value": True}
    assert body["RequireControlTotal"] == {"value": False}


def test_package_ap_ar_so_po_ca_preferences_claim_deepen_fields() -> None:
    """T156/T157: package AR/AP/SO/PO/CA prefs claim curated fields only."""
    ar = seed.load_baseline(_package_template("config/master/60-ar-preferences.yaml"))
    assert isinstance(ar, seed.BaselineFile)
    rec = ar.records[0]
    assert rec["InvoiceNumberingID"] == "ARINVOICE"
    assert rec["PaymentNumberingID"] == "ARPAYMENT"
    assert rec["RequireExtRef"] is True
    assert rec["CreditCheckError"] is True

    ap = seed.load_baseline(_package_template("config/master/61-ap-preferences.yaml"))
    assert isinstance(ap, seed.BaselineFile)
    rec = ap.records[0]
    assert rec["InvoiceNumberingID"] == "APBILL"
    assert rec["CheckNumberingID"] == "APPAYMENT"
    assert rec["RequireVendorRef"] is True
    assert rec["RequireApprovePayments"] is True

    so = seed.load_baseline(_package_template("config/master/56-so-preferences.yaml"))
    assert isinstance(so, seed.BaselineFile)
    assert so.records[0]["ShipmentNumberingID"] == "SOSHIPMENT"
    assert so.records[0]["CreditCheckError"] is True

    po = seed.load_baseline(_package_template("config/master/57-po-preferences.yaml"))
    assert isinstance(po, seed.BaselineFile)
    assert po.records[0]["RegularPONumberingID"] == "POORDER"
    assert po.records[0]["ReceiptNumberingID"] == "PORECEIPT"
    assert po.records[0]["AutoReleaseAP"] is False

    ca = seed.load_baseline(_package_template("config/master/62-ca-preferences.yaml"))
    assert isinstance(ca, seed.BaselineFile)
    rec = ca.records[0]
    assert rec["BatchNumberingID"] == "BATCH"
    assert rec["AutoPostOption"] is True
    assert rec["ReleaseAP"] is True
    assert rec["ReleaseAR"] is True
    assert "TransferNumberingID" not in rec


def test_package_prefs_diff_clean_when_live_matches_seed(
    instance: Instance,
) -> None:
    """T157: deepened fields compare clean; no permanent drift from extras."""
    baseline = seed.load_baseline(
        _package_template("config/master/20-in-preferences.yaml")
    )
    assert isinstance(baseline, seed.BaselineFile)
    live = {
        "HoldEntry": False,
        "INProgressAcctID": "12300",
        "INProgressSubID": "000000",
        "INTransitAcctID": "12400",
        "INTransitSubID": "000000",
        "TransitBranchID": "LAB5",
        "UpdateGL": True,
        "IssuesReasonCode": "INISSUE",
        "ReceiptReasonCode": "INRECEIPT",
        "AdjustmentReasonCode": "INADJUST",
        "PIReasonCode": "INPI",
        "BatchNumberingID": "BATCH",
        "ReceiptNumberingID": "INRECEIPT",
        "IssueNumberingID": "INISSUE",
        "AdjustmentNumberingID": "INADJUST",
        "KitAssemblyNumberingID": "INKITASSY",
        "AutoPost": True,
        "SummPost": False,
        "NegQty": False,
        "RequireControlTotal": True,
        # server-only noise must not dirty seed-claimed surface (B11 class)
        "LastModifiedDateTime": "2026-07-30T00:00:00+00:00",
        "PerRetainTran": 99,
    }
    recorder = Recorder({"/INPreferences": _live(live)})
    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_normalizes_booleans(tmp_path: Path, instance: Instance) -> None:
    text = "entity: E\nkey: K\nrecords:\n  - K: A\n    Active: true\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    # live returns the Python bool True; source YAML parses to bool too
    recorder = Recorder({"/E": _live({"K": "A", "Active": True})})

    assert seed.diff(_client(instance, recorder), baseline) == []


def test_diff_multi_key_filters_first_key_only(
    tmp_path: Path, instance: Instance
) -> None:
    """B14/B21: a multi-key read-back never sends a cross-view $filter AND.

    The list GET filters on the first (primary-view) key alone - a
    conjunction spanning views answers 200 [] while each predicate alone
    matches - and the remaining key fields pick the record client-side,
    so each of a multi-org tenant's links diffs against its own row.
    """
    baseline = _baseline(tmp_path, LEDGER_LINK_YAML)
    recorder = Recorder(
        {
            "/LedgerCompany": _live(
                {"LedgerCD": "ACTUAL", "OrganizationID": "CAPITAL"},
                {"LedgerCD": "ACTUAL", "OrganizationID": "PRODUCTS"},
                {"LedgerCD": "ACTUAL", "OrganizationID": "SERVICES"},
            )
        }
    )

    assert seed.diff(_client(instance, recorder), baseline) == []
    filters = [r.url.params["$filter"] for r in recorder.requests]
    assert filters == ["LedgerCD eq 'ACTUAL'", "LedgerCD eq 'ACTUAL'"]


def test_diff_multi_key_no_matching_row_is_missing(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = _baseline(tmp_path, LEDGER_LINK_YAML)
    recorder = Recorder(
        {"/LedgerCompany": _live({"LedgerCD": "ACTUAL", "OrganizationID": "CAPITAL"})}
    )

    drifts = seed.diff(_client(instance, recorder), baseline)

    assert drifts == [
        "LedgerCompany [ACTUAL, PRODUCTS]: missing on tenant",
        "LedgerCompany [ACTUAL, SERVICES]: missing on tenant",
    ]


def test_diff_multi_key_single_org_no_phantom_drift(
    tmp_path: Path, instance: Instance
) -> None:
    # the B14 regression leg: one link per ledger (the single-org tenant)
    # reads back clean under the pair key - no "missing on tenant"
    text = """\
entity: LedgerCompany
key: [LedgerCD, OrganizationID]
endpoint: Bootstrap/1.4.0
records:
  - LedgerCD: ACTUAL
    OrganizationID: COMPANY
"""
    baseline = _baseline(tmp_path, text)
    recorder = Recorder(
        {"/LedgerCompany": _live({"LedgerCD": "ACTUAL", "OrganizationID": "COMPANY"})}
    )

    assert seed.diff(_client(instance, recorder), baseline) == []


OPTIMIZATION_500 = httpx.Response(
    500,
    json={
        "message": "An error has occurred.",
        "exceptionMessage": (
            "Optimization cannot be performed.The following fields cause "
            "the error:\r\nRealGainAcctID: View CuryRecords has BQL delegate"
        ),
    },
)
NO_ENTITY_500 = httpx.Response(
    500,
    json={
        "message": "An error has occurred.",
        "exceptionMessage": "No entity satisfies the condition.",
        "exceptionType": (
            "PX.Api.ContractBased.NoEntitySatisfiesTheConditionException"
        ),
    },
)
CURRENCY_YAML = """\
entity: Currency
key: CuryID
endpoint: Bootstrap/1.4.0
records:
  - CuryID: EUR
    Description: Euro
"""


def test_diff_falls_back_to_key_url_on_optimization_500(
    tmp_path: Path, instance: Instance
) -> None:
    """B9: the list GET's optimized export 500s on delegate-view fields.

    diff retries the record via the key-URL single-record GET (verified vs
    26.101.0225 - the key-URL form skips the optimizer).
    """
    baseline = seed.load_baseline(_write(tmp_path, CURRENCY_YAML))
    recorder = Recorder(
        {
            "/Currency": OPTIMIZATION_500,
            "/Currency/EUR": httpx.Response(
                200, json=wrap({"CuryID": "EUR", "Description": "Euro"})
            ),
        }
    )

    assert seed.diff(_client(instance, recorder), baseline) == []
    paths = [r.url.path for r in recorder.requests]
    assert [p.split("/entity/", 1)[1] for p in paths] == [
        "Bootstrap/1.4.0/Currency",
        "Bootstrap/1.4.0/Currency/EUR",
    ]


def test_diff_fallback_flags_missing_record(tmp_path: Path, instance: Instance) -> None:
    # missing on the key-URL form = 500 NoEntitySatisfiesTheCondition-
    # Exception, not 404 or an empty list (verified vs 26.101.0225)
    baseline = seed.load_baseline(_write(tmp_path, CURRENCY_YAML))
    recorder = Recorder({"/Currency": OPTIMIZATION_500, "/Currency/EUR": NO_ENTITY_500})

    drifts = seed.diff(_client(instance, recorder), baseline)

    assert drifts == ["Currency [EUR]: missing on tenant"]


def test_diff_non_optimization_500_still_raises(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, CURRENCY_YAML))
    recorder = Recorder(
        {"/Currency": httpx.Response(500, json={"exceptionMessage": "boom"})}
    )

    with pytest.raises(RuntimeError, match="boom"):
        seed.diff(_client(instance, recorder), baseline)


ACTION_YAML = """\
action: GenerateCalendar
entity: MasterCalendar
endpoint: Bootstrap/1.4.0
record:
  FinancialYear: 2026
parameters:
  FromYear: 2026
  ToYear: 2026
done_when:
  filter: FinancialYear eq '2026'
"""


def test_load_baseline_dispatches_on_action_key(tmp_path: Path) -> None:
    parsed = seed.load_baseline(_write(tmp_path, ACTION_YAML))
    assert isinstance(parsed, seed.ActionFile)
    assert parsed.action == "GenerateCalendar"
    assert parsed.entity == "MasterCalendar"
    assert parsed.record == {"FinancialYear": 2026}
    assert parsed.parameters == {"FromYear": 2026, "ToYear": 2026}
    # done_when entity/endpoint omitted -> None here, action's own at probe time
    assert parsed.done_when.entity is None
    assert parsed.done_when.filter == "FinancialYear eq '2026'"


def test_load_action_file_rejects_unknown_field(tmp_path: Path) -> None:
    # V10: frozen models, extra="forbid" - typos surface at the parse boundary
    with pytest.raises(SystemExit, match="typo"):
        seed.load_baseline(_write(tmp_path, ACTION_YAML + "typo: oops\n"))


def test_load_action_file_requires_done_when(tmp_path: Path) -> None:
    # V4: no probe, no verify gate - an unprobed action can never skip or diff
    text = ACTION_YAML.split("done_when:", maxsplit=1)[0]
    with pytest.raises(SystemExit, match="done_when: Field required"):
        seed.load_baseline(_write(tmp_path, text))


def _action(tmp_path: Path, text: str = ACTION_YAML) -> seed.ActionFile:
    parsed = seed.load_baseline(_write(tmp_path, text))
    assert isinstance(parsed, seed.ActionFile)
    return parsed


def test_apply_action_skips_when_done_when_non_empty(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    # V4: the skip gate is the done_when live-state probe, never a marker
    action = _action(tmp_path)
    recorder = Recorder({"/MasterCalendar": _live({"FinancialYear": "2026"})})

    seed.apply(_client(instance, recorder), action)

    assert [r.method for r in recorder.requests] == ["GET"]
    assert "skip GenerateCalendar (already done)" in capsys.readouterr().out


def test_apply_action_invokes_on_204_never_following_location(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """204 = done; its Location header is bogus and never polled (T36 live)."""
    action = _action(tmp_path)
    recorder = Recorder(
        {
            "/MasterCalendar": httpx.Response(200, json=[]),
            "/GenerateCalendar": httpx.Response(
                204, headers={"Location": "/AcumaticaERP/entity/bogus/status/nope"}
            ),
        }
    )

    seed.apply(_client(instance, recorder), action)

    assert [
        (r.method, r.url.path.split("/entity/", 1)[1]) for r in recorder.requests
    ] == [
        ("GET", "Bootstrap/1.4.0/MasterCalendar"),
        ("POST", "Bootstrap/1.4.0/MasterCalendar/GenerateCalendar"),
    ]
    assert "invoke GenerateCalendar [MasterCalendar]" in capsys.readouterr().out


def test_apply_action_wraps_both_payloads(tmp_path: Path, instance: Instance) -> None:
    action = _action(tmp_path)
    recorder = Recorder(
        {
            "/MasterCalendar": httpx.Response(200, json=[]),
            "/GenerateCalendar": httpx.Response(204),
        }
    )

    seed.apply(_client(instance, recorder), action)

    body = json.loads(recorder.requests[-1].content)
    assert body == {
        "entity": wrap({"FinancialYear": 2026}),
        "parameters": wrap({"FromYear": 2026, "ToYear": 2026}),
    }


def test_apply_action_polls_202_location_to_completion(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """202 = long-running: poll the Location status URL until it answers 204."""
    action = _action(tmp_path)
    status_path = (
        "/AcumaticaERP/entity/Bootstrap/1.4.0/MasterCalendar"
        "/GenerateCalendar/status/abc"
    )
    polls: list[str] = []
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("/status/abc"):
            polls.append(request.method)
            return httpx.Response(202 if len(polls) < 2 else 204)
        if path.endswith("/GenerateCalendar"):
            return httpx.Response(202, headers={"Location": status_path})
        return httpx.Response(200, json=[])  # the done_when probe: empty

    client = AcumaticaClient(instance, transport=httpx.MockTransport(handler))
    client.poll_interval = 0  # offline: no wall-clock waits

    seed.apply(client, action)

    assert polls == ["GET", "GET"]
    assert requests[-1].url.path == status_path
    assert "invoke GenerateCalendar [MasterCalendar]" in capsys.readouterr().out


def test_apply_action_dry_run_makes_no_calls(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    action = _action(tmp_path)
    recorder = Recorder()

    n, errors = seed.apply(_client(instance, recorder), action, dry_run=True)

    assert n == 1
    assert errors == []
    assert recorder.requests == []
    assert "would invoke GenerateCalendar" in capsys.readouterr().out


def test_diff_action_drifts_when_probe_empty(
    tmp_path: Path, instance: Instance
) -> None:
    # V4: a tenant that lost the action's effect must not diff false-green
    action = _action(tmp_path)
    recorder = Recorder({"/MasterCalendar": httpx.Response(200, json=[])})

    drifts = seed.diff(_client(instance, recorder), action)

    assert drifts == ["action GenerateCalendar: not applied"]


def test_diff_action_clean_when_probe_non_empty(
    tmp_path: Path, instance: Instance
) -> None:
    action = _action(tmp_path)
    recorder = Recorder({"/MasterCalendar": _live({"FinancialYear": "2026"})})

    assert seed.diff(_client(instance, recorder), action) == []


def test_probe_routes_filter_and_defaults(tmp_path: Path, instance: Instance) -> None:
    """done_when entity/endpoint default to the action's; filter rides $filter."""
    action = _action(tmp_path)
    recorder = Recorder({"/MasterCalendar": _live({"FinancialYear": "2026"})})

    seed.diff(_client(instance, recorder), action)

    (request,) = recorder.requests
    assert request.url.path.endswith("/Bootstrap/1.4.0/MasterCalendar")
    assert request.url.params["$filter"] == "FinancialYear eq '2026'"


def test_probe_honors_done_when_overrides(tmp_path: Path, instance: Instance) -> None:
    text = ACTION_YAML.replace(
        "done_when:\n  filter: FinancialYear eq '2026'\n",
        "done_when:\n  entity: FinPeriod\n  endpoint: Default/25.200.001\n",
    )
    action = _action(tmp_path, text)
    recorder = Recorder({"/FinPeriod": _live({"PeriodID": "012026"})})

    assert seed.diff(_client(instance, recorder), action) == []
    (request,) = recorder.requests
    assert request.url.path.endswith("/Default/25.200.001/FinPeriod")
    assert "$filter" not in request.url.params


def test_diff_normalizes_numbers_by_value(tmp_path: Path, instance: Instance) -> None:
    # DecimalValue fields come back as floats: YAML 0 vs live 0.0 is not
    # drift (T13: CreditTerms.DiscPercent), and 0 vs 0.5 still is
    text = "entity: E\nkey: K\nrecords:\n  - K: A\n    Pct: 0\n    Days: 30\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder({"/E": _live({"K": "A", "Pct": 0.0, "Days": 30})})

    assert seed.diff(_client(instance, recorder), baseline) == []

    recorder.respond["/E"] = _live({"K": "A", "Pct": 0.5, "Days": 30})
    assert seed.diff(_client(instance, recorder), baseline) == [
        "E [A].Pct: source=0 live=0.5"
    ]


VENDOR_YAML = """\
entity: Vendor
key: VendorID
records:
  - VendorID: SHENZHEN
    VendorName: Shenzhen Circuit Supply
    MainContact:
      Address:
        Country: US
"""


def test_apply_wraps_linked_entity_bare(tmp_path: Path, instance: Instance) -> None:
    # T65: a nested dict is a linked entity - bare nested object, fields
    # wrapped (the live-verified Vendor MainContact/Address/Country shape)
    baseline = seed.load_baseline(_write(tmp_path, VENDOR_YAML))
    recorder = Recorder()
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads(recorder.requests[-1].content)
    assert body["MainContact"] == {"Address": {"Country": {"value": "US"}}}


def test_fetch_expands_linked_entity_paths(tmp_path: Path, instance: Instance) -> None:
    # T65: the expand set derives from the record shape - dict fields by
    # slash path, and diff's read must carry it or nested fields vanish
    baseline = seed.load_baseline(_write(tmp_path, VENDOR_YAML))
    live = {
        "VendorID": {"value": "SHENZHEN"},
        "VendorName": {"value": "Shenzhen Circuit Supply"},
        "MainContact": {"Address": {"Country": {"value": "US"}}},
    }
    recorder = Recorder({"/Vendor": httpx.Response(200, json=[live])})
    assert seed.diff(_client(instance, recorder), baseline) == []
    (request,) = recorder.requests
    assert request.url.params["$expand"] == "MainContact,MainContact/Address"


ACCOUNT_CUSTOM_YAML = """\
entity: Account
key: AccountCD
records:
  - AccountCD: "11000"
    Description: Accounts Receivable
    custom:
      AccountRecords:
        ControlAccountModule: AR
"""


def test_custom_param_from_seed_bag() -> None:
    assert (
        seed._custom_param(
            {
                "AccountCD": "11000",
                "custom": {
                    "AccountRecords": {
                        "ControlAccountModule": "AR",
                        "AllowManualEntry": False,
                    }
                },
            }
        )
        == "AccountRecords.AllowManualEntry,AccountRecords.ControlAccountModule"
    )
    assert seed._custom_param({"AccountCD": "11000"}) is None
    assert seed._custom_param({"custom": {}}) is None


def test_expand_paths_skips_top_level_custom() -> None:
    paths = seed._expand_paths(
        {
            "AccountCD": "11000",
            "MainContact": {"Address": {"Country": "US"}},
            "custom": {"AccountRecords": {"ControlAccountModule": "AR"}},
        }
    )
    assert paths == ["MainContact", "MainContact/Address"]
    assert not any(p.startswith("custom") for p in paths)


def test_fetch_requests_custom_query_param(
    tmp_path: Path, instance: Instance
) -> None:
    # Contract custom fields are not $expand — they need $custom=View.Field
    # or GET omits them and control-account seed permanently drifts.
    baseline = seed.load_baseline(_write(tmp_path, ACCOUNT_CUSTOM_YAML))
    live = {
        "AccountCD": {"value": "11000"},
        "Description": {"value": "Accounts Receivable"},
        "custom": {
            "AccountRecords": {
                "ControlAccountModule": {
                    "type": "CustomStringField",
                    "value": "AR",
                }
            }
        },
    }
    recorder = Recorder({"/Account": httpx.Response(200, json=[live])})
    assert seed.diff(_client(instance, recorder), baseline) == []
    (request,) = recorder.requests
    assert "$expand" not in request.url.params
    assert request.url.params["$custom"] == "AccountRecords.ControlAccountModule"


def test_apply_puts_custom_control_account(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, ACCOUNT_CUSTOM_YAML))
    recorder = Recorder()
    seed.apply(_client(instance, recorder), baseline)
    body = json.loads(recorder.requests[-1].content)
    assert body["custom"] == {
        "AccountRecords": {"ControlAccountModule": {"value": "AR"}}
    }


def test_diff_nested_reports_changed_and_missing(
    tmp_path: Path, instance: Instance
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, VENDOR_YAML))
    live = {
        "VendorID": {"value": "SHENZHEN"},
        "VendorName": {"value": "Shenzhen Circuit Supply"},
        "MainContact": {"Address": {"Country": {"value": "CA"}}},
    }
    recorder = Recorder({"/Vendor": httpx.Response(200, json=[live])})
    drifts = seed.diff(_client(instance, recorder), baseline)
    assert drifts == [
        "Vendor [SHENZHEN].MainContact.Address.Country: source='US' live='CA'"
    ]


# -- V45: apply per-record failure isolation (T184/T185) --


class _SequencedPutRecorder(Recorder):
    """Recorder whose PUT responses come from a queue (then default 200)."""

    def __init__(self, put_responses: list[httpx.Response]) -> None:
        super().__init__()
        self._put_responses = list(put_responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            self.requests.append(request)
            if self._put_responses:
                return self._put_responses.pop(0)
            return httpx.Response(200, json={})
        return super().__call__(request)


def test_apply_first_record_fail_continues_later(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """V45/T185: first-record fail still applies later records; returns errors."""
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = _SequencedPutRecorder(
        [
            httpx.Response(
                422,
                json={
                    "exceptionMessage": (
                        "Inserting  'UnitsOfMeasure' record raised at least one error."
                    ),
                    "UOM": {
                        "value": "KG",
                        "error": "Error: UOM 'KG' is reserved.",
                    },
                },
            ),
            httpx.Response(200, json={}),
        ]
    )
    n, errors = seed.apply(_client(instance, recorder), baseline)
    assert n == 1
    assert len(errors) == 1
    assert "UnitsOfMeasure [KG]" in errors[0]
    assert "reserved" in errors[0]
    puts = [r for r in recorder.requests if r.method == "PUT"]
    assert len(puts) == 2
    out = capsys.readouterr()
    assert "PUT UnitsOfMeasure [HOUR]" in out.out
    assert "UnitsOfMeasure [KG]" in out.err


def test_apply_multi_error_summary_all_fail(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """V45/T185: every record failure collected; no silent partial."""
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    blow = httpx.Response(500, json={"exceptionMessage": "server blew up"})
    recorder = _SequencedPutRecorder([blow, blow])
    n, errors = seed.apply(_client(instance, recorder), baseline)
    assert n == 0
    assert len(errors) == 2
    assert "UnitsOfMeasure [KG]" in errors[0]
    assert "UnitsOfMeasure [HOUR]" in errors[1]
    assert all("server blew up" in e for e in errors)
    err = capsys.readouterr().err
    assert "UnitsOfMeasure [KG]" in err
    assert "UnitsOfMeasure [HOUR]" in err
