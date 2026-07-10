"""Baseline parsing, normalization, and the apply/diff logic.

Live records are served by an AcumaticaClient over httpx.MockTransport, so
apply/diff run through the real client (wrap, $filter, _checked) offline.
"""

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


def test_load_baseline_parses_string_key(tmp_path: Path) -> None:
    baseline = seed.load_baseline(_write(tmp_path, BASELINE))
    assert baseline.entity == "UnitsOfMeasure"
    assert baseline.keys == ["UOM"]
    assert [r["UOM"] for r in baseline.records] == ["KG", "HOUR"]


def test_load_baseline_accepts_key_list(tmp_path: Path) -> None:
    text = BASELINE.replace("key: UOM", "key: [UOM, Description]")
    assert seed.load_baseline(_write(tmp_path, text)).keys == ["UOM", "Description"]


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
    text = BASELINE + "endpoint: Bootstrap/1.1.0\n"
    assert seed.load_baseline(_write(tmp_path, text)).endpoint == "Bootstrap/1.1.0"


AMBIGUOUS_YAML = """\
entity: Currency
key: CuryID
records:
  - CuryID: EUR
"""


def test_bootstrap_entities_parsed_from_packaged_template() -> None:
    # V2: the ambiguous set comes from bootstrap_project.xml, never a
    # hand-list - parity pinned here so a template edit surfaces offline
    assert seed.BOOTSTRAP_ENDPOINT == "Bootstrap/1.1.0"
    assert {"Company", "CreditTerms", "Currency"} == seed.BOOTSTRAP_ENTITIES


def test_load_baseline_rejects_bootstrap_entity_without_endpoint(
    tmp_path: Path,
) -> None:
    """V20/B8: an entity both endpoints serve + no endpoint: = hard error.

    The error names both endpoints; a silent Default-endpoint PUT would hit
    a different screen than the author meant (Bootstrap Currency = CM202000,
    Default Currency = CM201000 list).
    """
    with pytest.raises(SystemExit, match=r"Default/25\.200\.001.*Bootstrap/1\.1\.0"):
        seed.load_baseline(_write(tmp_path, AMBIGUOUS_YAML))


def test_load_baseline_bootstrap_entity_explicit_endpoint_passes(
    tmp_path: Path,
) -> None:
    # V20: explicit endpoint: disambiguates - either target is legitimate
    for endpoint in ("Bootstrap/1.1.0", "Default/25.200.001"):
        text = AMBIGUOUS_YAML + f"endpoint: {endpoint}\n"
        assert seed.load_baseline(_write(tmp_path, text)).endpoint == endpoint


def test_apply_and_diff_target_endpoint_override(
    tmp_path: Path, instance: Instance
) -> None:
    text = BASELINE + "endpoint: Bootstrap/1.1.0\n"
    baseline = seed.load_baseline(_write(tmp_path, text))
    recorder = Recorder({"/UnitsOfMeasure": _live({"UOM": "KG"})})

    seed.apply(_client(instance, recorder), baseline)
    seed.diff(_client(instance, recorder), baseline)

    paths = {r.url.path for r in recorder.requests}
    assert paths == {"/AcumaticaERP/entity/Bootstrap/1.1.0/UnitsOfMeasure"}


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
endpoint: Bootstrap/1.1.0
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
        "Bootstrap/1.1.0/Currency",
        "Bootstrap/1.1.0/Currency/EUR",
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
