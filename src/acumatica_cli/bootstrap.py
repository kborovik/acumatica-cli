"""Bootstrap customization package: build the zip, publish via /CustomizationApi.

An unconfigured tenant cannot be configured through the Default endpoint
(features are off, no company/branch exists, credit terms have no entity —
docs/rest-api.md). The CustomizationApi is the one door that works on a
virgin tenant, so bootstrap = publish `bootstrap_project.xml` (a custom
endpoint exposing CS100000 + CS101500 + CS206500) into the session tenant,
then seed through that endpoint.

Custom endpoints are tenant-scoped rows, so the package must be published
per tenant; publish() is idempotent — an already-published package is a
no-op skip.
"""

import io
import time
import zipfile
from importlib import resources

import httpx

from .client import AcumaticaClient

PACKAGE_NAME = "acu-bootstrap"
PACKAGE_DESCRIPTION = (
    "acu bootstrap: custom endpoint for features, company, and credit terms"
)
ENDPOINT = "Bootstrap/1.0.0"


def package_zip() -> bytes:
    """Build the customization package: a zip holding project.xml."""
    xml = (resources.files("acumatica_cli") / "bootstrap_project.xml").read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.xml", xml)
    return buf.getvalue()


def _log_tail(status: dict[str, object], limit: int = 5) -> str:
    """Last few publish-log messages, for a one-line error (docs/cli.md)."""
    log = status.get("log")
    if not isinstance(log, list):
        return ""
    messages = [
        str(entry.get("message", "")) for entry in log if isinstance(entry, dict)
    ]
    return "; ".join(m for m in messages[-limit:] if m)


def publish(client: AcumaticaClient, timeout: float = 600.0, poll: float = 5.0) -> str:
    """Publish the bootstrap package into the client's session tenant.

    Idempotent: returns ``"already published"`` when getPublished lists the
    package, else import -> publishBegin -> poll publishEnd until the server
    reports completion. Transport errors while polling are tolerated —
    publishing restarts the app domain, so the site may briefly drop
    connections mid-publish. Raises RuntimeError on a failed publish or on
    timeout.
    """
    if PACKAGE_NAME in client.customization_published():
        return "already published"
    client.customization_import(
        PACKAGE_NAME, package_zip(), description=PACKAGE_DESCRIPTION
    )
    client.customization_publish_begin([PACKAGE_NAME])
    deadline = time.monotonic() + timeout
    while True:
        try:
            status = client.customization_publish_end()
        except httpx.TransportError:
            status = {}  # site restarting mid-publish; keep polling
        if status.get("isFailed"):
            detail = _log_tail(status)
            raise RuntimeError(
                f"publishing {PACKAGE_NAME} failed" + (f": {detail}" if detail else "")
            )
        if status.get("isCompleted"):
            return "published"
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"publishing {PACKAGE_NAME} did not complete within {timeout:.0f}s"
            )
        time.sleep(poll)
