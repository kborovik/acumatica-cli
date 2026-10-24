"""AcumaticaClient: wrap/unwrap, error surfacing, session lifecycle.

HTTP is faked with httpx.MockTransport — the real client code runs end to
end (login through _checked, logout on exit) without a live instance.
"""

import json
from typing import Any

import httpx
import pytest

from acumatica_cli.client import (
    AcumaticaClient,
    SessionLostError,
    parse_entity_list,
    parse_entity_response,
    unwrap,
    wrap,
)
from acumatica_cli.config import Instance


def test_wrap_nests_values() -> None:
    assert wrap({"CurrencyID": "CAD", "Active": True}) == {
        "CurrencyID": {"value": "CAD"},
        "Active": {"value": True},
    }


def test_wrap_leaves_id_and_delete_bare() -> None:
    """V54: wrap already sends id/delete bare (update vs insert handle)."""
    assert wrap(
        {
            "id": "hdr",
            "CurrencyID": "CAD",
            "StockComponents": [{"id": "row", "delete": True, "ComponentID": "MB-CM4"}],
        }
    ) == {
        "id": "hdr",
        "CurrencyID": {"value": "CAD"},
        "StockComponents": [
            {"id": "row", "delete": True, "ComponentID": {"value": "MB-CM4"}}
        ],
    }


def test_unwrap_keeps_id_and_delete_on_value_field_rows() -> None:
    """V54: value-field records keep id/delete; empty files list stays elided."""
    entity = {
        "CurrencyID": {"value": "CAD"},
        "id": "some-guid",
        "delete": False,
        "custom": {},
        "files": [],
    }
    assert unwrap(entity) == {
        "CurrencyID": "CAD",
        "id": "some-guid",
        "delete": False,
    }


def test_wrap_detail_list_rows_wrap_the_list_does_not() -> None:
    """T60: the T50-proven JournalTransaction payload shape, exactly.

    The detail list itself is never value-wrapped; each row wraps like a
    record. Anything else 422s or silently drops the rows.
    """
    assert wrap(
        {
            "KitInventoryID": "GW-EDGE",
            "StockComponents": [{"ComponentID": "MB-CM4", "ComponentQty": 1}],
        }
    ) == {
        "KitInventoryID": {"value": "GW-EDGE"},
        "StockComponents": [
            {"ComponentID": {"value": "MB-CM4"}, "ComponentQty": {"value": 1}}
        ],
    }


def test_unwrap_inverts_detail_lists_and_elides_noise() -> None:
    """T60/V54: detail rows keep id; files-style id-only rows elide.

    Expanded `files` descriptors are plain dicts (no value wrapping) and
    must not surface as id-only rows; empty lists stay elided.
    """
    entity = {
        "KitInventoryID": {"value": "GW-EDGE"},
        "StockComponents": [
            {
                "ComponentID": {"value": "MB-CM4"},
                "ComponentQty": {"value": 1.0},
                "id": "row-guid",
            }
        ],
        "NonStockComponents": [],
        "files": [{"id": "f", "filename": "a.txt"}],
    }
    assert unwrap(entity) == {
        "KitInventoryID": "GW-EDGE",
        "StockComponents": [
            {"ComponentID": "MB-CM4", "ComponentQty": 1.0, "id": "row-guid"}
        ],
    }


def test_unwrap_wrap_round_trips_row_id() -> None:
    """V54: GET unwrap then PUT wrap keeps detail-row id bare; files stay elided."""
    entity = {
        "KitInventoryID": {"value": "GW-EDGE"},
        "id": "hdr-guid",
        "StockComponents": [
            {
                "ComponentID": {"value": "MB-CM4"},
                "ComponentQty": {"value": 1.0},
                "id": "row-guid",
            }
        ],
        "files": [{"id": "f", "filename": "a.txt"}],
    }
    plain = unwrap(entity)
    assert plain == {
        "KitInventoryID": "GW-EDGE",
        "id": "hdr-guid",
        "StockComponents": [
            {"ComponentID": "MB-CM4", "ComponentQty": 1.0, "id": "row-guid"}
        ],
    }
    assert wrap(plain) == {
        "KitInventoryID": {"value": "GW-EDGE"},
        "id": "hdr-guid",
        "StockComponents": [
            {
                "ComponentID": {"value": "MB-CM4"},
                "ComponentQty": {"value": 1.0},
                "id": "row-guid",
            }
        ],
    }


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


