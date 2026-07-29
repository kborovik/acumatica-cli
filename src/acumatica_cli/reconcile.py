"""Offline reconcile: inventory/ + optional config/ → findings/ (V35/V36).

Consumes disk trees only (no REST, no SSH, no password). Never writes
``config/`` — findings are the sole emit (V35/V36). Conflicts and gaps
land in ``findings/`` files; exit never 2 (diff owns drift).

Finding kinds (I.data findings/):

- **unmapped** — inventory tables with no catalog/map entity (configured
  not-CaC surface; never promote into seed)
- **rest_gaps** — catalog entity has inventory rows but seed file absent
  or whole ``config/`` missing
- **deltas** — both sides present: key/field value conflicts (REST wins
  when extract exists; report only — V36)
- **custom_columns** — inventory columns outside seed/catalog surface
  (``Usr*`` always; other attrs not on seed records / keys / include)

Optional ``snapshot_map.yaml`` (package data or data-repo root) maps
table → entity; absent → identity match on catalog entity name.

Compare path pad-trims string keys and fields on both sides (V38) so
fixed-width / trailing-space inventory values join and equal seed.
Per-row optional ``keys:`` / ``fields:`` maps rename seed names →
inventory column names (V38 M3) so e.g. SubaccountCD joins SubCD and
UnitID joins Unit without false key-skips. Global ``resolvers:`` plus
per-row ``resolves:`` map inv int FKs → CD strings before compare (V38 M4)
so seed CD space matches inventory IDs (Account/Sub first). Global
``enums:`` plus per-row ``enums:`` map REST labels → DAC codes (V38 M5)
so e.g. Usage Adjustment vs A and Active true vs 1 do not false-delta.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator

from . import extract, inventory, output
from .config import find_data_root
from .models import Model, validation_summary

DEFAULT_INVENTORY = "inventory"
DEFAULT_CONFIG = "config"
DEFAULT_OUT = "findings"
SUMMARY_NAME = "summary.yaml"
SEED_DIRS = ("bootstrap", "baseline", "setup", "master")
_MAP_NAME = "snapshot_map.yaml"
_SKIP_SEED_NAMES = frozenset({"features.yaml", "project.xml"})

# Findings file names under --out (deterministic set).
FINDING_FILES = (
    SUMMARY_NAME,
    "unmapped.yaml",
    "rest_gaps.yaml",
    "deltas.yaml",
    "custom_columns.yaml",
)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class InventorySummary(Model):
    """Parsed ``inventory/summary.yaml`` header fields we care about."""

    erp: str | None = None
    export_mode: str | None = None
    tables: dict[str, int] = Field(default_factory=dict)


class InventoryTree(Model):
    """Loaded inventory tree: summary + table IR (same shape as emit)."""

    root: Path
    summary: InventorySummary
    tables: list[inventory.TableData] = Field(default_factory=list)

    @property
    def by_name(self) -> dict[str, inventory.TableData]:
        """Table name → TableData (last wins if dups; emit forbids dups)."""
        return {t.name: t for t in self.tables}


class SeedFile(Model):
    """One baseline seed on disk (action files skipped for field compare)."""

    path: Path
    entity: str
    keys: list[str] = Field(min_length=1)
    records: list[dict[str, Any]] = Field(default_factory=list)
    rel: str  # path relative to config parent or as given

    @field_validator("keys", mode="before")
    @classmethod
    def _key_as_list(cls, v: object) -> object:
        return v if isinstance(v, list) else [v]


class FkResolver(Model):
    """Global inventory FK index: int id column → natural CD column (V38 M4)."""

    table: str
    id: str
    cd: str

    @field_validator("table", "id", "cd")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("resolver table/id/cd must be non-empty")
        return v


class TableMapEntry(Model):
    """One optional snapshot_map row: DAC table → catalog entity.

    Optional ``keys`` / ``fields`` are seed-name → inventory-column aliases
    (V38 M3). Optional ``resolves`` maps seed field → global resolver name
    (V38 M4): inventory value is looked up id→CD before compare. Optional
    ``enums`` maps seed field → global enum table name (V38 M5): seed labels
    fold to DAC codes before compare. Absent aliases → identity names. v1
    rows are ``{table, entity}`` only.
    """

    table: str
    entity: str
    keys: dict[str, str] = Field(default_factory=dict)
    fields: dict[str, str] = Field(default_factory=dict)
    resolves: dict[str, str] = Field(default_factory=dict)
    enums: dict[str, str] = Field(default_factory=dict)

    @field_validator("table", "entity")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("table/entity must be non-empty")
        return v

    @field_validator("keys", "fields", "resolves", "enums", mode="before")
    @classmethod
    def _str_map(cls, v: object) -> object:
        """Accept seed→inv/resolver/enum-name string maps; reject empty names."""
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("keys/fields/resolves/enums must be a mapping of strings")
        out: dict[str, str] = {}
        for seed_name, target in v.items():
            s = str(seed_name).strip()
            if isinstance(target, dict):
                raise ValueError(
                    f"keys/fields/resolves/enums[{s!r}]: expected string value "
                    "(use resolves: for FK names; enums: for label→code tables)"
                )
            i = str(target).strip()
            if not s or not i:
                raise ValueError("keys/fields/resolves/enums names must be non-empty")
            out[s] = i
        return out


class SnapshotMap(Model):
    """Optional table→entity map (package or data-repo ``snapshot_map.yaml``)."""

    resolvers: dict[str, FkResolver] = Field(default_factory=dict)
    enums: dict[str, dict[str, str]] = Field(default_factory=dict)
    tables: list[TableMapEntry] = Field(default_factory=list)

    @field_validator("resolvers", mode="before")
    @classmethod
    def _resolvers_map(cls, v: object) -> object:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("resolvers must be a mapping of name -> {table,id,cd}")
        return v

    @field_validator("enums", mode="before")
    @classmethod
    def _enums_map(cls, v: object) -> object:
        """Global name → {label: code}; stringify YAML bool keys as true/false."""
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("enums must be a mapping of name -> {label: code}")
        out: dict[str, dict[str, str]] = {}
        for name, mapping in v.items():
            n = str(name).strip()
            if not n:
                raise ValueError("enums names must be non-empty")
            if not isinstance(mapping, dict):
                raise ValueError(f"enums[{n!r}]: expected mapping of label -> code")
            inner: dict[str, str] = {}
            for label, code in mapping.items():
                if isinstance(label, bool):
                    lab = "true" if label else "false"
                else:
                    lab = str(label).strip()
                if isinstance(code, bool):
                    cd = "true" if code else "false"
                else:
                    cd = str(code).strip()
                if not lab or not cd:
                    raise ValueError(
                        f"enums[{n!r}]: label/code must be non-empty strings"
                    )
                inner[lab] = cd
            out[n] = inner
        return out

    def entity_for(self, table: str) -> str | None:
        """Catalog entity name for a DAC table, or None when unmapped."""
        entry = self.entry_for(table)
        return entry.entity if entry is not None else None

    def entry_for(self, table: str) -> TableMapEntry | None:
        """Full map row for a DAC table (aliases + entity), or None."""
        for row in self.tables:
            if row.table == table:
                return row
        return None


class UnmappedTable(Model):
    name: str
    rows: int
    columns: list[str] = Field(default_factory=list)


class RestGap(Model):
    entity: str
    table: str
    file: str
    reason: str


class FieldDelta(Model):
    entity: str
    table: str
    file: str
    key: list[str]
    field: str
    seed: str
    inventory: str


class CustomColumns(Model):
    name: str  # table
    entity: str | None = None
    columns: list[str] = Field(default_factory=list)


class FindingsBundle(Model):
    """In-memory findings before emit (V35/V36 — never seed shape)."""

    inventory: str
    config: str | None
    erp: str | None = None
    export_mode: str | None = None
    table_count: int = 0
    unmapped: list[UnmappedTable] = Field(default_factory=list)
    rest_gaps: list[RestGap] = Field(default_factory=list)
    deltas: list[FieldDelta] = Field(default_factory=list)
    custom_columns: list[CustomColumns] = Field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        """Per-kind finding counts for summary.yaml."""
        return {
            "unmapped": len(self.unmapped),
            "rest_gaps": len(self.rest_gaps),
            "deltas": len(self.deltas),
            "custom_columns": sum(len(c.columns) for c in self.custom_columns),
        }


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def load_inventory_tree(path: Path | str) -> InventoryTree:
    """Load an ``inventory/`` tree written by ``acu inventory`` (I.data)."""
    root = Path(path)
    if not root.is_dir():
        raise SystemExit(f"{root}: inventory directory not found")
    summary_path = root / inventory.SUMMARY_NAME
    if not summary_path.is_file():
        raise SystemExit(
            f"{root}: missing {inventory.SUMMARY_NAME} "
            "(run `acu inventory` first, or pass --inventory DIR)"
        )
    try:
        raw = yaml.safe_load(summary_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"{summary_path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{summary_path}: expected a mapping")
    try:
        summary = InventorySummary.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"{summary_path}: {validation_summary(exc)}") from exc

    tables_dir = root / inventory.TABLES_DIR
    tables: list[inventory.TableData] = []
    if tables_dir.is_dir():
        for f in sorted(tables_dir.glob("*.yaml"), key=lambda p: p.name.lower()):
            tables.append(_load_table_yaml(f))
    return InventoryTree(root=root, summary=summary, tables=tables)


def _load_table_yaml(path: Path) -> inventory.TableData:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping")
    try:
        return inventory.TableData.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def load_config_seeds(config_dir: Path | str | None) -> list[SeedFile]:
    """Load baseline seed files under ``config/{SEED_DIRS}/`` (optional).

    Action files (``action:``) and package-only names are skipped — reconcile
    field compare needs keyed records. Missing dir → empty list (caller
    decides rest_gaps vs inventory-only).
    """
    if config_dir is None:
        return []
    root = Path(config_dir)
    if not root.is_dir():
        return []
    seeds: list[SeedFile] = []
    for name in SEED_DIRS:
        d = root / name
        if not d.is_dir():
            continue
        for path in sorted(d.glob("*.yaml"), key=lambda p: p.name.lower()):
            if path.name in _SKIP_SEED_NAMES:
                continue
            loaded = _try_load_seed(path, config_root=root)
            if loaded is not None:
                seeds.append(loaded)
    return seeds


def _try_load_seed(path: Path, *, config_root: Path) -> SeedFile | None:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping")
    if "action" in data:
        return None  # setup action files — not record dumps
    if "entity" not in data or "key" not in data:
        return None
    # Prefer light parse over seed.load_baseline: dual-serve bootstrap
    # resolution is apply/diff concern; reconcile only needs entity/keys/records.
    try:
        rel = str(path.relative_to(config_root.parent))
    except ValueError:
        rel = str(path)
    # Normalize to config/... when path is under config/
    if path.parts:
        try:
            idx = path.parts.index(config_root.name)
            rel = "/".join(path.parts[idx:])
        except ValueError:
            pass
    try:
        return SeedFile.model_validate(
            {
                "path": path,
                "entity": data["entity"],
                "keys": data["key"],
                "records": data.get("records") or [],
                "rel": rel.replace("\\", "/"),
            }
        )
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def load_snapshot_map(root: Path | None = None) -> SnapshotMap:
    """Optional table→entity map: data-repo file wins, else package data.

    Absent → empty map (identity match on catalog entity names).
    """
    candidates: list[Path] = []
    if root is not None:
        candidates.append(Path(root) / _MAP_NAME)
    data = find_data_root()
    if data is not None:
        candidates.append(data / _MAP_NAME)
    # de-dupe while preserving order
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        try:
            key = path.resolve()
        except OSError:
            key = path
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    for path in ordered:
        if path.is_file():
            return _parse_snapshot_map(
                path.read_text(encoding="utf-8"), source=str(path)
            )
    try:
        pkg = resources.files("acumatica_cli") / _MAP_NAME
        if pkg.is_file():
            return _parse_snapshot_map(
                pkg.read_text(encoding="utf-8"), source=_MAP_NAME
            )
    except FileNotFoundError, TypeError, OSError:
        pass
    return SnapshotMap()


def _parse_snapshot_map(text: str, *, source: str) -> SnapshotMap:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SystemExit(f"{source}: invalid YAML: {exc}") from exc
    if raw is None:
        return SnapshotMap()
    if not isinstance(raw, dict):
        raise SystemExit(f"{source}: expected a mapping with 'tables:'")
    try:
        return SnapshotMap.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"{source}: {validation_summary(exc)}") from exc


# ---------------------------------------------------------------------------
# Reconcile
# ---------------------------------------------------------------------------


def reconcile(
    inv: InventoryTree,
    seeds: list[SeedFile],
    *,
    config_dir: Path | None,
    snapshot_map: SnapshotMap | None = None,
    manifest: extract.Manifest | None = None,
) -> FindingsBundle:
    """Compare inventory tree to optional config seeds; return FindingsBundle.

    Never mutates ``config/`` (V35/V36). Mapping: optional snapshot_map, else
    identity table name == catalog entity name.
    """
    m = manifest if manifest is not None else extract.load_manifest()
    smap = snapshot_map if snapshot_map is not None else SnapshotMap()
    by_entity = _catalog_by_entity(m)
    catalog_entities = set(by_entity)
    seed_index = _index_seeds(seeds)
    fk_indexes = _build_fk_indexes(inv, smap.resolvers)
    enum_tables = smap.enums
    config_present = config_dir is not None and config_dir.is_dir()

    unmapped: list[UnmappedTable] = []
    rest_gaps: list[RestGap] = []
    deltas: list[FieldDelta] = []
    custom: list[CustomColumns] = []

    for table in inv.tables:
        entity = _resolve_entity(table.name, smap, catalog_entities)
        col_names = [c.name for c in table.columns]
        if entity is None or entity not in by_entity:
            unmapped.append(
                UnmappedTable(name=table.name, rows=len(table.rows), columns=col_names)
            )
            _append_usr_custom(custom, table.name, None, col_names)
            continue
        specs = by_entity[entity]
        rest_gaps.extend(
            _rest_gaps_for_table(
                entity=entity,
                table=table,
                specs=specs,
                config_present=config_present,
                seeds=seed_index,
            )
        )
        map_entry = smap.entry_for(table.name)
        deltas.extend(
            _deltas_for_table(
                entity=entity,
                table=table,
                specs=specs,
                config_present=config_present,
                seeds=seed_index,
                map_entry=map_entry,
                fk_indexes=fk_indexes,
                enum_tables=enum_tables,
            )
        )
        _append_usr_custom(custom, table.name, entity, col_names)

    unmapped.sort(key=lambda u: u.name.lower())
    rest_gaps.sort(key=lambda g: (g.entity.lower(), g.file))
    deltas.sort(key=lambda d: (d.entity.lower(), d.file, tuple(d.key), d.field.lower()))
    custom.sort(key=lambda c: c.name.lower())

    return FindingsBundle(
        inventory=str(inv.root),
        config=str(config_dir) if config_present else None,
        erp=inv.summary.erp,
        export_mode=inv.summary.export_mode,
        table_count=len(inv.tables),
        unmapped=unmapped,
        rest_gaps=rest_gaps,
        deltas=deltas,
        custom_columns=custom,
    )


def _catalog_by_entity(
    manifest: extract.Manifest,
) -> dict[str, list[extract.EntitySpec]]:
    by_entity: dict[str, list[extract.EntitySpec]] = {}
    for spec in manifest.entities:
        by_entity.setdefault(spec.entity, []).append(spec)
    return by_entity


class _SeedIndex(Model):
    """In-memory seed indexes for path and entity lookup."""

    by_entity: dict[str, list[SeedFile]] = Field(default_factory=dict)
    by_file: dict[str, SeedFile] = Field(default_factory=dict)


def _index_seeds(seeds: list[SeedFile]) -> _SeedIndex:
    by_entity: dict[str, list[SeedFile]] = {}
    by_file: dict[str, SeedFile] = {}
    for s in seeds:
        by_entity.setdefault(s.entity, []).append(s)
        by_file[s.rel.replace("\\", "/")] = s
    return _SeedIndex(by_entity=by_entity, by_file=by_file)


def _resolve_entity(
    table: str, smap: SnapshotMap, catalog_entities: set[str]
) -> str | None:
    mapped = smap.entity_for(table)
    if mapped is not None:
        return mapped
    if table in catalog_entities:
        return table
    return None


def _rest_gaps_for_table(
    *,
    entity: str,
    table: inventory.TableData,
    specs: list[extract.EntitySpec],
    config_present: bool,
    seeds: _SeedIndex,
) -> list[RestGap]:
    gaps: list[RestGap] = []
    for spec in specs:
        seed_file = _find_seed_for_spec(spec, seeds)
        if not config_present:
            gaps.append(
                RestGap(
                    entity=entity,
                    table=table.name,
                    file=spec.file,
                    reason="config absent",
                )
            )
        elif seed_file is None:
            gaps.append(
                RestGap(
                    entity=entity,
                    table=table.name,
                    file=spec.file,
                    reason="seed file missing",
                )
            )
    return gaps


def _deltas_for_table(  # noqa: PLR0913 — keyword-only; map + V38 indexes
    *,
    entity: str,
    table: inventory.TableData,
    specs: list[extract.EntitySpec],
    config_present: bool,
    seeds: _SeedIndex,
    map_entry: TableMapEntry | None = None,
    fk_indexes: dict[str, dict[str, str]] | None = None,
    enum_tables: dict[str, dict[str, str]] | None = None,
) -> list[FieldDelta]:
    """Per-spec field deltas for one inventory table (optional map aliases)."""
    if not config_present:
        return []
    indexes = fk_indexes if fk_indexes is not None else {}
    enums = enum_tables if enum_tables is not None else {}
    out: list[FieldDelta] = []
    for spec in specs:
        seed_file = _find_seed_for_spec(spec, seeds)
        if seed_file is None:
            continue
        out.extend(
            _compare_seed_table(
                entity=entity,
                table=table,
                seed_file=seed_file,
                spec=spec,
                map_entry=map_entry,
                fk_indexes=indexes,
                enum_tables=enums,
            )
        )
    return out


def _append_usr_custom(
    custom: list[CustomColumns],
    table: str,
    entity: str | None,
    col_names: list[str],
) -> None:
    """Record Usr* customization columns on a table (Acumatica convention)."""
    usr = sorted(c for c in col_names if c.startswith("Usr"))
    if usr:
        custom.append(CustomColumns(name=table, entity=entity, columns=usr))


def _find_seed_for_spec(spec: extract.EntitySpec, seeds: _SeedIndex) -> SeedFile | None:
    """Match catalog file path to a loaded seed (path or entity fallback)."""
    want = spec.file.replace("\\", "/")
    if want in seeds.by_file:
        return seeds.by_file[want]
    base = Path(want).name
    for rel, s in seeds.by_file.items():
        if Path(rel).name == base and s.entity == spec.entity:
            return s
    candidates = seeds.by_entity.get(spec.entity, [])
    if len(candidates) == 1:
        return candidates[0]
    for s in candidates:
        if Path(s.rel).name == base:
            return s
    return None


def _build_fk_indexes(
    inv: InventoryTree, resolvers: dict[str, FkResolver]
) -> dict[str, dict[str, str]]:
    """Build resolver_name → {id_norm: cd_norm} from inventory tables (V38 M4)."""
    indexes: dict[str, dict[str, str]] = {}
    by_name = inv.by_name
    for name, spec in resolvers.items():
        table = by_name.get(spec.table)
        if table is None:
            indexes[name] = {}
            continue
        idx: dict[str, str] = {}
        for row in table.rows:
            if spec.id not in row or spec.cd not in row:
                continue
            idx[_norm(row[spec.id])] = _norm(row[spec.cd])
        indexes[name] = idx
    return indexes


def _resolve_inv_value(
    raw: str,
    *,
    field: str,
    resolves: dict[str, str],
    fk_indexes: dict[str, dict[str, str]],
) -> str:
    """Map inventory FK id → CD when field has a resolver; else return raw."""
    resolver_name = resolves.get(field)
    if resolver_name is None:
        return raw
    id_to_cd = fk_indexes.get(resolver_name)
    if not id_to_cd:
        return raw
    return id_to_cd.get(raw, raw)


def _enum_norm(
    raw: str,
    *,
    field: str,
    field_enums: dict[str, str],
    enum_tables: dict[str, dict[str, str]],
) -> str:
    """Map REST label → DAC code when field has an enum table (V38 M5).

    Seed side usually carries labels (Adjustment, Asset, true); inventory
    stores codes (A, A, 1). Labels in the table fold to codes; values that
    already equal a code (or are unmapped) pass through unchanged.
    """
    enum_name = field_enums.get(field)
    if enum_name is None:
        return raw
    table = enum_tables.get(enum_name)
    if not table:
        return raw
    if raw in table:
        return table[raw]
    # case-insensitive label match (seed YAML sometimes differs in case)
    lower = raw.lower()
    for lab, code in table.items():
        if lab.lower() == lower:
            return code
    return raw


def _compare_seed_table(  # noqa: PLR0913 — keyword-only compare bundle
    *,
    entity: str,
    table: inventory.TableData,
    seed_file: SeedFile,
    spec: extract.EntitySpec,
    map_entry: TableMapEntry | None = None,
    fk_indexes: dict[str, dict[str, str]] | None = None,
    enum_tables: dict[str, dict[str, str]] | None = None,
) -> list[FieldDelta]:
    """Field-level compare for rows that share a key tuple on both sides.

    Key fields come from the seed file (authoritative for CaC). Inventory
    column names often match contract field names; missing inventory key
    → skip that seed row (no false delta). Optional map ``keys`` / ``fields``
    rename seed names → inv columns (V38 M3). Optional ``resolves`` + global
    FK indexes convert inv int IDs → CD before compare (V38 M4; prefer seed
    CD space). Optional ``enums`` + global enum tables fold seed labels →
    codes (V38 M5). Join keys and field values are pad-trimmed on both sides
    (V38) so fixed-width / trailing-space DAC columns match seed natural keys.
    Detail lists skipped (not comparable to flat snapshot rows). Findings
    always report seed-side field names and resolved inventory values.
    """
    keys = seed_file.keys
    key_alias = map_entry.keys if map_entry is not None else {}
    field_alias = map_entry.fields if map_entry is not None else {}
    resolves = map_entry.resolves if map_entry is not None else {}
    field_enums = map_entry.enums if map_entry is not None else {}
    indexes = fk_indexes if fk_indexes is not None else {}
    enums = enum_tables if enum_tables is not None else {}
    inv_keys = [key_alias.get(k, k) for k in keys]
    inv_index = _index_inventory_rows(table.rows, inv_keys)
    deltas: list[FieldDelta] = []
    for rec in seed_file.records:
        if not all(k in rec for k in keys):
            continue
        # V38: pad-trim key identity both sides so join is padding-invariant.
        # Seed values are looked up under inv column names (alias or identity).
        ident = tuple(_norm(rec[k]) for k in keys)
        inv_row = inv_index.get(ident)
        if inv_row is None:
            continue
        for field, seed_val in sorted(rec.items()):
            if field in keys or isinstance(seed_val, (list, dict)):
                continue
            inv_field = field_alias.get(field, field)
            if inv_field not in inv_row:
                continue
            left = _enum_norm(
                _norm(seed_val),
                field=field,
                field_enums=field_enums,
                enum_tables=enums,
            )
            right = _enum_norm(
                _resolve_inv_value(
                    _norm(inv_row[inv_field]),
                    field=field,
                    resolves=resolves,
                    fk_indexes=indexes,
                ),
                field=field,
                field_enums=field_enums,
                enum_tables=enums,
            )
            if left != right:
                deltas.append(
                    FieldDelta(
                        entity=entity,
                        table=table.name,
                        file=spec.file,
                        key=list(ident),
                        field=field,
                        seed=left,
                        inventory=right,
                    )
                )
    return deltas


def _index_inventory_rows(
    rows: list[dict[str, str]], keys: list[str]
) -> dict[tuple[str, ...], dict[str, str]]:
    """Index inventory rows by pad-trimmed key tuple (V38 join).

    ``keys`` are inventory column names (after seed→inv alias resolution).
    """
    inv_index: dict[tuple[str, ...], dict[str, str]] = {}
    for row in rows:
        if all(k in row for k in keys):
            inv_index[tuple(_norm(row[k]) for k in keys)] = row
    return inv_index


def _norm(value: Any) -> str:
    """Pad-trim comparable form for join keys and field values (V38).

    String sides strip leading/trailing whitespace (DAC/NVarChar padding).
    Bools/numbers fold to a stable spelling so seed and inventory agree.
    Decimal-looking strings with a point collapse trailing zeros so seed
    ``0`` matches inventory ``0.000000`` without treating bare CDs like
    ``000000`` as numbers. Sibling of ``seed._norm`` spirit; used on *both*
    key join and field compare so padded inventory never misses a seed row
    or false-deltas.
    """
    if isinstance(value, bool):
        s = "true" if value else "false"
    elif value is None:
        s = ""
    elif isinstance(value, int):
        s = str(value)
    elif isinstance(value, float):
        as_int = int(value)
        s = str(as_int) if value == float(as_int) else str(value)
    else:
        s = str(value).strip()
        if "." in s:
            try:
                f = float(s)
            except ValueError:
                return s
            as_int = int(f)
            if f == float(as_int):
                s = str(as_int)
    return s


# ---------------------------------------------------------------------------
# Emit
# ---------------------------------------------------------------------------


def render_summary(bundle: FindingsBundle) -> str:
    doc: dict[str, Any] = {
        "inventory": bundle.inventory,
        "config": bundle.config,
        "erp": bundle.erp,
        "export_mode": bundle.export_mode,
        "tables": bundle.table_count,
        "findings": bundle.counts,
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def render_unmapped(bundle: FindingsBundle) -> str:
    doc: dict[str, Any] = {
        "kind": "unmapped",
        "tables": [t.model_dump() for t in bundle.unmapped],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def render_rest_gaps(bundle: FindingsBundle) -> str:
    doc: dict[str, Any] = {
        "kind": "rest_gaps",
        "gaps": [g.model_dump() for g in bundle.rest_gaps],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def render_deltas(bundle: FindingsBundle) -> str:
    doc: dict[str, Any] = {
        "kind": "deltas",
        "deltas": [d.model_dump() for d in bundle.deltas],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def render_custom_columns(bundle: FindingsBundle) -> str:
    doc: dict[str, Any] = {
        "kind": "custom_columns",
        "tables": [c.model_dump() for c in bundle.custom_columns],
    }
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def emit(
    bundle: FindingsBundle,
    out_dir: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Write FindingsBundle under ``out_dir`` (default ``findings/``).

    Skip-if-exists unless ``force``; dry-run would-write only. Never writes
    SEED_DIRS / ``config/`` / seed shape (V35/V36).
    """
    targets: list[tuple[Path, str, int]] = [
        (out_dir / SUMMARY_NAME, render_summary(bundle), bundle.table_count),
        (
            out_dir / "unmapped.yaml",
            render_unmapped(bundle),
            len(bundle.unmapped),
        ),
        (
            out_dir / "rest_gaps.yaml",
            render_rest_gaps(bundle),
            len(bundle.rest_gaps),
        ),
        (out_dir / "deltas.yaml", render_deltas(bundle), len(bundle.deltas)),
        (
            out_dir / "custom_columns.yaml",
            render_custom_columns(bundle),
            sum(len(c.columns) for c in bundle.custom_columns),
        ),
    ]

    written = 0
    skipped = 0
    for path, text, count in targets:
        if path.exists() and not force:
            output.data(f"skip {path} (exists)")
            skipped += 1
            continue
        if dry_run:
            output.data(f"would write {path} ({count})")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            output.data(f"write {path} ({count})")
        written += 1

    suffix = " (dry run)" if dry_run else ""
    counts = bundle.counts
    output.success(
        f"{written} written, {skipped} skipped; "
        f"unmapped={counts['unmapped']} rest_gaps={counts['rest_gaps']} "
        f"deltas={counts['deltas']} custom_columns={counts['custom_columns']}"
        f"{suffix}"
    )


