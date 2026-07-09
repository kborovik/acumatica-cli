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
from typing import Any

import httpx
import pytest

from acumatica_cli import bootstrap
from acumatica_cli.client import AcumaticaClient
from acumatica_cli.config import Instance


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


def test_package_zip_carries_the_bootstrap_endpoint() -> None:
    """Pin the <EntityEndpoint> serialization discovered in T12.

    Verified vs 26.101.0225 by live import round-trip: the <Endpoint> child
    is the XmlSerializer form of Model.Endpoint in the entity/maintenance/5.31
    namespace (the entity/data-model guess was the "Unknown root node"
    rejection); no .endpoint file is involved. View/DAC names read off the
    live box: CS101500 -> OrganizationMaint (mappings follow the screen's
    own bindings, T13 - AcctCD/AcctName on the primary BAccount view,
    OrganizationType/BaseCuryID on OrganizationView, CountryID on
    AddressDummy), CS206500 -> TermsMaint view TermsDef.
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
    assert set(entities) == {"Company", "CreditTerms"}
    # features stay OUT: contract-endpoint writes to CS100000 do not
    # persist (T3 verdict) - the CustomizationPlugin owns features
    assert entities["Company"].get("screen") == "CS101500"
    assert entities["CreditTerms"].get("screen") == "CS206500"
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


class Api:
    """MockTransport handler: scripted /CustomizationApi + auth responses."""

    def __init__(
        self,
        publish_end: list[httpx.Response],
        published: bool = False,
        content_exists: bool = True,
    ):
        self.requests: list[httpx.Request] = []
        self.publish_end = publish_end
        self.published = published
        self.content_exists = content_exists

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("/CustomizationApi/getPublished"):
            if self.published:
                return httpx.Response(
                    200, json={"projects": [{"name": bootstrap.PACKAGE_NAME}]}
                )
            # live shape (26.101.0225): no projects key at all, only a log
            return httpx.Response(
                200,
                json={
                    "log": [
                        {
                            "logType": "information",
                            "message": "The system does not contain published projects",
                        }
                    ]
                },
            )
        if path.endswith("/CustomizationApi/getProject"):
            if self.content_exists:
                return httpx.Response(200, json={"projectContentBase64": "UEs="})
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
        "getPublished",
        "import",
        "publishBegin",
        "publishEnd",
        "logout",
    ]

    import_body = json.loads(api.requests[2].content)
    assert import_body["projectName"] == bootstrap.PACKAGE_NAME
    assert import_body["isReplaceIfExists"] is True
    # projectContentBase64 = the live binder's field (26.101.0225); the
    # widely documented projectContents binds nothing on this build
    contents = base64.b64decode(import_body["projectContentBase64"])
    assert contents == bootstrap.package_zip()

    begin_body = json.loads(api.requests[3].content)
    assert begin_body["projectNames"] == [bootstrap.PACKAGE_NAME]
    assert begin_body["tenantMode"] == "Current"


def test_publish_skips_when_already_published(instance: Instance) -> None:
    api = Api(publish_end=[], published=True)
    assert _publish(instance, api) == "already published"
    assert _paths(api) == ["login", "getPublished", "getProject", "logout"]


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
