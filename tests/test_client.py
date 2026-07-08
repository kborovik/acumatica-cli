"""AcumaticaClient: wrap/unwrap, error surfacing, session lifecycle.

HTTP is faked with httpx.MockTransport — the real client code runs end to
end (login through _checked, logout on exit) without a live instance.
"""

import json
from typing import Any

import httpx
import pytest

from acumatica_cli.client import AcumaticaClient, unwrap, wrap
from acumatica_cli.config import Instance


def test_wrap_nests_values() -> None:
    assert wrap({"CurrencyID": "CAD", "Active": True}) == {
        "CurrencyID": {"value": "CAD"},
        "Active": {"value": True},
    }


def test_unwrap_keeps_only_value_fields() -> None:
    entity = {
        "CurrencyID": {"value": "CAD"},
        "id": "some-guid",
        "custom": {},
        "files": [],
    }
    assert unwrap(entity) == {"CurrencyID": "CAD"}


def _response(status: int, body: Any = None) -> httpx.Response:
    request = httpx.Request("PUT", "http://acu.test/AcumaticaERP/entity/x")
    if body is None:
        return httpx.Response(status, request=request)
    return httpx.Response(status, request=request, json=body)


def test_checked_passes_success_through() -> None:
    r = _response(200, body=[])
    assert AcumaticaClient._checked(r) is r  # pyright: ignore[reportPrivateUsage]


def test_checked_surfaces_exception_message() -> None:
    r = _response(500, body={"exceptionMessage": "PXSetupNotEntered"})
    with pytest.raises(RuntimeError, match=r"PUT /AcumaticaERP/entity/x -> 500"):
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="PXSetupNotEntered"):
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]


def test_checked_tolerates_non_json_error_body() -> None:
    request = httpx.Request("GET", "http://acu.test/AcumaticaERP/entity/x")
    r = httpx.Response(502, request=request, text="<html>bad gateway</html>")
    with pytest.raises(RuntimeError, match=r"GET /AcumaticaERP/entity/x -> 502$"):
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]


class Recorder:
    """MockTransport handler that records requests and plays canned responses."""

    def __init__(self, responses: dict[str, httpx.Response]):
        self.requests: list[httpx.Request] = []
        self.responses = responses

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.responses.items():
            if request.url.path.endswith(suffix):
                return response
        return httpx.Response(204)


def _client(instance: Instance, recorder: Recorder) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(recorder))


def test_login_sends_tenant_and_logout_runs_on_failure(instance: Instance) -> None:
    recorder = Recorder({})
    with pytest.raises(ValueError, match="boom"), _client(instance, recorder):
        raise ValueError("boom")

    login, logout = recorder.requests
    creds = json.loads(login.content)
    assert creds == {"name": "admin", "password": "pw", "tenant": "T1"}
    assert logout.url.path.endswith("/entity/auth/logout")
    # IIS returns 411 without an explicit Content-Length on logout
    assert logout.headers["Content-Length"] == "0"


def test_login_omits_blank_tenant(instance: Instance) -> None:
    recorder = Recorder({})
    with _client(instance.model_copy(update={"tenant": ""}), recorder):
        pass
    assert "tenant" not in json.loads(recorder.requests[0].content)


def test_login_failure_surfaces_acumatica_message(instance: Instance) -> None:
    recorder = Recorder(
        {
            "/auth/login": httpx.Response(
                500, json={"exceptionMessage": "A proper company ID cannot be..."}
            )
        }
    )
    with (
        pytest.raises(RuntimeError, match="A proper company ID"),
        _client(instance, recorder),
    ):
        pytest.fail("login should have raised")


def test_put_wraps_record_and_targets_endpoint(instance: Instance) -> None:
    recorder = Recorder({"/UnitsOfMeasure": httpx.Response(200, json={})})
    _client(instance, recorder).put("UnitsOfMeasure", {"FromUOM": "KG"})

    (request,) = recorder.requests
    assert request.method == "PUT"
    assert request.url.path == (
        "/AcumaticaERP/entity/Default/25.200.001/UnitsOfMeasure"
    )
    assert json.loads(request.content) == {"FromUOM": {"value": "KG"}}


def test_swagger_returns_raw_bytes_from_endpoint(instance: Instance) -> None:
    schema = b'{"openapi": "3.0.1"}'
    recorder = Recorder({"/swagger.json": httpx.Response(200, content=schema)})
    assert _client(instance, recorder).swagger() == schema

    (request,) = recorder.requests
    assert request.url.path == ("/AcumaticaERP/entity/Default/25.200.001/swagger.json")


def test_get_list_passes_odata_params(instance: Instance) -> None:
    recorder = Recorder({"/Currency": httpx.Response(200, json=[])})
    result = _client(instance, recorder).get_list(
        "Currency", params={"$filter": "CurrencyID eq 'CAD'"}
    )
    assert result == []
    assert recorder.requests[0].url.params["$filter"] == "CurrencyID eq 'CAD'"