def test_checked_appends_inner_exception_when_top_present() -> None:
    """V46: innerException.exceptionMessage appends even when top message is set."""
    r = _response(
        500,
        body={
            "exceptionMessage": "Operation failed",
            "innerException": {
                "exceptionMessage": "The system failed to commit the Components row."
            },
        },
    )
    with pytest.raises(RuntimeError, match="Operation failed") as ei:
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]
    assert "The system failed to commit the Components row." in str(ei.value)


def test_checked_tolerates_non_json_error_body() -> None:
    request = httpx.Request("GET", "http://acu.test/AcumaticaERP/entity/x")
    r = httpx.Response(502, request=request, text="<html>bad gateway</html>")
    with pytest.raises(RuntimeError, match=r"GET /AcumaticaERP/entity/x -> 502"):
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]
    with pytest.raises(RuntimeError, match="bad gateway"):
        AcumaticaClient._checked(r)  # pyright: ignore[reportPrivateUsage]


def _login_page(tenant: str) -> httpx.Response:
    """The sign-in page fragment the tenant guard parses (live shape 26.101.0225)."""
    return httpx.Response(
        200,
        text=(
            '<input name="ctl00$phUser$txtSingleCompany" type="hidden" '
            f'id="txtSingleCompany" value="{tenant}" />'
        ),
    )


class Recorder:
    """MockTransport handler that records requests and plays canned responses.

    landed is what the tenant-guard probe (GET /Frames/Login.aspx) reports as
    the session's landed tenant; it defaults to the fixture instance's tenant
    so sessions open cleanly unless a test steers the landing elsewhere.
    """

    def __init__(self, responses: dict[str, httpx.Response], landed: str = "T1"):
        self.requests: list[httpx.Request] = []
        self.responses = responses
        self.landed = landed

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        for suffix, response in self.responses.items():
            if request.url.path.endswith(suffix):
                return response
        if request.url.path.endswith("/Frames/Login.aspx"):
            return _login_page(self.landed)
        return httpx.Response(204)


def _client(instance: Instance, recorder: Recorder) -> AcumaticaClient:
    return AcumaticaClient(instance, transport=httpx.MockTransport(recorder))


def test_login_sends_tenant_and_logout_runs_on_failure(instance: Instance) -> None:
    recorder = Recorder({})
    with pytest.raises(ValueError, match="boom"), _client(instance, recorder):
        raise ValueError("boom")

    login, probe, logout = recorder.requests
    creds = json.loads(login.content)
    assert creds == {"name": "admin", "password": "pw", "tenant": "T1"}
    assert probe.url.path.endswith("/Frames/Login.aspx")
    assert logout.url.path.endswith("/entity/auth/logout")
    # IIS returns 411 without an explicit Content-Length on logout
    assert logout.headers["Content-Length"] == "0"


def test_blank_tenant_refused_before_any_http(instance: Instance) -> None:
    # V5 guard: empty tenant = the one silent default-tenant login left
    recorder = Recorder({})
    blank = instance.model_copy(update={"tenant": ""})
    with (
        pytest.raises(RuntimeError, match="explicit tenant"),
        _client(blank, recorder),
    ):
        pytest.fail("session should not open without a tenant")
    assert recorder.requests == []


def test_tenant_guard_refuses_mismatch_and_logs_out(instance: Instance) -> None:
    # V5, B5: a stale tenant map (or a single-tenant instance, which accepts
    # any name) lands the session on the default tenant with a clean 204 -
    # the landed-tenant probe is the only thing standing between that and a
    # wrong-tenant write or a false-green diff.
    recorder = Recorder({}, landed="Company")
    with (
        pytest.raises(
            RuntimeError,
            match=r"asked for tenant 'T1'.*'Company'.*acu tenant recycle",
        ),
        _client(instance, recorder),
    ):
        pytest.fail("session should have been refused on tenant mismatch")
    # V6: __exit__ never runs when __enter__ raises, so the guard must have
    # logged out itself - sessions count against the license cap
    assert recorder.requests[-1].url.path.endswith("/entity/auth/logout")


