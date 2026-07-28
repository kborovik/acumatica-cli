"""SnapshotArtifact IR: offline parse of SM203520 XML ZIP or ac.exe export xml.

Inventory is the dual-reader offline path (V35/V37): consume a tenant
snapshot *artifact* and normalize it to one intermediate representation.
No REST, no SSH, no password. Never writes ``config/`` (V35/V36); the
CLI write path (``inventory/`` summary + tables) lands in T128.

Accepted artifacts (V37):

- SM203520 Settings **XML** ZIP: ``manifest.xml`` + per-table ``*.xml``
- ``ac.exe export xml`` **folder**: one table XML file per table

Rejected: binary ``.adb`` (and ZIPs that only carry ``.adb``) — named
error, fail-closed.

Table XML shape (verified ac-exe export / dataset format, docs/ac-exe.md):

    <data>
      <table name="Account">
        <col name="AccountCD" type="NVarChar(10)"/>
        ...
      </table>
      <rows>
        <row AccountCD="10100" Description="Cash"/>
      </rows>
    </data>

Both sources normalize to :class:`SnapshotArtifact` with deterministic
table/row ordering so later emit is byte-stable.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator

from .models import Model

# Artifact kinds after normalize (export_mode summary field).
ExportMode = Literal["xml-zip", "xml-folder"]

_MANIFEST_NAMES = frozenset({"manifest.xml", "Manifest.xml", "MANIFEST.XML"})


class ColumnDef(Model):
    """One column from a table schema header (``<col name= type= .../>``)."""

    name: str
    type: str | None = None

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("column name must be non-empty")
        return v


class TableData(Model):
    """One table: ordered column schema + rows as string maps (XML attrs)."""

    name: str
    columns: list[ColumnDef] = Field(default_factory=list)
    rows: list[dict[str, str]] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("table name must be non-empty")
        return v


class SnapshotArtifact(Model):
    """Normalized IR for one snapshot artifact (V37).

    ``erp`` is the product build when the source exposes it (ZIP
    ``manifest.xml`` Version / Version-like fields); folder exports often
    omit it. ``export_mode`` records which reader path produced the IR.
    Tables are sorted by name; rows inside each table are sorted for
    determinism.
    """

    erp: str | None = None
    export_mode: ExportMode
    source: str  # path as given (for error context; not structural)
    tables: list[TableData] = Field(default_factory=list)

    @property
    def table_names(self) -> list[str]:
        """Table names in IR order (already sorted)."""
        return [t.name for t in self.tables]

    @property
    def row_counts(self) -> dict[str, int]:
        """``{table_name: row_count}`` for summary emit."""
        return {t.name: len(t.rows) for t in self.tables}


def parse_artifact(path: Path | str) -> SnapshotArtifact:
    """Parse a ZIP or folder artifact into :class:`SnapshotArtifact` (V37).

    Hard errors (``SystemExit``) for missing path, binary ``.adb``,
    unreadable ZIP, missing/invalid table XML, or empty ZIP without
    ``manifest.xml``.
    """
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"{p}: artifact not found")

    if p.is_file() and p.suffix.lower() == ".adb":
        raise SystemExit(
            f"{p}: binary .adb snapshot format is not supported "
            "(export Settings as XML ZIP on SM203520, or use ac.exe export xml)"
        )

    if p.is_file():
        return _parse_zip(p)
    if p.is_dir():
        return _parse_folder(p)
    raise SystemExit(f"{p}: artifact must be a ZIP file or a directory")


def assert_erp_matches(artifact: SnapshotArtifact, erp: str) -> None:
    """When both sides know a build, require equality (V37 sibling of V27).

    No-op when the artifact has no ``erp`` (folder exports without a
    header) or when ``erp`` is empty. Mismatch → ``SystemExit``.
    """
    want = erp.strip()
    if not want or artifact.erp is None:
        return
    got = artifact.erp.strip()
    if got != want:
        raise SystemExit(
            f"snapshot erp/build {got!r} does not match target.yaml erp {want!r} "
            f"(source: {artifact.source})"
        )


# ---------------------------------------------------------------------------
# ZIP (SM203520 XML)
# ---------------------------------------------------------------------------


def _parse_zip(path: Path) -> SnapshotArtifact:
    """SM203520 Settings XML ZIP → IR; reject .adb-bearing binary zips."""
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise SystemExit(f"{path}: not a valid ZIP ({exc})") from exc

    with zf:
        names = zf.namelist()
        basenames = {_zip_basename(n) for n in names}
        if any(b.lower().endswith(".adb") for b in basenames):
            raise SystemExit(
                f"{path}: binary .adb entries inside ZIP are not supported "
                "(export Settings as XML on SM203520)"
            )
        manifest_member = _find_manifest_member(names)
        if manifest_member is None:
            raise SystemExit(
                f"{path}: SM203520 XML ZIP requires manifest.xml "
                "(no manifest; not an ac.exe export folder)"
            )
        try:
            manifest_bytes = zf.read(manifest_member)
        except KeyError as exc:
            raise SystemExit(f"{path}: cannot read {manifest_member}") from exc
        erp, export_label = _parse_manifest(manifest_bytes, source=str(path))

        tables: list[TableData] = []
        for member in sorted(names, key=lambda n: _zip_basename(n).lower()):
            base = _zip_basename(member)
            if base in _MANIFEST_NAMES or not base.lower().endswith(".xml"):
                continue
            if member.endswith("/"):
                continue
            try:
                raw = zf.read(member)
            except KeyError:
                continue
            stem = Path(base).stem
            tables.append(
                _parse_table_xml(raw, name_hint=stem, source=f"{path}:{member}")
            )

    return _finalize(
        tables,
        erp=erp,
        export_mode="xml-zip",
        source=str(path),
        export_label=export_label,
    )


def _zip_basename(member: str) -> str:
    """Last path segment of a ZIP member (handles nested dirs)."""
    return member.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _find_manifest_member(names: list[str]) -> str | None:
    """Prefer root-level manifest.xml; else any path ending in that name."""
    for n in names:
        if _zip_basename(n) in _MANIFEST_NAMES and "/" not in n.replace(
            "\\", "/"
        ).rstrip("/"):
            return n
    for n in names:
        if _zip_basename(n) in _MANIFEST_NAMES:
            return n
    return None


_ERP_KEYS = ("Version", "version", "Erp", "erp", "Build", "build", "ProductVersion")
_MODE_KEYS = ("ExportMode", "exportMode", "export_mode", "Mode", "mode")


def _parse_manifest(raw: bytes, *, source: str) -> tuple[str | None, str | None]:
    """Extract (erp/version, export_mode label) from manifest.xml.

    Accepts element text, attributes, or ``UPSnapshot``/``Snapshot`` row
    attrs. Unknown shape → ``(None, None)`` (tables still parse).
    """
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SystemExit(
            f"{source}: manifest.xml is not well-formed XML: {exc}"
        ) from exc

    erp = _pick_field(root, _ERP_KEYS)
    mode = _pick_field(root, _MODE_KEYS)
    return erp, mode


def _pick_field(root: ET.Element, keys: tuple[str, ...]) -> str | None:
    """First non-empty value for ``keys`` via text, attrs, or snapshot rows."""
    found = _first_text(root, keys) or _first_attr(root, keys)
    if found is not None:
        return found
    for tag in ("UPSnapshot", "Snapshot", "snapshot"):
        for el in root.iter(tag):
            found = _attr_from(el, keys)
            if found is not None:
                return found
            for row in el.findall("row"):
                found = _attr_from(row, keys)
                if found is not None:
                    return found
    return None


def _attr_from(el: ET.Element, keys: tuple[str, ...]) -> str | None:
    for name in keys:
        val = el.get(name)
        if val is not None and val.strip():
            return val.strip()
    return None


def _first_text(root: ET.Element, names: tuple[str, ...]) -> str | None:
    for name in names:
        for el in root.iter(name):
            if el.text and el.text.strip():
                return el.text.strip()
    return None


def _first_attr(root: ET.Element, names: tuple[str, ...]) -> str | None:
    found = _attr_from(root, names)
    if found is not None:
        return found
    for el in root.iter():
        found = _attr_from(el, names)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Folder (ac.exe export xml)
# ---------------------------------------------------------------------------


def _parse_folder(path: Path) -> SnapshotArtifact:
    """``ac.exe export xml`` directory: each ``*.xml`` is one table."""
    adb = sorted(path.glob("**/*.adb"))
    if adb:
        raise SystemExit(
            f"{path}: binary .adb files are not supported "
            f"(found {adb[0].name}; use export xml, not export adb)"
        )

    xml_files = sorted(
        (
            p
            for p in path.rglob("*.xml")
            if p.is_file() and p.name not in _MANIFEST_NAMES
        ),
        key=lambda p: p.name.lower(),
    )
    # Prefer top-level only when present (export dumps flat); else recursive.
    top = sorted(
        (
            p
            for p in path.glob("*.xml")
            if p.is_file() and p.name not in _MANIFEST_NAMES
        ),
        key=lambda p: p.name.lower(),
    )
    files = top if top else xml_files
    if not files:
        raise SystemExit(
            f"{path}: no table XML files (expected ac.exe export xml folder with *.xml)"
        )

    erp: str | None = None
    export_label: str | None = None
    manifest = next((path / n for n in _MANIFEST_NAMES if (path / n).is_file()), None)
    if manifest is not None:
        erp, export_label = _parse_manifest(manifest.read_bytes(), source=str(manifest))

    tables: list[TableData] = []
    for f in files:
        tables.append(_parse_table_xml(f.read_bytes(), name_hint=f.stem, source=str(f)))

    return _finalize(
        tables,
        erp=erp,
        export_mode="xml-folder",
        source=str(path),
        export_label=export_label,
    )


# ---------------------------------------------------------------------------
# Shared table XML + finalize
# ---------------------------------------------------------------------------


def _parse_table_xml(raw: bytes, *, name_hint: str, source: str) -> TableData:
    """Parse one ``<data><table/><rows/></data>`` document into TableData."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise SystemExit(f"{source}: not well-formed XML: {exc}") from exc

    tag = _local(root.tag)
    if tag != "data":
        raise SystemExit(
            f"{source}: expected root <data> (ac.exe / SM203520 table XML), got <{tag}>"
        )

    table_el = _child(root, "table")
    if table_el is None:
        raise SystemExit(f"{source}: missing <table> schema header")

    name = (table_el.get("name") or table_el.get("Name") or name_hint or "").strip()
    if not name:
        raise SystemExit(f"{source}: table has no name attribute and no filename hint")

    columns = _parse_columns(table_el, source=source)
    rows_el = _child(root, "rows")
    rows = _parse_row_elements(rows_el) if rows_el is not None else []
    return TableData(name=name, columns=columns, rows=_sort_rows(rows, columns))


