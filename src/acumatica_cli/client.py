"""Contract-based REST API session (see docs/rest-api.md for verified quirks)."""

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
        creds: dict[str, str] = {
            "name": self.instance.username,
            "password": self.instance.password,
        }
        if self.instance.tenant:
            creds["tenant"] = self.instance.tenant
        self._checked(self._http.post("/entity/auth/login", json=creds))
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            # empty body sets Content-Length: 0 — IIS 411s without it
            self._http.post("/entity/auth/logout", content=b"")
        finally:
            self._http.close()

    def _url(self, entity: str) -> str:
        return f"/entity/{self.instance.endpoint}/{entity}"

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
        self, entity: str, params: dict[str, str] | None = None
    ) -> list[dict[str, Any]]:
        """GET the entity's records, optionally narrowed with OData params."""
        return self._checked(self._http.get(self._url(entity), params=params)).json()

    def swagger(self) -> bytes:
        """GET the endpoint's OpenAPI schema (swagger.json), raw bytes."""
        return self._checked(self._http.get(self._url("swagger.json"))).content

    def put(self, entity: str, record: dict[str, Any]) -> dict[str, Any]:
        """Upsert by the entity's key fields — the idempotence primitive."""
        return self._checked(
            self._http.put(self._url(entity), json=wrap(record))
        ).json()