def test_tenant_guard_refuses_unreadable_probe_page(instance: Instance) -> None:
    # fail closed: if the page shape changes on a new build, refuse rather
    # than silently degrade into the pre-guard behavior
    recorder = Recorder(
        {"/Frames/Login.aspx": httpx.Response(200, text="<html>no marker</html>")}
    )
    with (
        pytest.raises(RuntimeError, match="txtSingleCompany"),
        _client(instance, recorder),
    ):
        pytest.fail("session should have been refused without a landed tenant")
    assert recorder.requests[-1].url.path.endswith("/entity/auth/logout")


def test_tenant_guard_match_is_case_insensitive(instance: Instance) -> None:
    recorder = Recorder({}, landed="t1")
    with _client(instance, recorder):
        pass
    assert recorder.requests[-1].url.path.endswith("/entity/auth/logout")


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


def test_relogin_logout_login_probe_without_closing(instance: Instance) -> None:
    """V5/B24 seam: mid-session re-login keeps the httpx client open (V6)."""
    recorder = Recorder({"/UnitsOfMeasure": httpx.Response(200, json={})})
    with _client(instance, recorder) as client:
        n_after_enter = len(recorder.requests)
        client.relogin()
        mid = recorder.requests[n_after_enter:]
        assert [r.url.path.split("/")[-1] for r in mid] == [
            "logout",
            "login",
            "Login.aspx",
        ]
        assert mid[0].headers["Content-Length"] == "0"
        creds = json.loads(mid[1].content)
        assert creds == {"name": "admin", "password": "pw", "tenant": "T1"}
        # transport still live - another PUT reaches the mock
        client.put("UnitsOfMeasure", {"FromUOM": "KG"})
        assert recorder.requests[-1].method == "PUT"
    # outer __exit__ still logs out once at the end
    assert recorder.requests[-1].url.path.endswith("/entity/auth/logout")


def test_refresh_after_company_once_per_session(instance: Instance) -> None:
    """V5: first Company boundary re-logins once; second call is a no-op."""
    recorder = Recorder({})
    with _client(instance, recorder) as client:
        client.refresh_after_company()
        n_after_first = len(recorder.requests)
        client.refresh_after_company()
        assert len(recorder.requests) == n_after_first  # no second bounce


def test_put_wraps_record_and_targets_endpoint(instance: Instance) -> None:
    recorder = Recorder({"/UnitsOfMeasure": httpx.Response(200, json={})})
    _client(instance, recorder).put("UnitsOfMeasure", {"FromUOM": "KG"})

    (request,) = recorder.requests
    assert request.method == "PUT"
    assert request.url.path == (
        "/AcumaticaERP/entity/Default/25.200.001/UnitsOfMeasure"
    )
    assert json.loads(request.content) == {"FromUOM": {"value": "KG"}}


def test_api_version_override_reaches_url(instance: Instance) -> None:
    # V11/I.cfg: api_version is the sole version knob; the endpoint-name half
    # stays hardcoded Default (custom endpoints come in per call via seed)
    versioned = instance.model_copy(update={"api_version": "24.200.001"})
    recorder = Recorder({"/UnitsOfMeasure": httpx.Response(200, json={})})
    _client(versioned, recorder).put("UnitsOfMeasure", {"FromUOM": "KG"})

    (request,) = recorder.requests
    assert request.url.path == (
        "/AcumaticaERP/entity/Default/24.200.001/UnitsOfMeasure"
    )


def test_url_resolves_symbolic_default(instance: Instance) -> None:
    """V20: endpoint='default' is Default/<api_version> at the client choke point."""
    versioned = instance.model_copy(update={"api_version": "23.200.001"})
    client = _client(versioned, Recorder({}))
    assert client._url("Warehouse", "default") == (  # pyright: ignore[reportPrivateUsage]
        "/entity/Default/23.200.001/Warehouse"
    )
    assert client._url("Warehouse", None) == (  # pyright: ignore[reportPrivateUsage]
        "/entity/Default/23.200.001/Warehouse"
    )
    assert (
        client._url(  # pyright: ignore[reportPrivateUsage]
            "Warehouse", "Bootstrap/1.11.0"
        )
        == "/entity/Bootstrap/1.11.0/Warehouse"
    )


