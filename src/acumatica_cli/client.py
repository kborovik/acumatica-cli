"""Contract-based REST API session (see docs/rest-api.md for verified quirks)."""

import base64
import html
import re
from typing import Any

import httpx

from .config import Instance


def wrap(record: dict[str, Any]) -> dict[str, Any]:
    """Plain dict -> contract-API body: {"Field": {"value": ...}}."""
    return {k: {"value": v} for k, v in record.items()}


def unwrap(entity: dict[str, Any]) -> dict[str, Any]:
    """Contract-API entity -> plain dict (top-level value fields only)."""
    return {
        k: v["value"] for k, v in entity.items() if isinstance(v, dict) and "value" in v
    }


class AcumaticaClient:
    """Cookie-session client for the contract-based endpoint.

    Use as a context manager: sessions count against the license, so logout
    must run even on failure.
    """

    def __init__(
        self,
        instance: Instance,
        timeout: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.instance = instance
        self._http = httpx.Client(
            base_url=instance.base_url, timeout=timeout, transport=transport
        )

    def __enter__(self) -> "AcumaticaClient":
        # tenant guard (V5, docs/rest-api.md): an omitted or empty tenant is
        # the one login the server still routes silently — to the default
        # tenant. Defense-in-depth vs wrong-tenant writes: every data-plane
        # session must name its tenant, so refuse before any HTTP happens.
        if not self.instance.tenant:
            raise RuntimeError(
                f"no tenant set for {self.instance.host} - a session without "
                "an explicit tenant silently lands on the default tenant; "
                "set tenant in acu.yaml or pass -t/--tenant"
            )
        creds: dict[str, str] = {
            "name": self.instance.username,
            "password": self.instance.password,
            "tenant": self.instance.tenant,
        }
        self._checked(self._http.post("/entity/auth/login", json=creds))
        # tenant guard, landed side (V5, B5): login accepting the name proves
        # nothing - a stale tenant map reroutes named logins to the default
        # tenant, and a single-tenant instance accepts ANY name (both verified
        # live, docs/rest-api.md). Verify where the session actually landed
        # and refuse on mismatch; logout first, since __exit__ never runs
        # when __enter__ raises (V6 - sessions count against the license).
        try:
            landed = self._landed_tenant()
            if landed.casefold() != self.instance.tenant.casefold():
                raise RuntimeError(
                    f"tenant guard: asked for tenant {self.instance.tenant!r} "
                    f"but the session landed on {landed!r} - the instance "
                    "tenant map is stale or the tenant does not exist; check "
                    "acu tenant list and recycle the app pool"
                )
        except BaseException:
            self.__exit__()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            # empty body sets Content-Length: 0 — IIS 411s without it
            self._http.post("/entity/auth/logout", content=b"")
        finally:
            self._http.close()

    def _landed_tenant(self) -> str:
        """The tenant this session actually landed on (login name).

        Verified vs 26.101.0225 (docs/rest-api.md): the contract API exposes
        nothing tenant-identifying, but an authenticated GET of the sign-in
        page renders a hidden ``txtSingleCompany`` input whose value is the
        session tenant's login name in every observed state - multi-tenant,
        single-tenant, and mid-reroute under a stale tenant map. The probe
        does not disturb the session.
        """
        r = self._checked(self._http.get("/Frames/Login.aspx", follow_redirects=True))
        m = re.search(r'id="txtSingleCompany" value="([^"]*)"', r.text)
        if not m:
            raise RuntimeError(
                "tenant guard: /Frames/Login.aspx did not expose the landed "
                "tenant (txtSingleCompany missing - page shape changed on "
                "this build?); refusing the session rather than risking "
                "wrong-tenant writes"
            )
        return html.unescape(m.group(1))

    def _url(self, entity: str, endpoint: str | None = None) -> str:
        return f"/entity/{endpoint or self.instance.endpoint}/{entity}"

    @staticmethod
    def _checked(r: httpx.Response) -> httpx.Response:
        """Surface Acumatica's exceptionMessage instead of a bare status code."""
        if r.is_error:
            detail = ""
            try:
                body = r.json()
                detail = body.get("exceptionMessage") or body.get("message") or ""
            except Exception:
                pass
            raise RuntimeError(
                f"{r.request.method} {r.request.url.path} -> {r.status_code}"
                + (f": {detail}" if detail else "")
            )
        return r

    def get_list(
        self,
        entity: str,
        params: dict[str, str] | None = None,
        endpoint: str | None = None,
    ) -> list[dict[str, Any]]:
        """GET the entity's records, optionally narrowed with OData params.

        endpoint overrides the instance's endpoint per call (bootstrap YAML
        targets the custom Bootstrap endpoint; everything else defaults).
        """
        return self._checked(
            self._http.get(self._url(entity, endpoint), params=params)
        ).json()

    def swagger(self) -> bytes:
        """GET the endpoint's OpenAPI schema (swagger.json), raw bytes."""
        return self._checked(self._http.get(self._url("swagger.json"))).content

    def put(
        self, entity: str, record: dict[str, Any], endpoint: str | None = None
    ) -> dict[str, Any]:
        """Upsert by the entity's key fields — the idempotence primitive."""
        return self._checked(
            self._http.put(self._url(entity, endpoint), json=wrap(record))
        ).json()

    # -- CustomizationApi (same cookie session; works on a virgin tenant) --

    @staticmethod
    def _checked_log(r: httpx.Response) -> dict[str, Any]:
        """Raise on in-band CustomizationApi errors (verified vs 26.101.0225).

        The CustomizationApi answers 200 even when an operation fails — the
        failure only shows as ``log`` entries with ``logType: "error"``
        (e.g. an import that rejects the package still returns 200).
        """
        AcumaticaClient._checked(r)
        try:
            body: dict[str, Any] = r.json()
        except ValueError:
            return {}
        log = body.get("log")
        errors = [
            str(entry.get("message", ""))
            for entry in (log if isinstance(log, list) else [])
            if isinstance(entry, dict) and entry.get("logType") == "error"
        ]
        if errors:
            raise RuntimeError(
                f"POST {r.request.url.path} reported: " + "; ".join(errors)
            )
        return body

    def customization_published(self) -> list[str]:
        """Names of the customization projects published in the session tenant."""
        body = self._checked_log(
            self._http.post("/CustomizationApi/getPublished", json={})
        )
        projects = body.get("projects") or []
        return [p["name"] for p in projects if isinstance(p, dict) and "name" in p]

    def customization_project_exists(self, name: str) -> bool:
        """True when the project's content rows exist in the session tenant.

        getPublished alone is a false idempotence proxy: a tenant deleted and
        recreated under the same CompanyID still LISTS the publication while
        the project content (and everything the publish wrote) is gone
        (verified live vs 26.101.0225).
        """
        try:
            body = self._checked_log(
                self._http.post(
                    "/CustomizationApi/getProject", json={"projectName": name}
                )
            )
        except RuntimeError:
            return False
        return bool(body.get("projectContentBase64"))

    def customization_import(
        self, name: str, zip_bytes: bytes, description: str = ""
    ) -> None:
        """Upload a customization package zip (replacing any same-name project).

        The content field is ``projectContentBase64`` — the live binder's
        property (ImportParamsData, verified vs 26.101.0225 by reflection).
        The widely documented ``projectContents`` binds nothing on this
        build: the server deletes the existing project (isReplaceIfExists)
        and then reports "The project is not found".
        """
        self._checked_log(
            self._http.post(
                "/CustomizationApi/import",
                json={
                    "projectLevel": 0,
                    "isReplaceIfExists": True,
                    "projectName": name,
                    "projectDescription": description,
                    "projectContentBase64": base64.b64encode(zip_bytes).decode("ascii"),
                },
            )
        )

    def customization_publish_begin(self, names: list[str]) -> None:
        """Start publishing the named projects into the current tenant."""
        self._checked_log(
            self._http.post(
                "/CustomizationApi/publishBegin",
                json={
                    "isMergeWithExistingPackages": False,
                    "isOnlyValidation": False,
                    "isOnlyDbUpdates": False,
                    "isReplayPreviouslyExecutedScripts": False,
                    "projectNames": names,
                    "tenantMode": "Current",
                },
            )
        )

    def customization_publish_end(self) -> dict[str, Any]:
        """Poll the running publish: {isCompleted, isFailed, log: [...]}."""
        return self._checked(
            self._http.post("/CustomizationApi/publishEnd", json={})
        ).json()
