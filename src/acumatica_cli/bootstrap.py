"""Bootstrap customization package: build the zip, publish via /CustomizationApi.

An unconfigured tenant cannot be configured through the Default endpoint
(features are off, no company/branch exists, credit terms have no entity —
docs/rest-api.md). The CustomizationApi is the one door that works on a
virgin tenant, so bootstrap = publish a package whose CustomizationPlugin
(`bootstrap_plugin.cs`) enables features on publish — the contract API
cannot write CS100000 at all (T3 verdict) — and whose Bootstrap contract
endpoint exposes the seeding surface (serialization verified T12).

Contract ownership is hybrid (T69/V2): the data repo's
``bootstrap/project.xml`` is preferred when present (full company surface);
else the packaged minimal ``bootstrap_project.xml`` covers config-init +
the GL chain so PyPI-only and offline virgin paths still bootstrap.

Customization publishes are tenant-scoped, so the package must be published
per tenant; publish() is idempotent on content — the skip gate compares the
digest embedded in the published package's description against the package
built now, and the plugin's UpdateDatabase is a keyed update on re-run.

The feature set the plugin enables is data, not code (V2): load_features()
reads the data repo's bootstrap/features.yaml (absent -> the built-in six)
and package_zip() splices it into the plugin source at build time.
"""

import hashlib
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
PLUGIN_CLASS = "AcuBootstrapPlugin"
_ENDPOINT_NS = "{http://www.acumatica.com/entity/maintenance/5.31}"

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


def packaged_contract_xml() -> bytes:
    """The CLI-shipped minimal Bootstrap contract (config-init surface)."""
    return (resources.files("acumatica_cli") / "bootstrap_project.xml").read_bytes()


def load_contract_xml(root: Path | None = None) -> bytes:
    """Active contract bytes: data-repo override when present, else packaged.

    ``root`` is the data-repo discovery root (the dir holding ``.env``).
    Absent root or absent ``bootstrap/project.xml`` falls back to the
    packaged minimal contract (V2, same absence pattern as features.yaml).
    """
    if root is not None:
        path = root / "bootstrap" / "project.xml"
        if path.is_file():
            return path.read_bytes()
    return packaged_contract_xml()


def parse_endpoint(xml: bytes) -> tuple[str, frozenset[str]]:
    """Endpoint ``Name/version`` + entity names from a contract project.xml."""
    root = ET.fromstring(xml)
    endpoint = root.find(f"EntityEndpoint/{_ENDPOINT_NS}Endpoint")
    if endpoint is None:
        raise RuntimeError("bootstrap contract: no EntityEndpoint/Endpoint item")
    name = f"{endpoint.get('name')}/{endpoint.get('version')}"
    entities = frozenset(
        e.get("name", "") for e in endpoint.findall(f"{_ENDPOINT_NS}TopLevelEntity")
    )
    return name, entities


# Authored once on the packaged template's root description — kept for
# tests that pin a pre-digest stale description (V4 republish path).
PACKAGE_DESCRIPTION = ET.fromstring(packaged_contract_xml()).get("description", "")


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


def package_zip(
    features: Sequence[str] | None = None,
    *,
    root: Path | None = None,
    contract: bytes | None = None,
) -> bytes:
    """Build the customization package: a zip holding project.xml.

    The C# plugin travels as a <Graph> item whose Source ATTRIBUTE holds the
    file content (Customization.CstCodeFile shape, verified vs 26.101.0225:
    inline CDATA and zip-file variants are silently dropped on import).
    ElementTree escapes the newlines as &#10; on serialization.

    ``features`` (default: the built-in six) is spliced into the plugin's
    ``Enabled`` set at the ACU_FEATURES sentinel — the one point where the
    data repo's feature list enters the package (V2).

    Contract XML (V2/T69): ``contract`` when given; else
    ``bootstrap/project.xml`` under ``root`` when present; else the
    packaged minimal template.
    """
    pkg = resources.files("acumatica_cli")
    if contract is None:
        if root is None:
            from .config import find_data_root

            root = find_data_root()
        xml = load_contract_xml(root)
    else:
        xml = contract
    root_el = ET.fromstring(xml)
    source = (pkg / "bootstrap_plugin.cs").read_text(encoding="utf-8")
    if FEATURES_SENTINEL not in source:
        raise RuntimeError(f"bootstrap_plugin.cs: {FEATURES_SENTINEL} sentinel missing")
    enabled = DEFAULT_FEATURES if features is None else features
    source = source.replace(
        FEATURES_SENTINEL, ", ".join(f'"{name}"' for name in enabled)
    )
    graph = ET.SubElement(root_el, "Graph")
    graph.set("ClassName", PLUGIN_CLASS)
    graph.set("FileType", "NewFile")
    graph.set("Source", source)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("project.xml", ET.tostring(root_el, encoding="utf-8"))
    return buf.getvalue()


def content_digest(zip_bytes: bytes) -> str:
    """sha256 hex of the project.xml inside the package zip.

    Digest the XML bytes, not the zip: ET.tostring is deterministic, zip
    container bytes are not. This is the content-parity token (V4) — it
    covers everything package_zip splices in, features included.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return hashlib.sha256(zf.read("project.xml")).hexdigest()


def package_description(zip_bytes: bytes) -> str:
    """The import description: human text + the content digest.

    The description is the one round-trip channel the CustomizationApi
    offers (verified live vs 26.101.0225): the import's projectDescription
    comes back only in the root description attribute of getProject's
    re-serialized project.xml — getPublished rows hold names alone.
    Root description is read from the package's own project.xml so a
    data-repo contract carries its own prose (V2).
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        desc = ET.fromstring(zf.read("project.xml")).get("description", "")
    return f"{desc} [sha256:{content_digest(zip_bytes)}]"


def _published_description(client: AcumaticaClient) -> str | None:
    """The description embedded in the tenant's published package, if any.

    None when the project is gone (recreated tenant, B3) or its content
    does not parse as a package — both mean "republish".
    """
    content = client.customization_project_content(PACKAGE_NAME)
    if content is None:
        return None
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            root = ET.fromstring(zf.read("project.xml"))
    except zipfile.BadZipFile, KeyError, ET.ParseError:
        return None
    return root.get("description")


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
    *,
    root: Path | None = None,
    timeout: float = 600.0,
    poll: float = 5.0,
) -> str:
    """Publish the bootstrap package into the client's session tenant.

    ``features`` (default: the built-in six) flows into the plugin's Enabled
    set via package_zip. Idempotent on content, not existence (V4): the
    import embeds the digest of the package we build now in the project
    description, and the skip requires getPublished to list the package AND
    the published description to carry that same digest. A recreated tenant
    (stale publication row, content gone — B3) and a content change since
    the last publish (B7 — e.g. an edited features.yaml) both fail the gate
    and trigger reimport + republish. Otherwise import -> publishBegin ->
    poll publishEnd until the server reports completion. Transport errors
    while polling are tolerated — publishing restarts the app domain, so the
    site may briefly drop connections mid-publish. Raises RuntimeError on a
    failed publish or on timeout.
    """
    zip_bytes = package_zip(features, root=root)
    description = package_description(zip_bytes)
    if PACKAGE_NAME in client.customization_published() and (
        _published_description(client) == description
    ):
        return "already published"
    client.customization_import(PACKAGE_NAME, zip_bytes, description=description)
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