def test_parse_entity_list_name_version_rows() -> None:
    # T74/V12/V31: legacy top-level array — [{name, version, ...}, ...]
    body = [
        {"name": "Default", "version": "25.200.001", "href": "/entity/Default/..."},
        {"name": "Bootstrap", "version": "1.0.0"},
    ]
    r = httpx.Response(200, json=body)
    assert parse_entity_list(r) == [
        ("Default", "25.200.001"),
        ("Bootstrap", "1.0.0"),
    ]


def test_parse_entity_list_wrapper_endpoints() -> None:
    # T90/V31: 26.x GET /entity wraps rows under endpoints (gh #20)
    body = {
        "version": {
            "acumaticaBuildVersion": "26.101.0225",
            "databaseVersion": "26.101.0225",
        },
        "endpoints": [
            {
                "name": "Default",
                "version": "25.200.001",
                "href": "/AcumaticaERP/entity/Default/25.200.001/",
            },
            {"name": "Bootstrap", "version": "1.0.0"},
        ],
    }
    r = httpx.Response(200, json=body)
    assert parse_entity_list(r) == [
        ("Default", "25.200.001"),
        ("Bootstrap", "1.0.0"),
    ]
    endpoints, build = parse_entity_response(r)
    assert endpoints == [("Default", "25.200.001"), ("Bootstrap", "1.0.0")]
    assert build == "26.101.0225"


def test_parse_entity_response_array_has_no_build() -> None:
    # T92: legacy array → endpoints only, no ERP build id
    r = httpx.Response(200, json=[{"name": "Default", "version": "25.200.001"}])
    endpoints, build = parse_entity_response(r)
    assert endpoints == [("Default", "25.200.001")]
    assert build is None


def test_parse_entity_list_fail_closed_on_object() -> None:
    # T90/V31: object without endpoints array stays fail-closed
    r = httpx.Response(200, json={"Default": ["25.200.001"]})
    with pytest.raises(RuntimeError, match=r"not parseable as endpoint list"):
        parse_entity_list(r)


def test_parse_entity_list_fail_closed_empty_endpoints() -> None:
    r = httpx.Response(200, json={"endpoints": []})
    with pytest.raises(RuntimeError, match=r"not parseable as endpoint list"):
        parse_entity_list(r)


def test_parse_entity_list_fail_closed_empty_array() -> None:
    r = httpx.Response(200, json=[])
    with pytest.raises(RuntimeError, match=r"not parseable as endpoint list"):
        parse_entity_list(r)


def test_list_endpoints_hits_entity_root(instance: Instance) -> None:
    recorder = Recorder(
        {
            "/entity": httpx.Response(
                200,
                json=[{"name": "Default", "version": "25.200.001"}],
            )
        }
    )
    client = _client(instance, recorder)
    assert client.list_endpoints() == [("Default", "25.200.001")]
    assert any(r.url.path.rstrip("/").endswith("/entity") for r in recorder.requests)


def test_list_endpoints_accepts_wrapper(instance: Instance) -> None:
    # T90: config check path — Default under 26.x wrapper is green
    recorder = Recorder(
        {
            "/entity": httpx.Response(
                200,
                json={
                    "version": {"acumaticaBuildVersion": "26.101.0225"},
                    "endpoints": [
                        {"name": "Default", "version": "25.200.001"},
                    ],
                },
            )
        }
    )
    assert _client(instance, recorder).list_endpoints() == [("Default", "25.200.001")]


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


# -- V46: field-level error surfacing (T186/T187) --


def test_field_errors_from_422_value_wrappers() -> None:
    """V46: nested Field.error paths collected from a contract 422 body."""
    body = {
        "exceptionMessage": (
            "Inserting  'PaymentMethod' record raised at least one error."
        ),
        "exceptionType": "PX.Api.ContractBased.OutcomeEntityHasErrorsException",
        "PaymentMethodID": {
            "value": "BILL",
            "error": "Error: MeansOfPayment is invalid.",
        },
        "MeansOfPayment": {
            "value": "External Payment Processor",
            "error": "Error: 'External Payment Processor' is not allowed.",
        },
    }
    errs = AcumaticaClient._field_errors(body)  # pyright: ignore[reportPrivateUsage]
    assert errs == [
        "PaymentMethodID: Error: MeansOfPayment is invalid.",
        "MeansOfPayment: Error: 'External Payment Processor' is not allowed.",
    ]