def _child(parent: ET.Element, local_name: str) -> ET.Element | None:
    for child in parent:
        if _local(child.tag) == local_name:
            return child
    return None


def _parse_columns(table_el: ET.Element, *, source: str) -> list[ColumnDef]:
    columns: list[ColumnDef] = []
    seen: set[str] = set()
    for col in table_el:
        if _local(col.tag) != "col":
            continue
        cname = (col.get("name") or col.get("Name") or "").strip()
        if not cname:
            raise SystemExit(f"{source}: <col> missing name attribute")
        if cname in seen:
            raise SystemExit(f"{source}: duplicate column {cname!r}")
        seen.add(cname)
        columns.append(ColumnDef(name=cname, type=col.get("type") or col.get("Type")))
    return columns


def _parse_row_elements(rows_el: ET.Element) -> list[dict[str, str]]:
    """Attribute maps from each ``<row .../>``; string values as in XML."""
    rows: list[dict[str, str]] = []
    for row_el in rows_el:
        if _local(row_el.tag) != "row":
            continue
        rows.append(dict(row_el.attrib))
    return rows


def _sort_rows(
    rows: list[dict[str, str]], columns: list[ColumnDef]
) -> list[dict[str, str]]:
    """Stable deterministic row order: column-order key, then full item tuple."""
    col_names = [c.name for c in columns]

    def key(row: dict[str, str]) -> tuple[Any, ...]:
        primary = tuple(row.get(c, "") for c in col_names)
        secondary = tuple(sorted(row.items()))
        return (primary, secondary)

    return sorted(rows, key=key)


def _finalize(
    tables: list[TableData],
    *,
    erp: str | None,
    export_mode: ExportMode,
    source: str,
    export_label: str | None,
) -> SnapshotArtifact:
    """Sort tables by name; drop empty-name; attach summary fields."""
    del export_label  # reserved for T128 summary emit (surface via erp/mode)
    by_name: dict[str, TableData] = {}
    for t in tables:
        if t.name in by_name:
            raise SystemExit(f"{source}: duplicate table {t.name!r}")
        by_name[t.name] = t
    ordered = [by_name[k] for k in sorted(by_name.keys(), key=str.lower)]
    return SnapshotArtifact(
        erp=erp,
        export_mode=export_mode,
        source=source,
        tables=ordered,
    )


def _local(tag: str) -> str:
    """Strip Clark notation ``{ns}local`` if present."""
    if tag.startswith("{"):
        return tag.rsplit("}", 1)[-1]
    return tag
