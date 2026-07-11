"""Live tenant state to seed YAML: the inverse of apply.

Driven by the packaged extract_manifest.yaml - per entity: the source
endpoint, key fields, destination file, and a strip deny-list or include
allow-list shaping the extracted records. Emitted files parse via
seed.load_baseline by construction (V20: bootstrap-entity rows must carry
an endpoint) and re-extract byte-identically: records sort by key tuple,
fields order key-first then alphabetical, None and empty-string values
are elided.

setup/ action files are synthesized, not dumped: an action leaves no
keyed record to extract, so each manifest setup row's kind-dispatched
synthesizer reads the live state the action created (the done_when
surface) and derives the action file back. bootstrap/features.yaml is
the feature closure (V22/B15): the built-in six plus the union of the
manifest features: gates over record-producing entities - a live
FeaturesSet read is not available over the contract API (keyless
BqlDelegate view), so the closure derives from what the tenant serves.

Extract reads live state and writes local files only - drift stays diff's
job (exit 2 never happens here).
"""

from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .bootstrap import DEFAULT_FEATURES
from .client import OPTIMIZATION_500, AcumaticaClient, unwrap
from .models import Model, validation_summary
from .seed import BOOTSTRAP_ENDPOINT, BOOTSTRAP_ENTITIES

# The one non-manifest destination: the feature-closure file (V22/B15).
FEATURES_FILE = "bootstrap/features.yaml"


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
    """One setup/ synthesis row: a kind-dispatched action-file synthesizer."""

    kind: str
    file: str

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, v: str) -> str:
        if v not in SYNTHESIZERS:
            raise ValueError(
                f"unknown setup synthesizer kind '{v}' "
                f"(known: {', '.join(sorted(SYNTHESIZERS))})"
            )
        return v


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
        # FEATURES_FILE is claimed by the feature-closure render, never a row
        files = (
            [s.file for s in self.entities]
            + [s.file for s in self.setup]
            + [FEATURES_FILE]
        )
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


# -- setup/ synthesis: derive action files back from the state they created --


def _years(rows: list[dict[str, Any]]) -> list[str]:
    """Sorted distinct FinancialYear values off a live row set."""
    return sorted({str(unwrap(r)["FinancialYear"]) for r in rows})


def _synth_financial_year(client: AcumaticaClient) -> dict[str, Any] | None:
    """The FinYearSetup singleton back as its GeneratePeriods action file."""
    live = client.get_list("FinancialYearSettings", endpoint=BOOTSTRAP_ENDPOINT)
    if not live:
        return None
    settings = unwrap(live[0])
    return {
        "action": "GeneratePeriods",
        "entity": "FinancialYearSettings",
        "endpoint": BOOTSTRAP_ENDPOINT,
        "record": {
            # DateTimeValue comes back as a full ISO datetime; the action
            # record wants the date, quoted (the seed pipeline ships YAML
            # values as JSON verbatim - the setup/ template rationale)
            "BegFinYear": str(settings["BegFinYear"]).split("T")[0],
            "FinPeriods": settings["FinPeriods"],
            "PeriodType": settings["PeriodType"],
        },
        # the setup singleton either exists or does not - no filter
        "done_when": {},
    }


def _synth_master_calendar(client: AcumaticaClient) -> dict[str, Any] | None:
    """The master-calendar year range back as its GenerateCalendar action file."""
    live = client.get_list("MasterCalendar", endpoint=BOOTSTRAP_ENDPOINT)
    if not live:
        return None
    years = _years(live)
    return {
        "action": "GenerateCalendar",
        "entity": "MasterCalendar",
        "endpoint": BOOTSTRAP_ENDPOINT,
        "record": {"FinancialYear": years[0]},
        "parameters": {"FromYear": years[0], "ToYear": years[-1]},
        # the company calendar derives from the master, so it is the
        # stronger done evidence (the setup/ template rationale); the last
        # year means generation completed through the range
        "done_when": {
            "entity": "CompanyCalendar",
            "filter": f"FinancialYear eq '{years[-1]}'",
        },
    }


def _synth_open_periods(client: AcumaticaClient) -> dict[str, Any] | None:
    """The open-period range back as its GL503000 ProcessAll action file."""
    live = client.get_list(
        "CompanyPeriod",
        params={"$filter": "Status eq 'Open'"},
        endpoint=BOOTSTRAP_ENDPOINT,
    )
    if not live:
        return None
    years = _years(live)
    # OrganizationID = the extracted Company's AcctCD: the reference
    # resolves inside the emitted set (V22 - bootstrap/company.yaml
    # creates the organization the action names)
    companies = client.get_list("Company", endpoint=BOOTSTRAP_ENDPOINT)
    if not companies:
        raise RuntimeError("open-periods: no Company on tenant")
    org = sorted(str(unwrap(c)["AcctCD"]) for c in companies)[0]
    return {
        "action": "ProcessAll",
        "entity": "ManagePeriods",
        "endpoint": BOOTSTRAP_ENDPOINT,
        "record": {
            "Action": "Open",
            "FromYear": years[0],
            "ToYear": years[-1],
            "OrganizationID": org,
        },
        # both filter fields live on the one CompanyPeriod view (a
        # conjunction spanning views answers 200 [] - B14 class); the last
        # year Open means activation completed through the range
        "done_when": {
            "entity": "CompanyPeriod",
            "filter": f"FinancialYear eq '{years[-1]}' and Status eq 'Open'",
        },
    }