def test_field_errors_under_entity_and_detail_rows() -> None:
    body = {
        "exceptionMessage": "Updating  'User' record raised at least one error.",
        "entity": {
            "Username": {"value": "soadmin", "error": "Error: Username exists."},
            "Roles": [
                {
                    "Rolename": {
                        "value": "SO Admin",
                        "error": "Error: Role not found.",
                    },
                }
            ],
        },
    }
    errs = AcumaticaClient._field_errors(body)  # pyright: ignore[reportPrivateUsage]
    assert "entity.Username: Error: Username exists." in errs
    assert "entity.Roles[0].Rolename: Error: Role not found." in errs


def test_put_422_surfaces_field_errors_in_runtime_error(instance: Instance) -> None:
    """V46/T187: put RuntimeError detail includes Field.error text, not status only."""
    body = {
        "exceptionMessage": "Inserting  'Vendor' record raised at least one error.",
        "VendorID": {"value": "X", "error": "Error: VendorID cannot be empty."},
    }
    recorder = Recorder({"/Vendor": httpx.Response(422, json=body)})
    with pytest.raises(RuntimeError, match=r"VendorID.*cannot be empty") as ei:
        _client(instance, recorder).put("Vendor", {"VendorID": "X"})
    msg = str(ei.value)
    assert "422" in msg
    assert "Inserting  'Vendor'" in msg


def test_invoke_422_surfaces_field_errors(instance: Instance) -> None:
    body = {
        "exceptionMessage": "The request is invalid.",
        "entity": {
            "FinancialYear": {
                "value": 1999,
                "error": "Error: Year is out of range.",
            }
        },
    }
    recorder = Recorder(
        {"/MasterCalendar/GenerateCalendar": httpx.Response(422, json=body)}
    )
    with pytest.raises(RuntimeError, match=r"FinancialYear.*out of range"):
        _client(instance, recorder).invoke(
            "MasterCalendar",
            "GenerateCalendar",
            {"FinancialYear": 1999},
            {"FromYear": 1999, "ToYear": 1999},
        )


def _always_202(request: httpx.Request) -> httpx.Response:
    """A long action whose status URL never stops answering 202."""
    if request.url.path.endswith("/Frames/Login.aspx"):
        return _login_page("T1")
    if request.url.path.endswith("/GenerateCalendar"):
        return httpx.Response(202, headers={"Location": "GenerateCalendar/status/x"})
    if request.url.path.endswith("/status/x"):
        return httpx.Response(202)
    return httpx.Response(204)


def test_invoke_202_poll_raises_after_timeout(instance: Instance) -> None:
    """V63: a status URL stuck on 202 fails, naming entity, action, elapsed."""
    client = AcumaticaClient(instance, transport=httpx.MockTransport(_always_202))
    client.poll_interval = 0
    with pytest.raises(
        RuntimeError,
        match=r"MasterCalendar/GenerateCalendar still 202 after \d+s \(timeout 0.05s\)",
    ):
        client.invoke("MasterCalendar", "GenerateCalendar", {}, timeout=0.05)


def test_invoke_202_poll_uses_default_timeout(instance: Instance) -> None:
    client = AcumaticaClient(instance, transport=httpx.MockTransport(_always_202))
    client.poll_interval = 0
    client.invoke_timeout = 0
    with pytest.raises(RuntimeError, match=r"still 202 .*\(timeout 0s\)"):
        client.invoke("MasterCalendar", "GenerateCalendar", {})


# -- gh #53: bounded logout; a logout failure never masks the real error --


