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
    text = BASELINE + "endpoint: Bootstrap/1.7.0\n"
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "Bootstrap/1.7.0"


LEDGER_LINK_YAML = """\
entity: LedgerCompany
key: [LedgerCD, OrganizationID]
endpoint: Bootstrap/1.7.0
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
entity: Currency
key: CuryID
records:
  - CuryID: EUR
"""


def test_bootstrap_entities_parsed_from_packaged_template() -> None:
    # V2: the ambiguous set comes from bootstrap_project.xml, never a
    # hand-list - parity pinned here so a template edit surfaces offline
    assert seed.BOOTSTRAP_ENDPOINT == "Bootstrap/1.7.0"
    assert {
        "Company",
        "CreditTerms",
        "Currency",
        "GLPreferences",
        "LedgerCompany",
        "FinancialYearSettings",
        "MasterCalendar",
        "CompanyCalendar",
        "CompanyPeriod",
        "ManagePeriods",
        "INPreferences",
        "APPreferences",
        "ARPreferences",
        "SOPreferences",
        "POPreferences",
        "AvailabilityCalculationRule",
        "PostingClass",
        "CashAccount",
        "CAPreferences",
        "ReasonCode",
        "VendorClass",
        "StatementCycle",
        "Warehouse",
        "OrderType",
    } == seed.BOOTSTRAP_ENTITIES


def test_load_baseline_rejects_bootstrap_entity_without_endpoint(
    tmp_path: Path,
) -> None:
    """V20/B8: an entity both endpoints serve + no endpoint: = hard error.

    The error names both endpoints; a silent Default-endpoint PUT would hit
    a different screen than the author meant (Bootstrap Currency = CM202000,
    Default Currency = CM201000 list).
    """
    with pytest.raises(SystemExit, match=r"Default/25\.200\.001.*Bootstrap/1\.7\.0"):
        seed.load_baseline(_write(tmp_path, AMBIGUOUS_YAML))


def test_load_baseline_bootstrap_entity_explicit_endpoint_passes(
    tmp_path: Path,
) -> None:
    # V20: explicit endpoint: disambiguates - either target is legitimate
    for endpoint in ("Bootstrap/1.7.0", "Default/25.200.001"):
        text = AMBIGUOUS_YAML + f"endpoint: {endpoint}\n"
        assert seed.load_baseline(_write(tmp_path, text)).endpoint == endpoint


def test_apply_and_diff_target_endpoint_override(
    tmp_path: Path, instance: Instance
) -> None:
    text = BASELINE + "endpoint: Bootstrap/1.7.0\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder({"/UnitsOfMeasure": _live({"UOM": "KG"})})

    seed.apply(_client(instance, recorder), baseline)
    seed.diff(_client(instance, recorder), baseline)

    paths = {r.url.path for r in recorder.requests}
    assert paths == {"/AcumaticaERP/entity/Bootstrap/1.7.0/UnitsOfMeasure"}


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

    n = seed.apply(_client(instance, recorder), baseline)

    assert n == 2
    assert [r.method for r in recorder.requests] == ["PUT", "PUT"]
    assert "PUT UnitsOfMeasure [KG]" in capsys.readouterr().out


def test_apply_dry_run_makes_no_calls(
    tmp_path: Path, instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    recorder = Recorder()

    n = seed.apply(_client(instance, recorder), baseline, dry_run=True)

    assert n == 2
    assert recorder.requests == []
    assert "would PUT UnitsOfMeasure [KG]" in capsys.readouterr().out


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
endpoint: Bootstrap/1.7.0
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
endpoint: Bootstrap/1.7.0
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
        "Bootstrap/1.7.0/Currency",
        "Bootstrap/1.7.0/Currency/EUR",
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
endpoint: Bootstrap/1.7.0
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


def _action(tmp_path: Path, text: str = ACTION_YAML) -> "seed.ActionFile":
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
        ("GET", "Bootstrap/1.7.0/MasterCalendar"),
        ("POST", "Bootstrap/1.7.0/MasterCalendar/GenerateCalendar"),
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
        "/AcumaticaERP/entity/Bootstrap/1.7.0/MasterCalendar"
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

    n = seed.apply(_client(instance, recorder), action, dry_run=True)

    assert n == 1
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
    assert request.url.path.endswith("/Bootstrap/1.7.0/MasterCalendar")
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