# kind -> (synthesizer, skip reason when the live state is absent);
# SetupSynth validates manifest kinds against this registry
type Synthesizer = Callable[[AcumaticaClient], dict[str, Any] | None]
SYNTHESIZERS: dict[str, tuple[Synthesizer, str]] = {
    "financial-year": (_synth_financial_year, "no financial year setup"),
    "master-calendar": (_synth_master_calendar, "no master calendar"),
    "open-periods": (_synth_open_periods, "no open periods"),
}


def render_features(gates: Iterable[str]) -> str:
    """The feature-closure bootstrap/features.yaml: built-in six + gates.

    Deterministic order (byte-stable re-extract): the built-in six in
    their bootstrap.DEFAULT_FEATURES spelling, then the extra gates
    alphabetically.
    """
    names = list(DEFAULT_FEATURES) + sorted(set(gates) - set(DEFAULT_FEATURES))
    header = (
        "# FeaturesSet property names the bootstrap plugin enables on publish -\n"
        "# the built-in minimum plus every features: gate the extracted seed\n"
        "# files require (feature closure). A misspelled name enables nothing -\n"
        "# the plugin flags it in the publish log.\n"
    )
    return header + yaml.safe_dump(names, default_flow_style=False)


class _Extraction:
    """One extract run: the three passes share the run's knobs as state."""

    def __init__(
        self,
        client: AcumaticaClient,
        out_dir: Path,
        only: frozenset[str],
        force: bool,
        dry_run: bool,
    ) -> None:
        self.client = client
        self.manifest = load_manifest()
        self.out_dir = out_dir
        self.only = only
        self.force = force
        self.dry_run = dry_run

    def _selected(self, name: str, file: str) -> bool:
        """The --only filter: row name (entity or kind) or file stem."""
        return not self.only or name in self.only or Path(file).stem in self.only

    def _skip_existing(self, target: Path) -> bool:
        """The per-file skip-if-exists gate; --force disarms it."""
        if target.exists() and not self.force:
            output.data(f"skip {target} (exists)")
            return True
        return False

    def _emit(self, target: Path, text: str, count: int) -> None:
        """Write one destination file, or report what would be written."""
        if self.dry_run:
            output.data(f"would write {target} ({count} records)")
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        output.data(f"write {target} ({count} records)")

    def entities(self) -> set[str]:
        """The entity pass; returns destination files written (or would be)."""
        produced: set[str] = set()
        for spec in self.manifest.entities:
            if not self._selected(spec.entity, spec.file):
                continue
            target = self.out_dir / spec.file
            if self._skip_existing(target):
                continue
            live = _fetch(self.client, spec)
            if not live:
                output.data(f"skip {target} (no records)")
                continue
            records = _shape(spec, live)
            self._emit(target, _render(spec, records), len(records))
            produced.add(spec.file)
        return produced

    def setup(self) -> None:
        """The setup/ pass: synthesize each action file back from live state."""
        for synth in self.manifest.setup:
            if not self._selected(synth.kind, synth.file):
                continue
            target = self.out_dir / synth.file
            if self._skip_existing(target):
                continue
            synthesize, skip_reason = SYNTHESIZERS[synth.kind]
            doc = synthesize(self.client)
            if doc is None:
                output.data(f"skip {target} ({skip_reason})")
                if synth.kind == "open-periods":
                    # generated-but-unopened periods replay into a tenant
                    # that cannot post GL (B13/B16 class) - flag it
                    output.warn(
                        "no open periods on tenant - a replayed tenant "
                        "cannot post GL until periods are opened"
                    )
                continue
            text = yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
            self._emit(target, text, 1)

    def features(self, produced: set[str]) -> None:
        """The feature-closure pass (V22/B15).

        Gates union over entities whose destination file is in the output
        set - produced this run or already on disk.
        """
        if not self._selected("features", FEATURES_FILE):
            return
        target = self.out_dir / FEATURES_FILE
        if self._skip_existing(target):
            return
        gates = [
            gate
            for spec in self.manifest.entities
            if spec.file in produced or (self.out_dir / spec.file).exists()
            for gate in spec.features
        ]
        text = render_features(gates)
        self._emit(target, text, len(yaml.safe_load(text)))


def run(
    client: AcumaticaClient,
    out_dir: Path,
    only: frozenset[str] = frozenset(),
    force: bool = False,
    dry_run: bool = False,
) -> None:
    """Extract the manifest file set plus the feature closure under out_dir.

    Per file: skip when it exists (--force overwrites), skip when the
    tenant has no records, report-only under --dry-run. `only` filters
    rows by entity name, synthesizer kind, or file stem.
    """
    extraction = _Extraction(client, out_dir, only, force, dry_run)
    produced = extraction.entities()
    extraction.setup()
    extraction.features(produced)