class _DeadLogout(Recorder):
    """Recorder whose logout POST raises httpx.ConnectError."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/entity/auth/logout"):
            self.requests.append(request)
            raise httpx.ConnectError("refused", request=request)
        return super().__call__(request)


def test_logout_uses_short_timeout(instance: Instance) -> None:
    recorder = Recorder({})
    with _client(instance, recorder):
        pass
    logout = recorder.requests[-1]
    assert logout.url.path.endswith("/entity/auth/logout")
    assert logout.extensions["timeout"]["read"] == AcumaticaClient.logout_timeout
    assert AcumaticaClient.logout_timeout == 10.0


def test_logout_failure_on_error_exit_keeps_original(
    instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """PUT 422 then logout ConnectError: the 422 text propagates, logout warns."""
    body = {"exceptionMessage": "UOM 'KG' is reserved."}
    recorder = _DeadLogout({"/UnitsOfMeasure": httpx.Response(422, json=body)})
    with (
        pytest.raises(RuntimeError, match="UOM 'KG' is reserved"),
        _client(instance, recorder) as client,
    ):
        client.put("UnitsOfMeasure", {"UOM": "KG"})
    err = " ".join(capsys.readouterr().err.split())  # rich wraps
    assert "! logout failed: cannot connect to " in err
    assert "(acu tenant recycle)" in err


def test_logout_failure_on_clean_exit_warns(
    instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """Clean exit: logout failure is a warning, not an exception."""
    with _client(instance, _DeadLogout({})):
        pass
    assert "! logout failed: cannot connect to " in capsys.readouterr().err


def test_login_failure_survives_logout_failure(
    instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """__enter__ cleanup logout failing must not replace the login error."""
    recorder = _DeadLogout({}, landed="OTHER")
    with pytest.raises(RuntimeError, match="tenant guard"), _client(instance, recorder):
        pass
    assert "! logout failed:" in capsys.readouterr().err


def test_logout_error_status_warns(
    instance: Instance, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-2xx logout leaks the session as surely as a dead socket (V6)."""
    with _client(instance, Recorder({"/entity/auth/logout": httpx.Response(500)})):
        pass
    err = " ".join(capsys.readouterr().err.split())
    assert "! logout failed: HTTP 500" in err


def test_relogin_uses_short_logout_timeout(instance: Instance) -> None:
    recorder = Recorder({})
    with _client(instance, recorder) as client:
        n_after_enter = len(recorder.requests)
        client.relogin()
        logout = recorder.requests[n_after_enter]
        assert logout.url.path.endswith("/entity/auth/logout")
        assert logout.extensions["timeout"]["read"] == AcumaticaClient.logout_timeout


def test_relogin_dead_logout_raises_session_lost(instance: Instance) -> None:
    with (
        _client(instance, _DeadLogout({})) as client,
        pytest.raises(SessionLostError, match="re-login failed: cannot connect"),
    ):
        client.relogin()


def _flaky(statuses: list[int]) -> tuple[list[httpx.Request], AcumaticaClient]:
    """Client answering ``statuses`` in order (then 200) to non-auth calls."""
    seen: list[httpx.Request] = []
    queue = list(statuses)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(queue.pop(0) if queue else 200, json=[])

    client = AcumaticaClient(
        Instance(base_url="http://acu.test/AcumaticaERP", tenant="T1", password="pw"),
        transport=httpx.MockTransport(handler),
    )
    return seen, client


def test_get_retries_503_then_succeeds(capsys: pytest.CaptureFixture[str]) -> None:
    """Gh #52: a warmup 503 on GET is retried; the retry is logged on stderr."""
    seen, client = _flaky([503])
    assert client.get_list("Account") == []
    assert len(seen) == 2
    assert "retry 1/2: GET /entity/Default/25.200.001/Account (HTTP 503)" in (
        capsys.readouterr().err
    )


def test_get_persistent_503_fails_after_retry_tries() -> None:
    seen, client = _flaky([503, 503, 503, 503])
    with pytest.raises(RuntimeError, match="-> 503"):
        client.get_list("Account")
    assert len(seen) == AcumaticaClient.retry_tries


def test_get_retries_connect_error() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, json=[])

    client = AcumaticaClient(
        Instance(base_url="http://acu.test/AcumaticaERP", tenant="T1", password="pw"),
        transport=httpx.MockTransport(handler),
    )
    assert client.get_list("Account") == []
    assert len(calls) == 2


def test_invoke_post_is_not_retried() -> None:
    """Gh #52: an action POST may create a document - it goes out once."""
    seen, client = _flaky([503, 503])
    with pytest.raises(RuntimeError, match="-> 503"):
        client.invoke("SalesOrder", "ReleaseFromHold", {"OrderNbr": "1"})
    assert [r.method for r in seen] == ["POST"]


def test_put_retries_only_when_asked() -> None:
    """Default PUT (run document step) is single-shot; seed PUT opts in."""
    seen, client = _flaky([503])
    with pytest.raises(RuntimeError, match="-> 503"):
        client.put("SalesOrder", {"OrderType": "SO"})
    assert len(seen) == 1

    seen, client = _flaky([503])
    client.put("UnitsOfMeasure", {"UOM": "KG"}, retry=True)
    assert len(seen) == 2
