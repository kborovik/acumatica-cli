"""Derived-state observations: snapshot/*.yaml views -> snapshots/*.yaml files.

`acu snapshot` captures live balances, quantities, and totals as committed
evidence (SPEC I.cmd / V32). Not a seed inverse: never writes config/, never
carries endpoint: symbols, never participates in apply/diff. Views configure
the observer; observations are git-diffable flow-style YAML.

View file format (I.data snapshot/*):

    name: trial-balance
    source:
      gi: LAB5-TrialBalance          # or: entity: Account
      params: { Period: "072026" }   # pinned; not runtime-resolved
    key: [Branch, Account]
    capture: [Description, BegBalance, DebitTotal, CreditTotal, EndingBalance]
    decimals: 2

Observation file format (I.data snapshots/*):

    view: trial-balance
    erp: "26.101.0225"
    rows:
      - {Account: "10100", BegBalance: "0.00", ...}
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .client import AcumaticaClient, unwrap
from .models import Model, validation_summary

# Flow-style observation rows: one mapping per line under `rows:`.
_ROW_PREFIX = "  - "


class SourceSpec(Model):
    """Exactly one of gi: | entity:; optional params pinned in YAML (V33)."""

    gi: str | None = None
    entity: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_source(self) -> SourceSpec:
        if (self.gi is None) == (self.entity is None):
            raise ValueError("source: exactly one of gi, entity")
        return self

    @property
    def kind(self) -> Literal["gi", "entity"]:
        """Discriminator: ``gi`` or ``entity`` (exactly one set)."""
        return "gi" if self.gi is not None else "entity"

    @property
    def name(self) -> str:
        """The GI title or contract entity name for this source."""
        return self.gi if self.gi is not None else self.entity  # type: ignore[return-value]


class ViewDef(Model):
    """Observer config for one capture (I.data snapshot/*; V32/V33)."""

    name: str
    source: SourceSpec
    key: list[str]
    capture: list[str]
    decimals: int = 2
    path: Path = Field(exclude=True, repr=False)

    @field_validator("name")
    @classmethod
    def _name_stem(cls, v: str) -> str:
        v = v.strip()
        if not v or "/" in v or "\\" in v or v.endswith(".yaml"):
            raise ValueError(
                f"name must be a bare output stem (got {v!r}); "
                "writes snapshots/<name>.yaml"
            )
        return v

    @field_validator("key", "capture")
    @classmethod
    def _nonempty_fields(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("must be a non-empty list of field names")
        return list(v)

    @field_validator("decimals")
    @classmethod
    def _decimals_nonneg(cls, v: int) -> int:
        if v < 0:
            raise ValueError("decimals must be >= 0")
        return v


class Observation(Model):
    """Committed derived-state capture (I.data snapshots/*; V32)."""

    view: str
    erp: str
    rows: list[dict[str, Any]] = Field(default_factory=list)


def load_view(path: Path) -> ViewDef:
    """Parse one snapshot view definition; hard error on invalid shape."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping at the root")
    try:
        return ViewDef.model_validate({**raw, "path": path})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def load_observation(path: Path) -> Observation:
    """Parse a committed observation file."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise SystemExit(f"{path}: expected a mapping at the root")
    try:
        return Observation.model_validate(raw)
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def _is_numeric(value: Any) -> bool:
    """True when value should be fixed-point formatted (money/qty)."""
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return False
        try:
            Decimal(text)
        except InvalidOperation:
            return False
        return True
    return False


def format_key(value: Any) -> str | int | bool | None:
    """Identity cell for key columns - no money fixed-point (V32).

    Account codes like "10000" and ints stay as-is so sort identity
    matches the live tenant; only capture columns get decimals formatting.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        # key from float is rare; keep lossless string without forcing scale
        return str(value).rstrip("0").rstrip(".") if "." in str(value) else str(value)
    if isinstance(value, str):
        return value
    return str(value)


def format_cell(value: Any, decimals: int) -> str | int | bool | None:
    """Normalize one capture cell for observation output (V32).

    Numerics become fixed-point strings at ``decimals`` (money/qty). Non-
    numerics keep a YAML-safe scalar type. Nested values stringify.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if _is_numeric(value):
        quant = Decimal(1).scaleb(-decimals) if decimals else Decimal(1)
        d = Decimal(str(value)).quantize(quant)
        # fixed-point string always — never float in the file
        return f"{d:.{decimals}f}" if decimals else str(int(d))
    if isinstance(value, (int, str)):
        return value
    return str(value)


def _key_tuple(row: dict[str, Any], keys: list[str]) -> tuple[Any, ...]:
    return tuple(row.get(k) for k in keys)


def project_rows(
    live_rows: list[dict[str, Any]], view: ViewDef
) -> list[dict[str, Any]]:
    """Allowlist + format + sort by key; key-tuple collision → SystemExit 1 path.

    ``capture:`` is an allowlist (V32): unexpected live fields are dropped.
    Sort is always by the key tuple so REST/OData order is not contractual.
    Key columns use ``format_key`` (identity); other capture columns use
    fixed-point ``format_cell`` so money never lands as float.
    """
    projected: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], int] = {}
    capture_only = [f for f in view.capture if f not in view.key]
    for i, raw in enumerate(live_rows):
        row: dict[str, Any] = {}
        for k in view.key:
            row[k] = format_key(raw.get(k))
        for f in capture_only:
            if f in raw:
                row[f] = format_cell(raw.get(f), view.decimals)
        kt = _key_tuple(row, view.key)
        if kt in seen:
            raise SystemExit(
                f"{view.path}: key collision on {dict(zip(view.key, kt, strict=True))} "
                f"(rows {seen[kt]} and {i}); key must uniquely identify rows (V32)"
            )
        seen[kt] = i
        projected.append(row)
    projected.sort(key=lambda r: _key_tuple(r, view.key))
    # observation row: key fields first (view order), then capture alpha
    ordered: list[dict[str, Any]] = []
    capture_rest = sorted(capture_only)
    col_order = [*view.key, *capture_rest]
    for row in projected:
        ordered.append({c: row[c] for c in col_order if c in row})
    return ordered


def _yaml_flow_scalar(value: Any) -> str:
    """Render one scalar inside a flow mapping (ASCII, git-friendly).

    Strings are always double-quoted so codes like ``10100`` and fixed-
    point money stay strings on ``yaml.safe_load`` round-trip (bare
    ``10100`` would reparse as int and break key identity).
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_observation(obs: Observation) -> str:
    """Byte-stable observation YAML: one flow-style row per line (V32)."""
    lines = [
        f"view: {obs.view}",
        f"erp: {_yaml_flow_scalar(obs.erp)}",
        "rows:",
    ]
    if not obs.rows:
        lines.append("  []")
    else:
        for row in obs.rows:
            # key-sorted within the flow map for stable diffs when col set grows
            items = ", ".join(f"{k}: {_yaml_flow_scalar(row[k])}" for k in row)
            lines.append(f"{_ROW_PREFIX}{{{items}}}")
    return "\n".join(lines) + "\n"


def write_observation(path: Path, obs: Observation) -> None:
    """Write observation file; parent dirs created as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_observation(obs), encoding="utf-8")


def compare_observations(live: Observation, disk: Observation | None) -> list[str]:
    """Human-readable drift lines (view/erp/rows); empty = identical rows+erp.

    Row identity is the full projected row dict order as rendered. Used by
    --diff and --assert-unchanged; never writes.
    """
    if disk is None:
        return [f"missing on disk (live has {len(live.rows)} row(s))"]
    drifts: list[str] = []
    if live.view != disk.view:
        drifts.append(f"view: live={live.view!r} disk={disk.view!r}")
    if live.erp != disk.erp:
        drifts.append(f"erp: live={live.erp!r} disk={disk.erp!r}")
    live_text = render_observation(live)
    disk_text = render_observation(disk)
    if live_text == disk_text:
        return drifts
    if len(live.rows) != len(disk.rows):
        drifts.append(f"rows: live has {len(live.rows)}, disk has {len(disk.rows)}")
    elif not drifts:
        for i, (a, b) in enumerate(zip(live.rows, disk.rows, strict=True)):
            if a != b:
                drifts.append(f"row[{i}]: live={a!r} disk={b!r}")
    return drifts


def expand_view_files(files: tuple[Path, ...]) -> list[Path]:
    """Expand dir args to sorted ``*.yaml``; explicit files preserved order."""
    paths: list[Path] = []
    for path in files:
        if path.is_dir():
            found = sorted(path.glob("*.yaml"))
            if not found:
                raise SystemExit(f"{path}: no snapshot *.yaml files in directory")
            paths += found
        else:
            paths.append(path)
    return paths


def live_erp_build(client: AcumaticaClient) -> str:
    """Live ERP build id for observation ``erp:`` header (V32).

    Prefers ``GET /entity`` wrapper ``version.acumaticaBuildVersion`` (T92);
    falls back to claimed ``target.yaml`` erp is not available here — use
    the configured default string when the wrapper omits build.
    """
    try:
        _endpoints, build = client.entity_root()
    except RuntimeError, OSError:
        return "unknown"
    return build or "unknown"


def fetch_live_rows(client: AcumaticaClient, view: ViewDef) -> list[dict[str, Any]]:
    """Pull raw unwrapped rows for a view via entity: or gi: (V33)."""
    if view.source.kind == "entity":
        return _fetch_entity_rows(client, view)
    return _fetch_gi_rows(client, view)


def _odata_params(params: dict[str, Any]) -> dict[str, str]:
    """Stringify view params for OData/query use; keys pass through as-is."""
    return {str(k): "" if v is None else str(v) for k, v in params.items()}


def _fetch_entity_rows(client: AcumaticaClient, view: ViewDef) -> list[dict[str, Any]]:
    """Contract REST list GET on Default/<api_version> (V33; no per-view pin)."""
    assert view.source.entity is not None
    params = _odata_params(view.source.params)
    # narrow select when no explicit $select — allowlist only
    if "$select" not in params and "select" not in params:
        fields = list(dict.fromkeys([*view.key, *view.capture]))
        params["$select"] = ",".join(fields)
    rows = client.get_list(view.source.entity, params=params or None)
    return [unwrap(r) for r in rows]


def validate_gi_params(metadata_xml: str, gi_name: str, params: dict[str, Any]) -> None:
    """Fail-closed: every param key must appear in the GI's $metadata (V33).

    Acumatica OData metadata is EDMX. Parameter names may surface as
    ``Parameter`` / ``Property`` / ``ParameterImport`` elements with a
    ``Name`` attribute. Unknown param keys raise SystemExit — silent
    ignore of Period (and kin) would produce phantom full-set captures.
    """
    if not params:
        return
    try:
        root = ET.fromstring(metadata_xml)
    except ET.ParseError as exc:
        raise SystemExit(
            f"gi {gi_name!r}: $metadata not parseable as XML: {exc}"
        ) from exc
    names: set[str] = set()
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag in {
            "Parameter",
            "Property",
            "ParameterImport",
            "FunctionImport",
            "EntityType",
            "ComplexType",
        }:
            name = el.attrib.get("Name") or el.attrib.get("name")
            if name:
                names.add(name)
        # also collect Annotation/Documentation free text sparingly — skip
    # Always allow common OData system options
    system = {"$filter", "$select", "$top", "$orderby", "$expand", "$skip", "$format"}
    unknown = [k for k in params if k not in names and k not in system]
    if unknown and not names:
        # metadata had no Name attributes we recognize — fail closed
        raise SystemExit(
            f"gi {gi_name!r}: $metadata has no discoverable parameter names; "
            f"cannot validate params {sorted(params)}; refuse rather than "
            "silently ignore (V33)"
        )
    if unknown:
        raise SystemExit(
            f"gi {gi_name!r}: unknown params {unknown} not in $metadata "
            f"(known: {', '.join(sorted(names)) or '(none)'}) (V33)"
        )


def _fetch_gi_rows(client: AcumaticaClient, view: ViewDef) -> list[dict[str, Any]]:
    """OData Generic Inquiry rows (V33); params validated against $metadata."""
    assert view.source.gi is not None
    gi = view.source.gi
    params = dict(view.source.params)
    meta = client.odata_gi_metadata(gi)
    validate_gi_params(meta, gi, params)
    raw = client.odata_gi(gi, params=_odata_params(params) or None)
    return _odata_value_rows(raw)


def _odata_value_rows(body: Any) -> list[dict[str, Any]]:
    """Normalize OData JSON to a list of plain dict rows."""
    if isinstance(body, list):
        return [dict(r) for r in body if isinstance(r, dict)]
    if isinstance(body, dict):
        value = body.get("value")
        if isinstance(value, list):
            return [dict(r) for r in value if isinstance(r, dict)]
        # single entity object
        if body and not any(k.startswith("@odata") for k in body if k != "value"):
            # might still be a wrapper with only odata keys + value missing
            data = {k: v for k, v in body.items() if not str(k).startswith("@")}
            if data and "value" not in data:
                return [data]
    raise RuntimeError(f"OData response not a row list: {type(body).__name__}")


def capture_view(
    client: AcumaticaClient, view: ViewDef, erp: str | None = None
) -> Observation:
    """Fetch live rows, project, return Observation (no disk I/O)."""
    rows = project_rows(fetch_live_rows(client, view), view)
    return Observation(
        view=view.name,
        erp=erp if erp is not None else live_erp_build(client),
        rows=rows,
    )


def observation_path(out_dir: Path, view: ViewDef) -> Path:
    """Default observation path for a view under ``out_dir``."""
    return out_dir / f"{view.name}.yaml"


def _dry_run_views(views: list[ViewDef], out_dir: Path) -> int:
    """List resolved views without HTTP (V32 --dry-run)."""
    for view in views:
        src = f"{view.source.kind}:{view.source.name}"
        params = f" params={view.source.params}" if view.source.params else ""
        output.data(
            f"would capture {view.path} -> "
            f"{observation_path(out_dir, view)} ({src}{params})"
        )
    output.data(f"{len(views)} view(s) (dry run)")
    return 0


def _process_view(
    client: AcumaticaClient,
    view: ViewDef,
    *,
    out_dir: Path,
    erp: str,
    diff: bool,
) -> bool:
    """Capture one view; write or compare. Returns True if state moved."""
    dest = observation_path(out_dir, view)
    output.data(f"{view.path} -> {view.name} ({view.source.kind}:{view.source.name})")
    live = capture_view(client, view, erp=erp)
    if not diff:
        write_observation(dest, live)
        output.data(f"  wrote {dest} ({len(live.rows)} row(s))")
        return False
    disk = load_observation(dest) if dest.is_file() else None
    drifts = compare_observations(live, disk)
    if drifts:
        output.data(f"  changed ({len(drifts)} difference(s))")
        for line in drifts:
            output.data(f"    {line}")
        return True
    output.data(f"  {len(live.rows)} row(s) unchanged")
    return False


def run_views(
    client: AcumaticaClient | None,
    views: list[ViewDef],
    *,
    out_dir: Path,
    mode: Literal["write", "diff", "assert", "dry"] = "write",
) -> int:
    """Execute snapshot for views; return process exit code (V32 exit matrix).

    0 = ok (write or compare; change fine unless assert)
    1 = operational failure
    2 = state moved under assert mode
    """
    if mode == "dry":
        return _dry_run_views(views, out_dir)
    assert client is not None
    diff = mode in ("diff", "assert")
    erp = live_erp_build(client)
    moved = False
    try:
        for view in views:
            if _process_view(client, view, out_dir=out_dir, erp=erp, diff=diff):
                moved = True
    except SystemExit:
        raise
    except (RuntimeError, OSError) as exc:
        output.error(str(exc))
        return 1
    if mode == "assert" and moved:
        return 2
    return 0
