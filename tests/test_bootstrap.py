"""Bootstrap package + /CustomizationApi publish flow — fully offline.

Pins the package zip layout, the exact HTTP sequence (getPublished ->
import -> publishBegin -> publishEnd poll), idempotent skip, failure
surfacing, and the restart-tolerant poll loop.
"""

import base64
import io
import json
import xml.etree.ElementTree as ET
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any

import httpx
import pytest

from acumatica_cli import bootstrap
from acumatica_cli.client import AcumaticaClient
from acumatica_cli.config import Instance


def _plugin_source(features: list[str] | None = None) -> str:
    """The C# source as shipped inside package_zip's Graph item."""
    with zipfile.ZipFile(io.BytesIO(bootstrap.package_zip(features))) as zf:
        root = ET.fromstring(zf.read("project.xml"))
    (graph,) = root.findall("Graph")
    return graph.get("Source") or ""


def test_package_zip_holds_the_project_xml() -> None:
    with zipfile.ZipFile(io.BytesIO(bootstrap.package_zip())) as zf:
        assert zf.namelist() == ["project.xml"]
        root = ET.fromstring(zf.read("project.xml"))
    # root shape verified vs the training package on the live box: the
    # project name comes from the import call, not the XML
    assert root.tag == "Customization"
    assert root.get("product-version") == "26.101"
    # the plugin travels as a Graph item; source in the Source ATTRIBUTE
    # (CstCodeFile shape verified vs 26.101.0225 - CDATA is silently dropped)
    (graph,) = root.findall("Graph")
    assert graph.get("ClassName") == bootstrap.PLUGIN_CLASS
    assert graph.get("FileType") == "NewFile"
    source = graph.get("Source") or ""
    assert "class AcuBootstrapPlugin : Customization.CustomizationPlugin" in source
    assert "UpdateDatabase" in source
    # package name must survive ValidatePackageName: alphanumeric only
    assert bootstrap.PACKAGE_NAME.isalnum()


def test_plugin_source_on_disk_carries_no_feature_names() -> None:
    """V2: the feature set is config, never authored in plugin source (B6).

    The shipped .cs holds only the injection sentinel; every Enabled name
    arrives from features.yaml (or the Python code default) at build time.
    """
    source = (resources.files("acumatica_cli") / "bootstrap_plugin.cs").read_text(
        encoding="utf-8"
    )
    assert bootstrap.FEATURES_SENTINEL in source
    for name in bootstrap.DEFAULT_FEATURES:
        assert name not in source


def test_package_zip_injects_the_default_six() -> None:
    source = _plugin_source()
    assert bootstrap.FEATURES_SENTINEL not in source
    for name in bootstrap.DEFAULT_FEATURES:
        assert f'"{name}"' in source


def test_package_zip_injects_given_features() -> None:
    source = _plugin_source(["MultiCompany", "Multicurrency", "SubAccount"])
    assert '"Multicurrency"' in source
    assert '"SubAccount"' in source
    assert "FinancialModule" not in source


def test_plugin_logs_unknown_feature_names() -> None:
    """The silent-typo guard travels in the built package (T24).

    A misspelled features.yaml entry matches no FeaturesSet property and
    enables nothing; the plugin must say so in the publish log.
    """
    assert "unknown feature name" in _plugin_source(["FinancalModule"])


def test_load_features_defaults_when_file_absent(tmp_path: Path) -> None:
    assert bootstrap.load_features(tmp_path) == list(bootstrap.DEFAULT_FEATURES)


def test_load_features_reads_the_yaml_list(tmp_path: Path) -> None:
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "features.yaml").write_text(
        "# enabled FeaturesSet bits\n- MultiCompany\n- Multicurrency\n"
    )
    assert bootstrap.load_features(tmp_path) == ["MultiCompany", "Multicurrency"]


@pytest.mark.parametrize(
    "body",
    [
        "features: [MultiCompany]",  # mapping, not a list
        "[]",  # empty list enables nothing - authoring mistake
        "- Multi Company",  # not a property name (space)
        "- 42",  # not a string
    ],
)
def test_load_features_rejects_bad_files(tmp_path: Path, body: str) -> None:
    (tmp_path / "bootstrap").mkdir()
    (tmp_path / "bootstrap" / "features.yaml").write_text(body)
    with pytest.raises(SystemExit, match=r"features\.yaml"):
        bootstrap.load_features(tmp_path)


