"""Extract engine: catalog validation, shaping, and the live round-trip.

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
import yaml
from click.testing import CliRunner

from acumatica_cli import bootstrap, cli, extract, seed
from acumatica_cli import client as client_mod
from acumatica_cli.client import AcumaticaClient, unwrap, wrap
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
SETUP_NOT_ENTERED_500_BODY = {
    "message": "An error has occurred.",
    "exceptionMessage": (
        "The required configuration data is not entered on the Company Branches form."
    ),
    "exceptionType": "PX.Data.PXSetupNotEnteredException",
}


class FakeServer:
    """Canned per-entity tables; honors $filter, $select, and key URLs.

    Entities listed in ``delegate_view`` replay B9: any list GET without a
    $select projection answers the optimization 500; the key-URL GET and
    the $select-narrowed list GET succeed. Entities listed in
    ``setup_not_entered`` replay the virgin-tenant empty-state class (B19):
    every GET answers the PXSetupNotEnteredException 500.
    """

    def __init__(
        self,
        tables: dict[str, list[dict[str, Any]]],
        keys: dict[str, list[str]],
        delegate_view: frozenset[str] = frozenset(),
        setup_not_entered: frozenset[str] = frozenset(),
    ):
        self.tables = tables
        self.keys = keys
        self.delegate_view = delegate_view
        self.setup_not_entered = setup_not_entered
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
        if entity in self.setup_not_entered:
            return httpx.Response(500, json=SETUP_NOT_ENTERED_500_BODY)
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
            # quoted values compare verbatim; bare literals (true/false)
            # compare case-folded (JSON booleans str() to True/False)
            for m in re.finditer(r"(\w+) eq (?:'([^']*)'|(\w+))", flt):
                field, quoted, literal = m.groups()
                if quoted is not None:
                    records = [r for r in records if str(r.get(field)) == quoted]
                else:
                    records = [
                        r for r in records if str(r.get(field)).lower() == literal
                    ]
        if select:
            fields = select.split(",")
            records = [{k: r[k] for k in fields} for r in records]
        return httpx.Response(200, json=[wrap(r) for r in records])


def _client(instance: Instance, server: FakeServer) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(server))


# -- canned live state for the packaged GL-chain catalog entities --
# Master-surface rows (T116 path completeness) default empty: offline
# extract skips (no records) until T117 filter-split fixtures land.

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
            # stripped by the catalog (B11 class)
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
            # stripped by the catalog (B10/B11)
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
    # B9 / filter fixtures for Currency unit tests (not a packaged catalog
    # row under LAB5 — Multicurrency omitted from templates; V34 equality).
    "Currency": [
        {
            "CuryID": "EUR",
            "Description": "Euro",
            "CurySymbol": "",
            "DecimalPlaces": 2,
            "IsActive": True,
            "IsFinancial": True,
            "RealGainAcctID": "83000",
            "TranslationGainAcctID": "83000",
            "TranslationLossAcctID": "84000",
        },
        {
            "CuryID": "JPY",
            "Description": "Yen",
            "DecimalPlaces": 0,
            "IsActive": False,
            "IsFinancial": False,
        },
    ],
    "Ledger": [
        {
            "LedgerID": "ACTUAL",
            "Description": "Actual Ledger",
            "Type": "Actual",
            # stripped by the catalog (server-derived, T34)
            "CurrencyID": "USD",
        }
    ],
    "GLPreferences": [{"RetEarnAccountID": "32000", "YtdNetIncAccountID": "33000"}],
    # the gh-issue-#7 repro shape (B21): one ledger, three org links -
    # the pair key keeps every link distinct through the round-trip
    "LedgerCompany": [
        {"LedgerCD": "ACTUAL", "OrganizationID": "CAPITAL"},
        {"LedgerCD": "ACTUAL", "OrganizationID": "PRODUCTS"},
        {"LedgerCD": "ACTUAL", "OrganizationID": "SERVICES"},
    ],
    "UnitsOfMeasure": [
        {"UnitID": "PIECE", "Description": "Piece", "L3Code": "PCB"},
        {"UnitID": "HOUR", "Description": "Hour", "L3Code": "HUR"},
    ],
    # -- setup/ synthesis sources (T49): the state the GL action chain left --
    "FinancialYearSettings": [
        {
            # DateTimeValue: the synthesizer keeps the date part only
            "BegFinYear": "2026-01-01T00:00:00+00:00",
            "FinPeriods": 12,
            "PeriodType": "Month",
        }
    ],
    "MasterCalendar": [{"FinancialYear": "2026"}, {"FinancialYear": "2027"}],
    "CompanyCalendar": [
        {"FinancialYear": "2026", "OrganizationID": "COMPANY"},
        {"FinancialYear": "2027", "OrganizationID": "COMPANY"},
    ],
    "CompanyPeriod": [
        {"FinancialYear": "2026", "FinPeriodID": "012026", "Status": "Open"},
        {"FinancialYear": "2027", "FinPeriodID": "012027", "Status": "Open"},
        {"FinancialYear": "2027", "FinPeriodID": "022027", "Status": "Inactive"},
    ],
}
# Last-wins keys per entity name (multi-file Warehouse/StockItem share names).
KEYS = {spec.entity: spec.keys for spec in extract.load_manifest().entities}
KEYS["Currency"] = ["CuryID"]  # synthetic B9 fixture entity
# Empty tables for every catalog entity without canned live state → skip clean
for row in extract.load_manifest().entities:
    TABLES.setdefault(row.entity, [])
DELEGATE_VIEW = frozenset({"Currency"})

# Catalog row counts (V34 path set): entity + setup (+ features outside catalog).
_CATALOG_ENTITY_ROWS = len(extract.load_manifest().entities)
_CATALOG_SETUP_ROWS = len(extract.load_manifest().setup)
_CATALOG_SEED_ROWS = _CATALOG_ENTITY_ROWS + _CATALOG_SETUP_ROWS  # 37
# GL-chain rows that write under default TABLES (Company appears twice).
_GL_WRITE_ENTITIES = frozenset(
    {
        "Company",
        "CreditTerms",
        "Subaccount",
        "Account",
        "Ledger",
        "GLPreferences",
        "LedgerCompany",
        "UnitsOfMeasure",
    }
)
_GL_ENTITY_WRITES = sum(
    1 for s in extract.load_manifest().entities if s.entity in _GL_WRITE_ENTITIES
)  # Company x2 + 7 = 9
_MASTER_SKIPS = _CATALOG_ENTITY_ROWS - _GL_ENTITY_WRITES  # empty master rows
_FULL_WRITES = _GL_ENTITY_WRITES + _CATALOG_SETUP_ROWS + 1  # + features
_FULL_ROWS = _CATALOG_SEED_ROWS + 1  # + features pass


@pytest.fixture
def server() -> FakeServer:
    return FakeServer(TABLES, KEYS, DELEGATE_VIEW)


# -- manifest self-check --


def test_packaged_manifest_is_self_consistent() -> None:
    """Packaged catalog: GL chain + master path rows, endpoint-explicit, keyed."""
    manifest = extract.load_manifest()
    files = [s.file for s in manifest.entities]
    assert files[:9] == [
        "config/bootstrap/company.yaml",
        "config/bootstrap/credit-terms.yaml",
        "config/baseline/10-subaccounts.yaml",
        "config/baseline/20-accounts.yaml",
        "config/baseline/40-ledger.yaml",
        "config/baseline/50-gl-preferences.yaml",
        "config/baseline/60-ledger-company.yaml",
        "config/baseline/90-uoms.yaml",
        "config/baseline/91-company-packaging.yaml",
    ]
    assert "config/baseline/30-currencies.yaml" not in files  # V34: not in templates
    assert "config/master/10-reason-codes.yaml" in files
    assert "config/master/85-kit-specifications.yaml" in files
    assert len(manifest.entities) == _CATALOG_ENTITY_ROWS
    assert [(s.kind, s.file) for s in manifest.setup] == [
        ("financial-year", "config/setup/10-financial-year.yaml"),
        ("master-calendar", "config/setup/20-master-calendar.yaml"),
        ("open-periods", "config/setup/30-open-periods.yaml"),
    ]
    assert extract.FEATURES_FILE == "config/bootstrap/features.yaml"
    seed_files = files + [s.file for s in manifest.setup]
    assert len(seed_files) == len(set(seed_files))
    assert extract.FEATURES_FILE not in seed_files
    # V30/T115 hard-cut: every emit path under config/, never root SEED_DIRS
    for path in [*seed_files, extract.FEATURES_FILE]:
        assert path.startswith("config/"), path
        assert not path.startswith(("bootstrap/", "baseline/", "setup/", "master/")), (
            path
        )
    for spec in manifest.entities:
        assert spec.keys, spec.entity
        if spec.entity in seed.BOOTSTRAP_ENTITIES:
            # V20: bootstrap-served entities must carry an endpoint (literal
            # or symbolic). Dual-serve Default surfaces may use `default`
            # (Warehouse locations/defaults) rather than bootstrap.
            assert spec.endpoint is not None, spec.entity
    # V25/B21: the pair identifies each org-ledger link; LedgerCD (the
    # primary-view field) must stay first - diff's read-back filters on
    # the first key alone (B14)
    keys = {s.entity: s.keys for s in manifest.entities}
    assert keys["LedgerCompany"] == ["LedgerCD", "OrganizationID"]


def test_catalog_completeness_v34() -> None:
    """V34 unit gate: packaged template seed set equals catalog file set."""
    missing, extra = extract.catalog_completeness_gap()
    assert not missing, f"missing_from_catalog={sorted(missing)}"
    assert not extra, f"extra_in_catalog={sorted(extra)}"

    # features synthesis + views are excluded from both sides
    templates = extract.packaged_template_seed_files()
    assert extract.FEATURES_FILE not in templates
    assert not any(p.startswith("config/views/") for p in templates)
    assert "config/bootstrap/project.xml" not in templates
    catalog = extract.catalog_seed_files()
    assert templates == catalog
    assert len(templates) == _CATALOG_SEED_ROWS


def test_manifest_keeps_symbolic_bootstrap_endpoint() -> None:
    spec = extract.EntitySpec(
        entity="Company", keys=["AcctCD"], file="f.yaml", endpoint="bootstrap"
    )
    assert spec.endpoint == "bootstrap"
    assert seed.resolve_endpoint(spec.endpoint) == seed.BOOTSTRAP_ENDPOINT


def test_manifest_rejects_bootstrap_entity_without_endpoint() -> None:
    with pytest.raises(ValueError, match="must carry an endpoint"):
        extract.Manifest(
            entities=[
                extract.EntitySpec(entity="Company", keys=["AcctCD"], file="c.yaml")
            ]
        )


def test_manifest_rejects_duplicate_files() -> None:
    spec = extract.EntitySpec(entity="Ledger", keys=["LedgerID"], file="dup.yaml")
    with pytest.raises(ValueError, match="duplicate destination files"):
        extract.Manifest(entities=[spec, spec])


def test_manifest_rejects_row_claiming_features_file() -> None:
    # config/bootstrap/features.yaml belongs to the feature-closure render
    spec = extract.EntitySpec(
        entity="Ledger", keys=["LedgerID"], file=extract.FEATURES_FILE
    )
    with pytest.raises(ValueError, match="duplicate destination files"):
        extract.Manifest(entities=[spec])


def test_setup_synth_rejects_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown setup synthesizer kind"):
        extract.SetupSynth(kind="bogus", file="config/setup/x.yaml")


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
        "file": "config/baseline/90-uoms.yaml",
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


def test_shape_duplicate_key_tuple_is_a_hard_error() -> None:
    # V25/B21: an under-declared key surfaces at shape time - the file
    # would diff as permanent false drift, so it must never be emitted
    live = [
        wrap({"UnitID": "HOUR", "Description": "Hour"}),
        wrap({"UnitID": "HOUR", "Description": "Stunde"}),
    ]
    with pytest.raises(RuntimeError, match=r"duplicate key tuple \[HOUR\].*\(UnitID\)"):
        extract._shape(_spec(), live)  # pyright: ignore[reportPrivateUsage]


# -- run: file handling, filters, dry-run --


def _run(
    instance: Instance,
    server: FakeServer,
    out: Path,
    **kwargs: Any,
) -> int:
    return extract.run(_client(instance, server), out, **kwargs)


def test_run_writes_files_and_reports(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(instance, server, tmp_path)
    out = capsys.readouterr().out
    # hard-cut: emit only under config/, never root SEED_DIRS (T115/V30)
    assert (tmp_path / "config").is_dir()
    assert not (tmp_path / "bootstrap").exists()
    assert not (tmp_path / "baseline").exists()
    assert not (tmp_path / "setup").exists()
    for spec in extract.load_manifest().entities:
        target = tmp_path / spec.file
        live = TABLES.get(spec.entity) or []
        if not live:
            assert f"skip {target} (no records)" in out
            assert not target.exists()
            continue
        assert target.is_file()
        assert f"write {target} ({len(live)} records)" in out


def test_run_skip_exists_and_force_overwrites(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    target = tmp_path / "config" / "bootstrap" / "company.yaml"
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
    target = tmp_path / "config" / "baseline" / "90-uoms.yaml"
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
    company = tmp_path / "config" / "bootstrap" / "company.yaml"
    fin_year = tmp_path / "config" / "setup" / "10-financial-year.yaml"
    features = tmp_path / "config" / "bootstrap" / "features.yaml"
    assert f"would write {company} (1 records)" in out
    assert f"would write {fin_year} (1 records)" in out
    # built-in six + SubAccount (Warehouse/Kit gates only when those rows produce)
    assert f"would write {features} (7 records)" in out
    assert list(tmp_path.iterdir()) == []


def test_rerun_skips_every_emitted_file(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Second run: every written destination skips; --force rewrites them."""
    _run(instance, server, tmp_path)
    capsys.readouterr()
    _run(instance, server, tmp_path)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
    # written destinations re-skip as (exists); empty master re-skips (no records)
    exists_skips = [ln for ln in lines if ln.endswith("(exists)")]
    no_rec_skips = [ln for ln in lines if ln.endswith("(no records)")]
    assert len(exists_skips) == _FULL_WRITES
    assert len(no_rec_skips) == _MASTER_SKIPS
    assert len(lines) == _FULL_ROWS
    _run(instance, server, tmp_path, force=True)
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln]
    assert len(lines) == _FULL_ROWS
    assert sum(1 for ln in lines if ln.startswith("write ")) == _FULL_WRITES
    assert any("91-company-packaging" in ln and ln.startswith("write ") for ln in lines)


