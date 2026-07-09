"""Bootstrap customization package: build the zip, publish via /CustomizationApi.

An unconfigured tenant cannot be configured through the Default endpoint
(features are off, no company/branch exists, credit terms have no entity —
docs/rest-api.md). The CustomizationApi is the one door that works on a
virgin tenant, so bootstrap = publish a package whose CustomizationPlugin
(`bootstrap_plugin.cs`) enables features on publish — the contract API
cannot write CS100000 at all (T3 verdict).

Customization publishes are tenant-scoped, so the package must be published
per tenant; publish() is idempotent — an already-published package is a
no-op skip, and the plugin's UpdateDatabase is a keyed update on re-run.
"""

import io
import time
import xml.etree.ElementTree as ET
import zipfile
from importlib import resources

import httpx

from .client import AcumaticaClient

# Alphanumeric only: CstDbStorage.ValidatePackageName rejects '-' and '_'
# (verified vs 26.101.0225 — "Invalid project name")
PACKAGE_NAME = "AcuBootstrap"
PACKAGE_DESCRIPTION = "acu bootstrap: CustomizationPlugin enables features on publish"
PLUGIN_CLASS = "AcuBootstrapPlugin"


def package_zip() -> bytes:
    """Build the customization package: a zip holding project.xml.

    The C# plugin travels as a <Graph> item whose Source ATTRIBUTE holds the
    file content (Customization.CstCodeFile shape, verified vs 26.101.0225:
    inline CDATA and zip-file variants are silently dropped on import).
    ElementTree escapes the newlines as &#10; on serialization.
    """
    pkg = resources.files("acumatica_cli")
    root = ET.fromstring((pkg / "bootstrap_project.xml").read_bytes())
    graph = ET.SubElement(root, "Graph")
    graph.set("ClassName", PLUGIN_CLASS)
    graph.set("FileType", "NewFile")
    graph.set("Source", (pkg / "bootstrap_plugin.cs").read_text(encoding="utf-8"))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.xml", ET.tostring(root, encoding="utf-8"))
    return buf.getvalue()


def _log_tail(status: dict[str, object], limit: int = 5) -> str:
    """Last few publish-log messages, for a one-line error (SPEC V9)."""
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
    package AND its content still exists (a recreated tenant with the same
    CompanyID keeps the stale publication row while the content and the
    plugin's writes are gone), else import -> publishBegin -> poll publishEnd
    until the server reports completion. Transport errors while polling are
    tolerated — publishing restarts the app domain, so the site may briefly
    drop connections mid-publish. Raises RuntimeError on a failed publish or
    on timeout.
    """
    if PACKAGE_NAME in client.customization_published() and (
        client.customization_project_exists(PACKAGE_NAME)
    ):
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