def test_package_zip_carries_the_bootstrap_endpoint() -> None:
    """Pin the <EntityEndpoint> serialization discovered in T12.

    Verified vs 26.101.0225 by live import round-trip: the <Endpoint> child
    is the XmlSerializer form of Model.Endpoint in the entity/maintenance/5.31
    namespace (the entity/data-model guess was the "Unknown root node"
    rejection); no .endpoint file is involved. View/DAC names read off the
    live box: CS101500 -> OrganizationMaint (mappings follow the screen's
    own bindings, T13 - AcctCD/AcctName on the primary BAccount view,
    OrganizationType/BaseCuryID on OrganizationView, CountryID on
    AddressDummy), CS206500 -> TermsMaint view TermsDef, CM202000 ->
    CurrencyMaint (T29 - general info on the primary CuryListRecords view
    of CurrencyList, the GL Accounts tab on CuryRecords of CM.Currency;
    every gain/loss Acct/Sub pair is required at persist, so all ten pairs
    ship).
    """
    ns = "{http://www.acumatica.com/entity/maintenance/5.31}"
    with zipfile.ZipFile(io.BytesIO(bootstrap.package_zip())) as zf:
        root = ET.fromstring(zf.read("project.xml"))
    (item,) = root.findall("EntityEndpoint")
    (endpoint,) = item.findall(f"{ns}Endpoint")
    assert endpoint.get("name") == "Bootstrap"
    assert endpoint.get("version") == "1.0.0"
    # SystemContracts.V4 is the build's only IsCurrent implementation
    assert endpoint.get("systemContractVersion") == "4"
    entities = {e.get("name"): e for e in endpoint.findall(f"{ns}TopLevelEntity")}
    assert set(entities) == {"Company", "CreditTerms", "Currency"}
    # features stay OUT: contract-endpoint writes to CS100000 do not
    # persist (T3 verdict) - the CustomizationPlugin owns features
    assert entities["Company"].get("screen") == "CS101500"
    assert entities["CreditTerms"].get("screen") == "CS206500"
    assert entities["Currency"].get("screen") == "CM202000"
    # mappings follow the screen's own bindings (T13): a DAC prop existing
    # on the primary view is not enough - writes land only through the
    # view the screen edits. Field names = DAC props verbatim.
    views: dict[str, dict[str, str]] = {
        "Company": {
            "AcctCD": "BAccount",
            "AcctName": "BAccount",
            "OrganizationType": "OrganizationView",
            "BaseCuryID": "OrganizationView",
            "CountryID": "AddressDummy",
        },
        "CreditTerms": dict.fromkeys(
            (
                "TermsID",
                "Descr",
                "VisibleTo",
                "DueType",
                "DayDue00",
                "DiscType",
                "DiscPercent",
            ),
            "TermsDef",
        ),
        "Currency": {
            # general info -> the primary CurrencyList view; IsFinancial is
            # what makes the currency financial (B8: the Default endpoint
            # cannot reach it as a CM.Currency-creating write)
            **dict.fromkeys(
                (
                    "CuryID",
                    "Description",
                    "CurySymbol",
                    "DecimalPlaces",
                    "IsActive",
                    "IsFinancial",
                ),
                "CuryListRecords",
            ),
            # GL Accounts tab -> CuryRecords (CM.Currency): all ten
            # gain/loss Acct/Sub pairs carry PXDefault(PersistingCheck.Null)
            # with no setup default - required at persist
            **dict.fromkeys(
                (
                    f"{kind}{side}{part}ID"
                    for kind in (
                        "Real",
                        "Reval",
                        "Translation",
                        "Unrealized",
                        "Rounding",
                    )
                    for side in ("Gain", "Loss")
                    for part in ("Acct", "Sub")
                ),
                "CuryRecords",
            ),
        },
    }
    for entity, expected in views.items():
        fields = {
            f.get("name") for f in entities[entity].findall(f"{ns}Fields/{ns}Field")
        }
        mappings = {
            m.get("field"): m.find(f"{ns}To")
            for m in entities[entity].findall(f"{ns}Mappings/{ns}Mapping")
        }
        assert set(mappings) == fields == set(expected)
        for name, to in mappings.items():
            assert name is not None
            assert to is not None
            assert to.get("object") == expected[name]
            assert to.get("field") == name
    assert (
        entities["Company"].findall(f"{ns}Fields/{ns}Field")[0].get("type")
        == "StringValue"
    )
    # Currency value types (T29): DecimalPlaces is Nullable<Int16> ->
    # ShortValue, the two flags -> BooleanValue, everything else (keys,
    # text, segment-mask CD strings) -> StringValue
    currency_types = {
        f.get("name"): f.get("type")
        for f in entities["Currency"].findall(f"{ns}Fields/{ns}Field")
    }
    assert currency_types["DecimalPlaces"] == "ShortValue"
    assert currency_types["IsActive"] == "BooleanValue"
    assert currency_types["IsFinancial"] == "BooleanValue"
    assert currency_types["CuryID"] == "StringValue"
    assert currency_types["RealGainAcctID"] == "StringValue"


