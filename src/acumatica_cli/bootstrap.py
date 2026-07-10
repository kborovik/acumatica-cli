"""Bootstrap customization package: build the zip, publish via /CustomizationApi.

An unconfigured tenant cannot be configured through the Default endpoint
(features are off, no company/branch exists, credit terms have no entity —
docs/rest-api.md). The CustomizationApi is the one door that works on a
virgin tenant, so bootstrap = publish a package whose CustomizationPlugin
(`bootstrap_plugin.cs`) enables features on publish — the contract API
cannot write CS100000 at all (T3 verdict) — and whose Bootstrap contract
endpoint exposes CS101500 company + CS206500 credit terms for seeding
(`bootstrap_project.xml`, serialization verified T12).

Customization publishes are tenant-scoped, so the package must be published
per tenant; publish() is idempotent — an already-published package is a
no-op skip, and the plugin's UpdateDatabase is a keyed update on re-run.

The feature set the plugin enables is data, not code (V2): load_features()
reads the data repo's bootstrap/features.yaml (absent -> the built-in six)
and package_zip() splices it into the plugin source at build time.
"""

import io
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from importlib import resources
from pathlib import Path

import httpx
import yaml

from .client import AcumaticaClient

# Alphanumeric only: CstDbStorage.ValidatePackageName rejects '-' and '_'
# (verified vs 26.101.0225 — "Invalid project name")
PACKAGE_NAME = "AcuBootstrap"
PACKAGE_DESCRIPTION = (
    "acu bootstrap: CustomizationPlugin enables features on publish; "
    "Bootstrap endpoint exposes company + credit terms"
)
PLUGIN_CLASS = "AcuBootstrapPlugin"

# Code default when the data repo carries no bootstrap/features.yaml
# (SPEC I.data) — the minimum for company/branch + baseline seeding.
# The set is injected into the plugin source at package build; it never
# lives in bootstrap_plugin.cs (V2: feature flags are config, not tool
# source — B6).
DEFAULT_FEATURES = (
    "FinancialModule",
    "FinancialStandard",
    "DistributionModule",
    "Inventory",
    "Branch",
    "MultiCompany",
)
FEATURES_SENTINEL = "/*ACU_FEATURES*/"


def load_features(root: Path) -> list[str]:
    """The FeaturesSet property names from <root>/bootstrap/features.yaml.

    Absent file -> the built-in six (SPEC I.data). Names are validated as
    plausible property names here (they are spliced into C# string literals);
    whether each matches a real FeaturesSet property only the plugin can
    tell — it logs the strays at publish time (the silent-typo guard).
    """
    path = root / "bootstrap" / "features.yaml"
    if not path.is_file():
        return list(DEFAULT_FEATURES)
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list) or not data:
        raise SystemExit(
            f"{path}: expected a non-empty list of FeaturesSet property names"
        )
    for name in data:
        if not isinstance(name, str) or not (name.isascii() and name.isidentifier()):
            raise SystemExit(f"{path}: {name!r} is not a FeaturesSet property name")
    return list(data)


def package_zip(features: Sequence[str] | None = None) -> bytes:
    """Build the customization package: a zip holding project.xml.

    The C# plugin travels as a <Graph> item whose Source ATTRIBUTE holds the
    file content (Customization.CstCodeFile shape, verified vs 26.101.0225:
    inline CDATA and zip-file variants are silently dropped on import).
    ElementTree escapes the newlines as &#10; on serialization.

    ``features`` (default: the built-in six) is spliced into the plugin's
    ``Enabled`` set at the ACU_FEATURES sentinel — the one point where the
    data repo's feature list enters the package (V2).
    """
    pkg = resources.files("acumatica_cli")
    root = ET.fromstring((pkg / "bootstrap_project.xml").read_bytes())
    source = (pkg / "bootstrap_plugin.cs").read_text(encoding="utf-8")
    if FEATURES_SENTINEL not in source:
        raise RuntimeError(f"bootstrap_plugin.cs: {FEATURES_SENTINEL} sentinel missing")
    enabled = DEFAULT_FEATURES if features is None else features
    source = source.replace(
        FEATURES_SENTINEL, ", ".join(f'"{name}"' for name in enabled)
    )
    graph = ET.SubElement(root, "Graph")
    graph.set("ClassName", PLUGIN_CLASS)
    graph.set("FileType", "NewFile")
    graph.set("Source", source)
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


def publish(
    client: AcumaticaClient,
    features: Sequence[str] | None = None,
    timeout: float = 600.0,
    poll: float = 5.0,
) -> str:
    """Publish the bootstrap package into the client's session tenant.

    ``features`` (default: the built-in six) flows into the plugin's Enabled
    set via package_zip. Idempotent: returns ``"already published"`` when
    getPublished lists the package AND its content still exists (a recreated
    tenant with the same CompanyID keeps the stale publication row while the
    content and the plugin's writes are gone), else import -> publishBegin ->
    poll publishEnd until the server reports completion. Transport errors
    while polling are tolerated — publishing restarts the app domain, so the
    site may briefly drop connections mid-publish. Raises RuntimeError on a
    failed publish or on timeout.
    """
    if PACKAGE_NAME in client.customization_published() and (
        client.customization_project_exists(PACKAGE_NAME)
    ):
        return "already published"
    client.customization_import(
        PACKAGE_NAME, package_zip(features), description=PACKAGE_DESCRIPTION
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
