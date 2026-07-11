"""Live tenant state to seed YAML: the inverse of apply.

Driven by the packaged extract_manifest.yaml - per entity: the source
endpoint, key fields, destination file, and a strip deny-list or include
allow-list shaping the extracted records. Emitted files parse via
seed.load_baseline by construction (V20: bootstrap-entity rows must carry
an endpoint) and re-extract byte-identically: records sort by key tuple,
fields order key-first then alphabetical, None and empty-string values
are elided.

Extract reads live state and writes local files only - drift stays diff's
job (exit 2 never happens here).
"""

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .client import OPTIMIZATION_500, AcumaticaClient, unwrap
from .models import Model, validation_summary
from .seed import BOOTSTRAP_ENDPOINT, BOOTSTRAP_ENTITIES


class EntitySpec(Model):
    """One manifest row: how a live entity becomes a seed file."""

    entity: str
    keys: list[str] = Field(min_length=1)
    file: str
    endpoint: str | None = None
    strip: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)

    @field_validator("endpoint")
    @classmethod
    def _resolve_symbolic(cls, v: str | None) -> str | None:
        # `bootstrap` resolves to the packaged endpoint at load, so the
        # manifest never carries a literal version to go stale (V21)
        return BOOTSTRAP_ENDPOINT if v == "bootstrap" else v

    @model_validator(mode="after")
    def _strip_include_exclusive(self) -> "EntitySpec":
        if self.strip and self.include:
            raise ValueError("strip and include are mutually exclusive")
        return self


class SetupSynth(Model):
    """One setup/ synthesis row: a kind-dispatched action-file synthesizer.

    The synthesizers themselves land with T49; the model anchors the
    manifest shape (kind selects the synthesizer, file the destination).
    """

    kind: str
    file: str


class Manifest(Model):
    """The parsed extract manifest: entity rows plus setup synthesis rows."""

    entities: list[EntitySpec]
    setup: list[SetupSynth] = Field(default_factory=list)

    @model_validator(mode="after")
    def _self_consistent(self) -> "Manifest":
        # V20 by construction: an emitted file for an entity both endpoints
        # serve must carry an endpoint, or load_baseline rejects it
        for spec in self.entities:
            if spec.entity in BOOTSTRAP_ENTITIES and spec.endpoint is None:
                raise ValueError(
                    f"entity '{spec.entity}' is served by the packaged "
                    f"{BOOTSTRAP_ENDPOINT} and must carry an endpoint"
                )
        files = [s.file for s in self.entities] + [s.file for s in self.setup]
        dupes = {f for f in files if files.count(f) > 1}
        if dupes:
            raise ValueError(f"duplicate destination files: {sorted(dupes)}")
        return self


def load_manifest() -> Manifest:
    """Parse and validate the packaged extract manifest."""
    raw = yaml.safe_load(
        (resources.files("acumatica_cli") / "extract_manifest.yaml").read_text(
            encoding="utf-8"
        )
    )
    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(f"extract_manifest.yaml: {validation_summary(exc)}") from exc


def _fetch(client: AcumaticaClient, spec: EntitySpec) -> list[dict[str, Any]]:
    """Every live record of the entity, contract-API-wrapped.

    Primary read = the plain list GET. Entities mapping a BQL-delegate
    view 500 on that optimized export (B9); the fallback narrows the list
    GET to the key fields via $select (delegate fields out of scope), then
    reads each record through the key-URL single-record GET, which skips
    the optimizer (V4: read-back must survive delegate-view entities).
    """
    try:
        return client.get_list(spec.entity, endpoint=spec.endpoint)
    except RuntimeError as err:
        if OPTIMIZATION_500 not in str(err):
            raise
    key_rows = client.get_list(
        spec.entity,
        params={"$select": ",".join(spec.keys)},
        endpoint=spec.endpoint,
    )
    records: list[dict[str, Any]] = []
    for row in key_rows:
        values = unwrap(row)
        record = client.get_record(
            spec.entity, [values[k] for k in spec.keys], spec.endpoint
        )
        if record is not None:
            records.append(record)
    return records


def _shape(spec: EntitySpec, live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live records -> byte-stable seed records.

    Unwrap, apply the strip deny-list or include allow-list (key fields
    always survive), elide None and empty-string values, order fields key
    fields first (manifest order) then alphabetical, and sort records by
    key tuple - server order never leaks into the emitted bytes.
    """
    shaped: list[dict[str, Any]] = []
    for entity in live:
        record = unwrap(entity)
        missing = [k for k in spec.keys if k not in record]
        if missing:
            raise RuntimeError(
                f"{spec.entity}: live record missing key field(s) {', '.join(missing)}"
            )
        keep = {
            field: value
            for field, value in record.items()
            if field in spec.keys
            or (
                (field in spec.include if spec.include else field not in spec.strip)
                and value is not None
                and value != ""
            )
        }
        ordered = {k: keep[k] for k in spec.keys}
        ordered |= {k: keep[k] for k in sorted(keep.keys() - set(spec.keys))}
        shaped.append(ordered)
    return sorted(shaped, key=lambda r: tuple(str(r[k]) for k in spec.keys))


def _render(spec: EntitySpec, records: list[dict[str, Any]]) -> str:
    """Seed records -> the baseline YAML document load_baseline parses."""
    doc: dict[str, Any] = {
        "entity": spec.entity,
        "key": spec.keys[0] if len(spec.keys) == 1 else spec.keys,
    }
    if spec.endpoint is not None:
        doc["endpoint"] = spec.endpoint
    doc["records"] = records
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def run(
    client: AcumaticaClient,
    out_dir: Path,
    only: frozenset[str] = frozenset(),
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Extract every manifest entity into seed files under out_dir.

    Per file: skip when it exists (--force overwrites), skip when the
    tenant has no records, report-only under --dry-run. `only` filters
    rows by entity name or file stem.
    """
    manifest = load_manifest()
    for spec in manifest.entities:
        if only and spec.entity not in only and Path(spec.file).stem not in only:
            continue
        target = out_dir / spec.file
        if target.exists() and not force:
            output.data(f"skip {target} (exists)")
            continue
        live = _fetch(client, spec)
        if not live:
            output.data(f"skip {target} (no records)")
            continue
        records = _shape(spec, live)
        if dry_run:
            output.data(f"would write {target} ({len(records)} records)")
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render(spec, records), encoding="utf-8")
        output.data(f"write {target} ({len(records)} records)")