def _served_package(description: str) -> str:
    """A server-style getProject re-serialization, base64-encoded.

    The live server hands back its own serialization of the project — not
    the imported bytes — with the import's projectDescription in the root
    description attribute (verified vs 26.101.0225). Only that attribute
    matters to the skip gate.
    """
    root = ET.Element(
        "Customization",
        {"level": "", "description": description, "product-version": "26.101"},
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("project.xml", ET.tostring(root))
    return base64.b64encode(buf.getvalue()).decode("ascii")


class Api:
    """MockTransport handler: scripted /CustomizationApi + auth responses."""

    def __init__(
        self,
        publish_end: list[httpx.Response],
        published: bool = False,
        content_exists: bool = True,
        description: str | None = None,
    ):
        self.requests: list[httpx.Request] = []
        self.publish_end = publish_end
        self.published = published
        self.content_exists = content_exists
        # what the "published" project's description claims; default = the
        # digest of the default-features package (the matching state)
        self.description = (
            bootstrap.package_description(bootstrap.package_zip())
            if description is None
            else description
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/CustomizationApi/getPublished"):
            # live shape (26.101.0225) when nothing is published: no
            # projects key at all, only a log
            body: dict[str, Any] = (
                {"projects": [{"name": bootstrap.PACKAGE_NAME}]}
                if self.published
                else {
                    "log": [
                        {
                            "logType": "information",
                            "message": "The system does not contain published projects",
                        }
                    ]
                }
            )
            return httpx.Response(200, json=body)
        if path.endswith("/CustomizationApi/getProject"):
            if self.content_exists:
                return httpx.Response(
                    200,
                    json={"projectContentBase64": _served_package(self.description)},
                )
            # live shape: a recreated tenant lists the publication while the
            # project content is gone - getProject reports in-band
            return httpx.Response(
                200,
                json={
                    "log": [
                        {
                            "logType": "error",
                            "message": "The project is not found: AcuBootstrap",
                        }
                    ]
                },
            )
        if path.endswith("/CustomizationApi/publishEnd"):
            return self.publish_end.pop(0)
        if path.endswith("/Frames/Login.aspx"):
            # tenant-guard probe: report the session landed on the fixture
            # instance's tenant so the context manager opens cleanly
            return httpx.Response(
                200, text='<input id="txtSingleCompany" value="T1" />'
            )
        return httpx.Response(204)


def _publish(instance: Instance, api: Api) -> str:
    with AcumaticaClient(instance, transport=httpx.MockTransport(api)) as client:
        return bootstrap.publish(client, timeout=60.0, poll=0.0)


def _paths(api: Api) -> list[str]:
    return [r.url.path.rsplit("/", 1)[-1] for r in api.requests]


def test_publish_runs_import_then_publish_sequence(instance: Instance) -> None:
    api = Api(publish_end=[httpx.Response(200, json={"isCompleted": True})])
    assert _publish(instance, api) == "published"
    assert _paths(api) == [
        "login",
        "Login.aspx",
        "getPublished",
        "import",
        "publishBegin",
        "publishEnd",
        "logout",
    ]

    import_body = json.loads(api.requests[3].content)
    assert import_body["projectName"] == bootstrap.PACKAGE_NAME
    assert import_body["isReplaceIfExists"] is True
    # projectContentBase64 = the live binder's field (26.101.0225); the
    # widely documented projectContents binds nothing on this build
    contents = base64.b64decode(import_body["projectContentBase64"])
    assert contents == bootstrap.package_zip()
    # the description embeds the content digest (V4) - the description is
    # the CustomizationApi's one round-trip channel, so this is what the
    # next run's skip gate reads back
    assert import_body["projectDescription"] == bootstrap.package_description(contents)
    assert bootstrap.content_digest(contents) in import_body["projectDescription"]

    begin_body = json.loads(api.requests[4].content)
    assert begin_body["projectNames"] == [bootstrap.PACKAGE_NAME]
    assert begin_body["tenantMode"] == "Current"


def test_publish_builds_the_package_with_features(instance: Instance) -> None:
    """The features list flows publish -> package_zip -> imported bytes (V2)."""
    api = Api(publish_end=[httpx.Response(200, json={"isCompleted": True})])
    with AcumaticaClient(instance, transport=httpx.MockTransport(api)) as client:
        bootstrap.publish(client, features=["MultiCompany"], timeout=60.0, poll=0.0)
    import_body = json.loads(api.requests[3].content)
    contents = base64.b64decode(import_body["projectContentBase64"])
    assert contents == bootstrap.package_zip(["MultiCompany"])
    assert contents != bootstrap.package_zip()


def test_publish_skips_when_content_matches(instance: Instance) -> None:
    """Skip = published + content digest match, never existence alone (V4)."""
    api = Api(publish_end=[], published=True)
    assert _publish(instance, api) == "already published"
    assert _paths(api) == [
        "login",
        "Login.aspx",
        "getPublished",
        "getProject",
        "logout",
    ]


@pytest.mark.parametrize(
    "stale_description",
    [
        # same package, different content: another features set was published
        bootstrap.package_description(bootstrap.package_zip(["MultiCompany"])),
        # pre-digest description (packages published before the gate existed)
        bootstrap.PACKAGE_DESCRIPTION,
    ],
)
def test_publish_reruns_on_digest_mismatch(
    instance: Instance, stale_description: str
) -> None:
    """Changed content republishes; a stale skip silently starves config (B7).

    The published project exists and getPublished lists it — existence alone
    would skip. The description's digest does not match the package built
    now, so the gate must fall through to a full reimport + republish
    carrying the new digest (V4).
    """
    api = Api(
        publish_end=[httpx.Response(200, json={"isCompleted": True})],
        published=True,
        description=stale_description,
    )
    assert _publish(instance, api) == "published"
    assert _paths(api) == [
        "login",
        "Login.aspx",
        "getPublished",
        "getProject",
        "import",
        "publishBegin",
        "publishEnd",
        "logout",
    ]
    import_body = json.loads(api.requests[4].content)
    assert import_body["projectDescription"] == bootstrap.package_description(
        bootstrap.package_zip()
    )


def test_publish_reruns_when_publication_is_stale(instance: Instance) -> None:
    """Recreated tenant: publication listed, content gone -> full re-publish.

    getPublished alone is a false idempotence proxy (verified live): a tenant
    deleted and recreated under the same CompanyID still lists the package
    while the project content and the plugin's writes died with the tenant.
    """
    api = Api(
        publish_end=[httpx.Response(200, json={"isCompleted": True})],
        published=True,
        content_exists=False,
    )
    assert _publish(instance, api) == "published"
    assert _paths(api) == [
        "login",
        "Login.aspx",
        "getPublished",
        "getProject",
        "import",
        "publishBegin",
        "publishEnd",
        "logout",
    ]


def test_publish_polls_until_completed(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    naps: list[float] = []
    monkeypatch.setattr(bootstrap.time, "sleep", naps.append)
    api = Api(
        publish_end=[
            httpx.Response(200, json={"isCompleted": False}),
            httpx.Response(200, json={"isCompleted": True}),
        ]
    )
    assert _publish(instance, api) == "published"
    assert _paths(api).count("publishEnd") == 2
    assert naps == [0.0]


def test_import_error_in_200_log_raises(instance: Instance) -> None:
    """The CustomizationApi reports failures in-band: 200 + logType error.

    Payload shape sampled live (26.101.0225): an import that rejects the
    package still answers 200, with the failure only in the log entries.
    """

    class ImportFails(Api):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/CustomizationApi/import"):
                self.requests.append(request)
                return httpx.Response(
                    200,
                    json={
                        "log": [
                            {
                                "logType": "information",
                                "message": "Delete project: AcuBootstrap",
                            },
                            {
                                "logType": "error",
                                "message": "The project is not found: AcuBootstrap",
                            },
                        ]
                    },
                )
            return super().__call__(request)

    api = ImportFails(publish_end=[])
    with pytest.raises(RuntimeError, match="The project is not found"):
        _publish(instance, api)
    # failed import must stop the flow before publishBegin
    assert "publishBegin" not in _paths(api)


def test_publish_failure_surfaces_log_tail(instance: Instance) -> None:
    api = Api(
        publish_end=[
            httpx.Response(
                200,
                json={
                    "isFailed": True,
                    "log": [{"message": "Endpoint validation failed"}],
                },
            )
        ]
    )
    with pytest.raises(RuntimeError, match="Endpoint validation failed"):
        _publish(instance, api)


def test_publish_times_out(instance: Instance, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter([0.0, 100.0])  # deadline = 0 + 60; second check is past it
    monkeypatch.setattr(bootstrap.time, "monotonic", lambda: next(clock))
    api = Api(publish_end=[httpx.Response(200, json={"isCompleted": False})])
    with pytest.raises(RuntimeError, match="did not complete within"):
        _publish(instance, api)


def test_publish_tolerates_transport_errors_while_polling(
    instance: Instance, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bootstrap.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky_end(self: AcumaticaClient) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("site restarting")
        return {"isCompleted": True}

    monkeypatch.setattr(AcumaticaClient, "customization_publish_end", flaky_end)
    api = Api(publish_end=[])
    assert _publish(instance, api) == "published"
    assert calls["n"] == 2