def test_run_only_filters_entity_name_or_file_stem(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run(instance, server, tmp_path, only=frozenset({"Ledger", "20-accounts"}))
    written = sorted(p.name for p in (tmp_path / "config" / "baseline").iterdir())
    assert written == ["20-accounts.yaml", "40-ledger.yaml"]
    assert "company.yaml" not in capsys.readouterr().out


# -- B9: the delegate-view fallback --


def _currency_spec() -> extract.EntitySpec:
    """Synthetic Currency row (not packaged under LAB5; B9/filter unit fixture)."""
    return extract.EntitySpec(
        entity="Currency",
        keys=["CuryID"],
        file="config/baseline/30-currencies.yaml",
        endpoint="bootstrap",
        filter="IsFinancial eq true",
        strip=[
            "TranslationGainAcctID",
            "TranslationGainSubID",
            "TranslationLossAcctID",
            "TranslationLossSubID",
        ],
        features=["Multicurrency"],
    )


def test_b9_fallback_selects_keys_then_key_urls(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
) -> None:
    """The optimization 500 reroutes: $select key list, then per-key GETs.

    The filter rides both list reads (T52): the fallback's key list is
    already narrowed, so the non-financial JPY row never gets a key-URL
    walk and never reaches the shaped records.
    """
    spec = _currency_spec()
    client = _client(instance, server)
    live = extract._fetch(client, spec)  # pyright: ignore[reportPrivateUsage]
    records = extract._shape(spec, live)  # pyright: ignore[reportPrivateUsage]
    currency_requests = [
        (r.url.path.split("/entity/", 1)[1], dict(r.url.params))
        for r in server.requests
    ]
    assert currency_requests == [
        ("Bootstrap/1.0.0/Currency", {"$filter": "IsFinancial eq true"}),
        (
            "Bootstrap/1.0.0/Currency",
            {"$select": "CuryID", "$filter": "IsFinancial eq true"},
        ),
        ("Bootstrap/1.0.0/Currency/EUR", {}),
    ]
    text = extract._render(spec, records)  # pyright: ignore[reportPrivateUsage]
    assert "RealGainAcctID" in text
    assert "TranslationGainAcctID" not in text
    assert "JPY" not in text
    assert "endpoint: bootstrap" in text
    out = tmp_path / "config" / "baseline" / "30-currencies.yaml"
    out.parent.mkdir(parents=True)
    out.write_text(text)
    assert out.is_file()


def test_fetch_filter_narrows_plain_list_get(
    instance: Instance, server: FakeServer
) -> None:
    """A filter rides the plain list GET too - the non-B9 read path (T52)."""
    spec = _spec(filter="UnitID eq 'HOUR'")
    records = extract._fetch(  # pyright: ignore[reportPrivateUsage]
        _client(instance, server), spec
    )
    assert [unwrap(r)["UnitID"] for r in records] == ["HOUR"]
    assert dict(server.requests[-1].url.params) == {"$filter": "UnitID eq 'HOUR'"}


def test_entity_spec_filter_defaults_to_none() -> None:
    # no filter -> the plain list GET goes out with no params at all
    assert _spec().filter is None


def test_b9_non_optimization_500_never_takes_fallback(
    instance: Instance,
) -> None:
    """Only the optimization 500 reroutes; any other 500 raises to the caller.

    Pre-V24 this aborted the run; now the row reports and the run goes on,
    but the B9 fallback ($select key list) still never fires for it.
    """
    server = FakeServer({}, {})  # unknown entity -> non-optimization 500
    with pytest.raises(RuntimeError, match="No entity satisfies"):
        extract._fetch(  # pyright: ignore[reportPrivateUsage]
            _client(instance, server), _currency_spec()
        )
    assert not any("select" in str(r.url) for r in server.requests)


# -- setup/ synthesis: the GL action chain derived back from live state --


def test_synthesized_financial_year(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    _run(instance, server, tmp_path, only=frozenset({"financial-year"}))
    doc = yaml.safe_load(
        (tmp_path / "config" / "setup" / "10-financial-year.yaml").read_text()
    )
    assert doc == {
        "action": "GeneratePeriods",
        "entity": "FinancialYearSettings",
        "endpoint": "bootstrap",
        # BegFinYear: date part only, off the live ISO datetime
        "record": {"BegFinYear": "2026-01-01", "FinPeriods": 12, "PeriodType": "Month"},
        "done_when": {},
    }


def test_synthesized_master_calendar_spans_year_range(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    _run(instance, server, tmp_path, only=frozenset({"master-calendar"}))
    doc = yaml.safe_load(
        (tmp_path / "config" / "setup" / "20-master-calendar.yaml").read_text()
    )
    assert doc == {
        "action": "GenerateCalendar",
        "entity": "MasterCalendar",
        "endpoint": "bootstrap",
        "record": {"FinancialYear": "2026"},
        "parameters": {"FromYear": "2026", "ToYear": "2027"},
        # the last year is the stronger done evidence: generation
        # completed through the range
        "done_when": {"entity": "CompanyCalendar", "filter": "FinancialYear eq '2027'"},
    }


def test_synthesized_open_periods_sources_company_org(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """OrganizationID = the extracted Company AcctCD (V22 in-set closure)."""
    _run(instance, server, tmp_path, only=frozenset({"open-periods"}))
    doc = yaml.safe_load(
        (tmp_path / "config" / "setup" / "30-open-periods.yaml").read_text()
    )
    assert doc == {
        "action": "ProcessAll",
        "entity": "ManagePeriods",
        "endpoint": "bootstrap",
        "record": {
            "Action": "Open",
            "FromYear": "2026",
            "ToYear": "2027",
            "OrganizationID": "COMPANY",
        },
        "done_when": {
            "entity": "CompanyPeriod",
            "filter": "FinancialYear eq '2027' and Status eq 'Open'",
        },
    }


def test_open_periods_none_open_skips_with_warn(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    server.tables = server.tables | {
        "CompanyPeriod": [
            {"FinancialYear": "2026", "FinPeriodID": "012026", "Status": "Inactive"}
        ]
    }
    _run(instance, server, tmp_path, only=frozenset({"open-periods"}))
    captured = capsys.readouterr()
    target = tmp_path / "config" / "setup" / "30-open-periods.yaml"
    assert f"skip {target} (no open periods)" in captured.out
    assert "no open periods on tenant" in captured.err
    assert not target.exists()


# -- features closure (V22/B15) --


def test_features_closure_unions_gates_of_record_producing_entities(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """features.yaml = the built-in six + gates, and load_features parses it.

    SubAccount rides Subaccount (produces under default FakeServer). Master
    feature gates (Warehouse, KitAssemblies, …) stay off while those rows
    skip empty offline.
    """
    _run(instance, server, tmp_path)
    assert bootstrap.load_features(tmp_path) == [
        *bootstrap.DEFAULT_FEATURES,
        "SubAccount",
    ]


def test_features_closure_drops_gate_when_no_records(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    server.tables = server.tables | {"Subaccount": []}
    _run(instance, server, tmp_path)
    names = bootstrap.load_features(tmp_path)
    assert "SubAccount" not in names
    assert names == list(bootstrap.DEFAULT_FEATURES)


def test_features_closure_counts_preexisting_files(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """A prior run's file is in the output set even when this run skips it."""
    server.tables = server.tables | {"Subaccount": []}
    target = tmp_path / "config" / "baseline" / "10-subaccounts.yaml"
    target.parent.mkdir(parents=True)
    target.write_text("operator-edited\n")
    _run(instance, server, tmp_path)
    assert "SubAccount" in bootstrap.load_features(tmp_path)


# -- the round-trip: emitted files parse and diff clean --


def test_round_trip_every_file_parses_and_diffs_clean(
    instance: Instance, server: FakeServer, tmp_path: Path
) -> None:
    """Extract -> load_baseline -> diff against the same live state = clean.

    Proves V20 by construction (bootstrap entities carry endpoint:) and
    that shaping (strip/elide) never manufactures drift; action files
    diff through their synthesized done_when probes (V4).
    """
    _run(instance, server, tmp_path)
    diff_client = _client(instance, server)
    manifest = extract.load_manifest()
    for spec in manifest.entities:
        path = tmp_path / spec.file
        if not (TABLES.get(spec.entity) or []):
            assert not path.exists(), spec.file  # empty → skip (no records)
            continue
        assert path.exists(), spec.entity
        parsed = seed.load_baseline(path)
        assert isinstance(parsed, seed.BaselineFile)
        assert seed.diff(diff_client, parsed) == [], spec.entity
    for synth in manifest.setup:
        action = seed.load_baseline(tmp_path / synth.file)
        assert isinstance(action, seed.ActionFile)
        assert seed.diff(diff_client, action) == [], synth.kind


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
    files = sorted(p.relative_to(a) for p in a.rglob("*.yaml"))
    assert files == sorted(p.relative_to(b) for p in b.rglob("*.yaml"))
    # GL-chain entity writes + setup + features (master empty → skip)
    assert len(files) == _FULL_WRITES
    for rel in files:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), str(rel)


# -- V24: per-row failure isolation (B19, gh issue #5) --


def test_row_failure_reported_and_run_continues(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One failing entity row: x line on stderr, every later row still lands."""
    server.tables = {k: v for k, v in server.tables.items() if k != "Subaccount"}
    failed = _run(instance, server, tmp_path)
    assert failed == 1
    captured = capsys.readouterr()
    assert "x Subaccount: " in captured.err
    assert "No entity satisfies" in captured.err
    assert "Subaccount" not in captured.out  # failures are process, not data (V9)
    # rows past the failure all ran: entities, setup synths, features
    assert (tmp_path / "config" / "baseline" / "20-accounts.yaml").is_file()
    assert (tmp_path / "config" / "setup" / "30-open-periods.yaml").is_file()
    assert (tmp_path / "config" / "bootstrap" / "features.yaml").is_file()
    # GL writes minus Subaccount + setup + features; empty master skips
    written = _FULL_WRITES - 1
    assert f"x {written} written, {_MASTER_SKIPS} skipped, 1 failed" in captured.err


def test_setup_not_entered_500_skips_clean(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The virgin-tenant empty-state 500 classifies as a skip, not a failure."""
    server.setup_not_entered = frozenset({"Ledger"})
    failed = _run(instance, server, tmp_path, only=frozenset({"Ledger"}))
    assert failed == 0
    captured = capsys.readouterr()
    target = tmp_path / "config" / "baseline" / "40-ledger.yaml"
    assert f"skip {target} (screen setup not entered)" in captured.out
    assert not target.exists()
    assert "+ 0 written, 1 skipped" in captured.err


def test_duplicate_key_tuple_is_row_failure_and_run_continues(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """V25 through V24: dup key tuple -> x line, no file, later rows land."""
    server.tables = server.tables | {
        "Subaccount": [
            {"SubaccountCD": "000000", "Description": "Default"},
            {"SubaccountCD": "000000", "Description": "Duplicate"},
        ]
    }
    failed = _run(instance, server, tmp_path)
    assert failed == 1
    captured = capsys.readouterr()
    assert "x Subaccount: records duplicate key tuple [000000]" in captured.err
    assert not (tmp_path / "config" / "baseline" / "10-subaccounts.yaml").exists()
    # rows past the failure all ran; the failed file never gates them
    assert (tmp_path / "config" / "baseline" / "20-accounts.yaml").is_file()
    assert (tmp_path / "config" / "bootstrap" / "features.yaml").is_file()
    written = _FULL_WRITES - 1
    assert f"x {written} written, {_MASTER_SKIPS} skipped, 1 failed" in captured.err


def test_setup_synth_failure_isolated(
    instance: Instance,
    server: FakeServer,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A synth row's non-empty-state failure reports; later synths still run."""
    server.tables = {k: v for k, v in server.tables.items() if k != "MasterCalendar"}
    failed = _run(
        instance,
        server,
        tmp_path,
        only=frozenset({"master-calendar", "open-periods"}),
    )
    assert failed == 1
    captured = capsys.readouterr()
    assert "x master-calendar: " in captured.err
    assert (tmp_path / "config" / "setup" / "30-open-periods.yaml").is_file()


# the B19 live repro (issue #5): a clean tenant's reads split by server
# accident between empty 200 [] and PXSetupNotEnteredException 500
VIRGIN_SETUP_NOT_ENTERED = frozenset(
    {
        "Subaccount",
        "Account",
        "Ledger",
        "GLPreferences",
        "LedgerCompany",
        "MasterCalendar",
        "CompanyPeriod",
    }
)
VIRGIN_TABLES: dict[str, list[dict[str, Any]]] = {
    "Company": [],
    "CreditTerms": [],
    "UnitsOfMeasure": TABLES["UnitsOfMeasure"],
    "FinancialYearSettings": [],
}
# remaining catalog entities answer empty 200 [] (skip no records)
for row in extract.load_manifest().entities:
    VIRGIN_TABLES.setdefault(row.entity, [])


def test_virgin_tenant_dry_run_walks_full_manifest_exit_0(
    monkeypatch: pytest.MonkeyPatch, instance: Instance, tmp_path: Path
) -> None:
    """The T57 verify leg: a virgin tenant extracts whole, dry-run exits 0."""
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: instance)
    server = FakeServer(VIRGIN_TABLES, KEYS, setup_not_entered=VIRGIN_SETUP_NOT_ENTERED)
    monkeypatch.setattr(
        cli, "AcumaticaClient", lambda inst, **kw: _client(inst, server)
    )
    result = CliRunner().invoke(
        cli.cli, ["extract", "--out", str(tmp_path), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    # entity empty + setup-not-entered screens + financial-year empty
    # + UoM write + features write; master path rows skip as (no records)
    empty_entity_n = sum(
        1
        for s in extract.load_manifest().entities
        if s.entity not in VIRGIN_SETUP_NOT_ENTERED
        and not (VIRGIN_TABLES.get(s.entity) or [])
    )
    entity_setup_not_entered_n = sum(
        1
        for s in extract.load_manifest().entities
        if s.entity in VIRGIN_SETUP_NOT_ENTERED
    )
    # master-calendar + open-periods synths also hit setup_not_entered sources
    screen_skip_n = entity_setup_not_entered_n + 2
    assert result.output.count("(no records)") == empty_entity_n
    assert result.output.count("(screen setup not entered)") == screen_skip_n
    assert result.output.count("(entity not in active Bootstrap contract)") == 0
    assert result.output.count("(no financial year setup)") == 1
    assert (
        f"would write {tmp_path / 'config' / 'baseline' / '90-uoms.yaml'} (2 records)"
        in result.output
    )
    # features closure = the built-in six: only the gate-free UoM row produced
    assert (
        f"would write {tmp_path / 'config' / 'bootstrap' / 'features.yaml'} (6 records)"
        in result.output
    )
    skipped = empty_entity_n + screen_skip_n + 1  # + financial-year empty
    assert f"+ 2 written, {skipped} skipped (dry run)" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_extract_cmd_exits_1_when_any_row_failed(
    monkeypatch: pytest.MonkeyPatch, instance: Instance, tmp_path: Path
) -> None:
    """Exit 1 any row failed, never 2 - drift stays diff's (V9/V24)."""
    monkeypatch.setattr(cli, "load_instance", lambda overrides=None: instance)
    tables = {k: v for k, v in TABLES.items() if k != "Subaccount"}
    server = FakeServer(tables, KEYS, DELEGATE_VIEW)
    monkeypatch.setattr(
        cli, "AcumaticaClient", lambda inst, **kw: _client(inst, server)
    )
    result = CliRunner().invoke(cli.cli, ["extract", "--out", str(tmp_path)])
    assert result.exit_code == 1
    assert "x Subaccount: " in result.stderr
    assert "1 failed" in result.stderr


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