def run(
    *,
    inventory_dir: Path,
    config_dir: Path | None,
    out_dir: Path,
    force: bool = False,
    dry_run: bool = False,
) -> FindingsBundle:
    """Load → reconcile → emit. Returns the bundle (for tests)."""
    inv = load_inventory_tree(inventory_dir)
    seeds = load_config_seeds(config_dir)
    smap = load_snapshot_map()
    bundle = reconcile(inv, seeds, config_dir=config_dir, snapshot_map=smap)
    # Guard: never write into config paths (V35/V36)
    if config_dir is not None:
        try:
            out_resolved = out_dir.resolve()
            cfg_resolved = config_dir.resolve()
            if out_resolved == cfg_resolved or cfg_resolved in out_resolved.parents:
                raise SystemExit(
                    f"{out_dir}: refuse to write findings under config/ "
                    "(V35/V36: reconcile never writes seed)"
                )
        except OSError:
            pass
    emit(bundle, out_dir, force=force, dry_run=dry_run)
    return bundle


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_INVENTORY",
    "DEFAULT_OUT",
    "FindingsBundle",
    "InventoryTree",
    "SnapshotMap",
    "emit",
    "load_config_seeds",
    "load_inventory_tree",
    "load_snapshot_map",
    "reconcile",
    "run",
]
