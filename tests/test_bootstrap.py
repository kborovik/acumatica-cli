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
    # the endpoint item must carry the name/version acu seeds through
    (endpoint,) = root.findall("EntityEndpoint")
    assert endpoint.get("name") == bootstrap.ENDPOINT


class Api:
    """MockTransport handler: scripted /CustomizationApi + auth responses."""

    def __init__(self, publish_end: list[httpx.Response], published: bool = False):
        self.requests: list[httpx.Request] = []
        self.publish_end = publish_end
        self.published = published

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
    contents = base64.b64decode(import_body["projectContents"])
    assert contents == bootstrap.package_zip()

    begin_body = json.loads(api.requests[3].content)
    assert begin_body["projectNames"] == [bootstrap.PACKAGE_NAME]
    assert begin_body["tenantMode"] == "Current"


def test_publish_skips_when_already_published(instance: Instance) -> None:
    api = Api(publish_end=[], published=True)
    assert _publish(instance, api) == "already published"
    assert _paths(api) == ["login", "getPublished", "logout"]


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
                                "message": "Delete project: acu-bootstrap",
                            },
                            {
                                "logType": "error",
                                "message": "The project is not found: acu-bootstrap",
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
