"""Extract engine: manifest validation, shaping, and the live round-trip.

Live records are served by an AcumaticaClient over httpx.MockTransport
(FakeServer honors $filter/$select and key URLs), so extract and the
diff-clean round-trip run through the real client offline.
"""

import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest
from click.testing import CliRunner

from acumatica_cli import cli, extract, seed
from acumatica_cli import client as client_mod
from acumatica_cli.client import AcumaticaClient, wrap
from acumatica_cli.config import Instance

# -- FakeServer: canned entity tables behind the real contract API shapes --

OPTIMIZATION_500_BODY = {
    "message": "An error has occurred.",
    "exceptionMessage": (
        "Optimization cannot be performed.The following fields cause "
        "the error:\r\nRealGainAcctID: View CuryRecords has BQL delegate"
    ),
}
NO_ENTITY_500_BODY = {
    "message": "An error has occurred.",
    "exceptionMessage": "No entity satisfies the condition.",
    "exceptionType": "PX.Api.ContractBased.NoEntitySatisfiesTheConditionException",
}


class FakeServer:
    """Canned per-entity tables; honors $filter, $select, and key URLs.

    Entities listed in ``delegate_view`` replay B9: any list GET without a
    $select projection answers the optimization 500; the key-URL GET and
    the $select-narrowed list GET succeed.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        keys: dict[str, list[str]],
        delegate_view: frozenset[str] = frozenset(),
    ):
        self.tables = tables
        self.keys = keys
        self.delegate_view = delegate_view
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith(("/auth/login", "/auth/logout")):
            return httpx.Response(204)
        if request.url.path.endswith("/Frames/Login.aspx"):
            # the landed-tenant probe (V5); the fixture instance is tenant T1
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        # /AcumaticaERP/entity/<Endpoint>/<version>/<Entity>[/<key>...]
        _, _, entity, *key_path = request.url.path.split("/entity/", 1)[1].split("/")
        records = self.tables.get(entity)
        if records is None:
            return httpx.Response(500, json=NO_ENTITY_500_BODY)
        if key_path:
            return self._by_key(entity, records, key_path)
        return self._list(entity, records, request.url.params)

    def _by_key(
        self, entity: str, records: list[dict[str, Any]], key_path: list[str]
    ) -> httpx.Response:
        wanted = [unquote(k) for k in key_path]
        for record in records:
            if [str(record[k]) for k in self.keys[entity]] == wanted:
                return httpx.Response(200, json=wrap(record))
        return httpx.Response(500, json=NO_ENTITY_500_BODY)

    def _list(
        self, entity: str, records: list[dict[str, Any]], params: httpx.QueryParams
    ) -> httpx.Response:
        select = params.get("$select")
        if entity in self.delegate_view and select is None:
            return httpx.Response(500, json=OPTIMIZATION_500_BODY)
        flt = params.get("$filter")
        if flt:
            for m in re.finditer(r"(\w+) eq '([^']*)'", flt):
                records = [r for r in records if str(r.get(m[1])) == m[2]]
        if select:
            fields = select.split(",")
            records = [{k: r[k] for k in fields} for r in records]
        return httpx.Response(200, json=[wrap(r) for r in records])


def _client(instance: Instance, server: FakeServer) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(server))


# -- canned live state for the packaged manifest's nine M1 entities --

TABLES: dict[str, list[dict[str, Any]]] = {
    "Company": [
        {
            "AcctCD": "COMPANY",
            "AcctName": "Example Company",
            "OrganizationType": "Without Branches",
            "BaseCuryID": "USD",
            "CountryID": "US",
        }
    ],
    "CreditTerms": [
        {
            "TermsID": "NET30",
            "Descr": "Net 30 Days",
            "VisibleTo": "All",
            "DueType": "Fixed Number of Days",
            "DayDue00": 30,
            "DiscType": "Fixed Number of Days",
            "DiscPercent": 0.0,
        }
    ],
    "Subaccount": [
        {
            "SubaccountCD": "000000",
            "Description": "Default",
            "Active": True,
            "Secured": False,
            # stripped by the manifest (B11 class)
            "LastModifiedDateTime": "2026-07-11T00:00:00+00:00",
        }
    ],
    "Account": [
        {
            "AccountCD": "32000",
            "Active": True,
            "Description": "Retained Earnings",
            "PostOption": "Summary",
            "Type": "Liability",
            # stripped by the manifest (B10/B11)
            "AccountGroup": "EXPENSE",
            "ChartOfAccountsOrder": 2,
            "CashAccount": "",
            "LastModifiedDateTime": "2026-07-11T00:00:00+00:00",
            # elided: None never reaches the emitted file
            "CurrencyID": None,
        },
        {
            "AccountCD": "10100",
            "Active": True,
            "Description": "Cash",
            "PostOption": "Detail",
            "Type": "Asset",
            "AccountGroup": None,
            "ChartOfAccountsOrder": 1,
            "LastModifiedDateTime": "2026-07-11T00:00:00+00:00",
        },
    ],
    # B9 entity: the plain list GET 500s (delegate_view below)
    "Currency": [
        {
            "CuryID": "EUR",
            "Description": "Euro",
            "CurySymbol": "",
            "DecimalPlaces": 2,
            "IsActive": True,
            "IsFinancial": True,
            "RealGainAcctID": "83000",
            # stripped by the manifest (T31 Translation* pairs)
            "TranslationGainAcctID": "83000",
            "TranslationLossAcctID": "84000",
        }
    ],
    "Ledger": [
        {
            "LedgerID": "ACTUAL",
            "Description": "Actual Ledger",
            "Type": "Actual",
            # stripped by the manifest (server-derived, T34)
            "CurrencyID": "USD",
        }
    ],
    "GLPreferences": [{"RetEarnAccountID": "32000", "YtdNetIncAccountID": "33000"}],
    "LedgerCompany": [{"LedgerCD": "ACTUAL", "OrganizationID": "COMPANY"}],
    "UnitsOfMeasure": [
        {"UnitID": "PIECE", "Description": "Piece", "L3Code": "PCB"},
        {"UnitID": "HOUR", "Description": "Hour", "L3Code": "HUR"},
    ],
}
KEYS = {spec.entity: spec.keys for spec in extract.load_manifest().entities}
DELEGATE_VIEW = frozenset({"Currency"})


@pytest.fixture
def server() -> FakeServer:
    return FakeServer(TABLES, KEYS, DELEGATE_VIEW)


# -- manifest self-check --


def test_packaged_manifest_is_self_consistent() -> None:
    """The M1 manifest: the verified GL set, endpoint-explicit, keyed."""
    manifest = extract.load_manifest()
    assert [s.entity for s in manifest.entities] == [
        "Company",
        "CreditTerms",
        "Subaccount",
        "Account",
        "Currency",
        "Ledger",
        "GLPreferences",
        "LedgerCompany",
        "UnitsOfMeasure",
    ]
    files = [s.file for s in manifest.entities]
    assert len(files) == len(set(files))
    for spec in manifest.entities:
        assert spec.keys, spec.entity
        if spec.entity in seed.BOOTSTRAP_ENTITIES:
            # V20 by construction: the emitted file must carry endpoint:
            assert spec.endpoint == seed.BOOTSTRAP_ENDPOINT, spec.entity


def test_manifest_resolves_symbolic_bootstrap_endpoint() -> None:
    spec = extract.EntitySpec(
        entity="Company", keys=["AcctCD"], file="f.yaml", endpoint="bootstrap"
    )
    assert spec.endpoint == seed.BOOTSTRAP_ENDPOINT


def test_manifest_rejects_bootstrap_entity_without_endpoint() -> None:
    with pytest.raises(ValueError, match="must carry an endpoint"):
        extract.Manifest(
            entities=[
                extract.EntitySpec(entity="Currency", keys=["CuryID"], file="c.yaml")
            ]
        )


def test_manifest_rejects_duplicate_files() -> None:
    spec = extract.EntitySpec(entity="Ledger", keys=["LedgerID"], file="dup.yaml")
    with pytest.raises(ValueError, match="duplicate destination files"):
        extract.Manifest(entities=[spec, spec])


def test_entity_spec_rejects_empty_keys() -> None:
    with pytest.raises(ValueError, match="keys"):
        extract.EntitySpec(entity="Ledger", keys=[], file="f.yaml")


def test_entity_spec_rejects_strip_with_include() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        extract.EntitySpec(
            entity="Ledger",
            keys=["LedgerID"],
            file="f.yaml",
            strip=["A"],
            include=["B"],
        )


def test_optimization_marker_has_one_spelling() -> None:
    # promoted to client; seed imports it back - one spelling repo-wide
    assert seed.OPTIMIZATION_500 is client_mod.OPTIMIZATION_500


# -- shaping --


def _spec(**overrides: Any) -> extract.EntitySpec:
    base: dict[str, Any] = {
        "entity": "UnitsOfMeasure",
        "keys": ["UnitID"],
        "file": "baseline/90-uoms.yaml",
    }
    return extract.EntitySpec(**(base | overrides))


def test_shape_sorts_records_and_orders_fields_key_first() -> None:
    live = [
        wrap({"Zeta": "z", "UnitID": "PIECE", "Alpha": "a"}),
        wrap({"Zeta": "z", "UnitID": "HOUR", "Alpha": "a"}),
    ]
    shaped = extract._shape(_spec(), live)  # pyright: ignore[reportPrivateUsage]
    assert [r["UnitID"] for r in shaped] == ["HOUR", "PIECE"]
    assert list(shaped[0]) == ["UnitID", "Alpha", "Zeta"]


def test_shape_strips_deny_list_and_elides_empty() -> None:
    live = [wrap({"UnitID": "HOUR", "Noise": "x", "Empty": "", "Null": None})]
    spec = _spec(strip=["Noise"])
    shaped = extract._shape(spec, live)  # pyright: ignore[reportPrivateUsage]
    assert shaped == [{"UnitID": "HOUR"}]


def test_shape_include_allow_list_keeps_keys() -> None:
    live = [wrap({"UnitID": "HOUR", "Description": "Hour", "Noise": "x"})]
    spec = _spec(include=["Description"])
    shaped = extract._shape(spec, live)  # pyright: ignore[reportPrivateUsage]
    assert shaped == [{"UnitID": "HOUR", "Description": "Hour"}]


def test_shape_missing_key_field_is_a_hard_error() -> None:
    with pytest.raises(RuntimeError, match="missing key field"):
        extract._shape(  # pyright: ignore[reportPrivateUsage]
            _spec(), [wrap({"Description": "no key"})]
        )


# -- run: file handling, filters, dry-run --


def _run(
    instance: Instance,
    server: FakeServer,
    out: Path,
    **kwargs: Any,
) -> None:
    extract.run(_client(instance, server), out, **kwargs)


def test_run_writes_files_and_reports(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(instance, server, tmp_path)
    out = capsys.readouterr().out
    for spec in extract.load_manifest().entities:
        assert (tmp_path / spec.file).is_file()
        n = len(TABLES[spec.entity])
        assert f"write {tmp_path / spec.file} ({n} records)" in out


def test_run_skip_exists_and_force_overwrites(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "bootstrap" / "company.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("operator-edited\n")

    _run(instance, server, tmp_path, only=frozenset({"Company"}))
    assert f"skip {target} (exists)" in capsys.readouterr().out
    assert target.read_text() == "operator-edited\n"

    _run(instance, server, tmp_path, only=frozenset({"Company"}), force=True)
    assert f"write {target}" in capsys.readouterr().out
    assert "entity: Company" in target.read_text()


def test_run_skips_entity_with_no_live_records(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server.tables = server.tables | {"UnitsOfMeasure": []}
    _run(instance, server, tmp_path, only=frozenset({"UnitsOfMeasure"}))
    target = tmp_path / "baseline" / "90-uoms.yaml"
    assert f"skip {target} (no records)" in capsys.readouterr().out
    assert not target.exists()


def test_run_dry_run_writes_nothing(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(instance, server, tmp_path, dry_run=True)
    out = capsys.readouterr().out
    assert f"would write {tmp_path / 'bootstrap' / 'company.yaml'} (1 records)" in out
    assert list(tmp_path.iterdir()) == []


def test_run_only_filters_entity_name_or_file_stem(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(instance, server, tmp_path, only=frozenset({"Ledger", "20-accounts"}))
    written = sorted(p.name for p in (tmp_path / "baseline").iterdir())
    assert written == ["20-accounts.yaml", "40-ledger.yaml"]
    assert "company.yaml" not in capsys.readouterr().out


# -- B9: the delegate-view fallback --


def test_b9_fallback_selects_keys_then_key_urls(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """The optimization 500 reroutes: $select key list, then per-key GETs."""
    _run(instance, server, tmp_path, only=frozenset({"Currency"}))
    currency_requests = [
        (r.url.path.split("/entity/", 1)[1], dict(r.url.params))
        for r in server.requests
    ]
    assert currency_requests == [
        ("Bootstrap/1.4.0/Currency", {}),  # plain list GET -> 500
        ("Bootstrap/1.4.0/Currency", {"$select": "CuryID"}),
        ("Bootstrap/1.4.0/Currency/EUR", {}),
    ]
    text = (tmp_path / "baseline" / "30-currencies.yaml").read_text()
    assert "RealGainAcctID" in text  # the key-URL GET returned full records
    assert "TranslationGainAcctID" not in text  # manifest strip still applies


def test_b9_non_optimization_500_still_raises(
    instance: Instance, tmp_path: Path
) -> None:
    server = FakeServer({}, {})  # unknown entity -> non-optimization 500
    with pytest.raises(RuntimeError, match="No entity satisfies"):
        _run(instance, server, tmp_path, only=frozenset({"Currency"}))


# -- the round-trip: emitted files parse and diff clean --


def test_round_trip_every_file_parses_and_diffs_clean(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """Extract -> load_baseline -> diff against the same live state = clean.

    Proves V20 by construction (bootstrap entities carry endpoint:) and
    that shaping (strip/elide) never manufactures drift.
    """
    _run(instance, server, tmp_path)
    diff_client = _client(instance, server)
    for spec in extract.load_manifest().entities:
        parsed = seed.load_baseline(tmp_path / spec.file)
        assert isinstance(parsed, seed.BaselineFile)
        assert seed.diff(diff_client, parsed) == [], spec.entity


def test_round_trip_is_byte_stable_under_permuted_server_order(
    instance: Instance, tmp_path: Path
) -> None:
    permuted = {
        entity: [dict(reversed(list(r.items()))) for r in reversed(records)]
        for entity, records in TABLES.items()
    }
    a, b = tmp_path / "a", tmp_path / "b"
    _run(instance, FakeServer(TABLES, KEYS, DELEGATE_VIEW), a)
    _run(instance, FakeServer(permuted, KEYS, DELEGATE_VIEW), b)
    for spec in extract.load_manifest().entities:
        assert (a / spec.file).read_bytes() == (b / spec.file).read_bytes(), spec.file


# -- CLI wiring --


def test_extract_cmd_wires_flags_through(
    monkeypatch: pytest.MonkeyPatch, instance: Instance, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: instance)
    server = FakeServer(TABLES, KEYS, DELEGATE_VIEW)

    def fake_client(inst: Instance, **kwargs: Any) -> AcumaticaClient:
        return _client(inst, server)

    monkeypatch.setattr(cli, "AcumaticaClient", fake_client)
    calls: list[dict[str, Any]] = []

    def fake_run(client: AcumaticaClient, out_dir: Path, **kwargs: Any) -> None:
        calls.append({"out_dir": out_dir} | kwargs)

    monkeypatch.setattr(cli.extract, "run", fake_run)
    result = CliRunner().invoke(
        cli.cli,
        ["extract", "--out", str(tmp_path), "--only", "Ledger", "--force", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert calls == [
        {
            "out_dir": tmp_path,
            "only": frozenset({"Ledger"}),
            "force": True,
            "dry_run": True,
        }
    ]


def test_extract_cmd_defaults_out_to_cwd(
    monkeypatch: pytest.MonkeyPatch, instance: Instance
) -> None:
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: instance)
    server = FakeServer(TABLES, KEYS, DELEGATE_VIEW)
    monkeypatch.setattr(
        cli, "AcumaticaClient", lambda inst, **kw: _client(inst, server)
    )
    calls: list[Path] = []
    monkeypatch.setattr(
        cli.extract, "run", lambda client, out_dir, **kw: calls.append(out_dir)
    )
    result = CliRunner().invoke(cli.cli, ["extract"])
    assert result.exit_code == 0, result.output
    assert calls == [Path(".")]
