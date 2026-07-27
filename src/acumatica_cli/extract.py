"""Live tenant state to seed YAML: the inverse of apply.

Driven by the packaged seed_catalog.yaml - per entity: the source
endpoint, key fields, destination file, an optional record filter, a
strip deny-list or include allow-list shaping the extracted records, and
optional detail_keys for list fields (T60 load_baseline).
Hard-cut emit under config/{bootstrap,baseline,setup,master}/ (V30/V34):
no root SEED_DIRS paths, no --layout. Emitted files parse via
seed.load_baseline by construction (V20: bootstrap-entity rows must
carry an endpoint) and re-extract byte-identically: records sort by key
tuple, fields order key-first then alphabetical, None and empty-string
values are elided.

setup/ action files are synthesized, not dumped: an action leaves no
keyed record to extract, so each catalog setup row's kind-dispatched
synthesizer reads the live state the action created (the done_when
surface) and derives the action file back.
config/bootstrap/features.yaml is the feature closure (V22/B15): the
built-in six plus the union of the catalog features: gates over
record-producing entities - a live FeaturesSet read is not available
over the contract API (keyless BqlDelegate view), so the closure
derives from what the tenant serves.

Extract reads live state and writes local files only - drift stays diff's
job (exit 2 never happens here).
"""

import itertools
from collections.abc import Callable, Iterable
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .bootstrap import DEFAULT_FEATURES
from .client import (
    OPTIMIZATION_500,
    SETUP_NOT_ENTERED_500,
    AcumaticaClient,
    unwrap,
)
from .models import Model, validation_summary
from .seed import active_bootstrap, resolve_endpoint

# The one non-catalog destination: the feature-closure file (V22/B15).
FEATURES_FILE = "config/bootstrap/features.yaml"
_SYMBOLIC_BOOTSTRAP = "bootstrap"
_CATALOG_NAME = "seed_catalog.yaml"


class EntitySpec(Model):
    """One catalog row: how a live entity becomes a seed file."""

    entity: str
    keys: list[str] = Field(min_length=1)
    file: str
    # Symbolic `bootstrap` stays symbolic until HTTP time (V20/V21): the
    # emitted file carries the symbol so a version bump never rewrites
    # extract output; resolve_endpoint maps it to Bootstrap/<ver>.
    endpoint: str | None = None
    filter: str | None = None
    strip: list[str] = Field(default_factory=list)
    include: list[str] = Field(default_factory=list)
    features: list[str] = Field(default_factory=list)
    # {ListField: RowKeyField} — emitted into seed so load_baseline accepts
    # detail arrays (T60); required when include/strip keeps a list field.
    detail_keys: dict[str, str] | None = None

    @model_validator(mode="after")
    def _strip_include_exclusive(self) -> EntitySpec:
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
    """The parsed seed catalog: entity rows plus setup synthesis rows."""

    entities: list[EntitySpec]
    setup: list[SetupSynth] = Field(default_factory=list)

    @model_validator(mode="after")
    def _self_consistent(self) -> Manifest:
        # V20 by construction: an emitted file for an entity the active
        # Bootstrap contract serves must carry an endpoint (literal or
        # symbolic bootstrap), or load_baseline rejects it
        _name, entities = active_bootstrap()
        for spec in self.entities:
            if spec.entity in entities and spec.endpoint is None:
                raise ValueError(
                    f"entity '{spec.entity}' is served by the active "
                    f"{_name} and must carry an endpoint"
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
    """Parse and validate the packaged seed catalog."""
    raw = yaml.safe_load(
        (resources.files("acumatica_cli") / _CATALOG_NAME).read_text(encoding="utf-8")
    )
    try:
        return Manifest.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(f"{_CATALOG_NAME}: {validation_summary(exc)}") from exc


# V34 exclusions: features synthesis, contract XML, observer views — not seed.
_TEMPLATE_SEED_EXEMPT_NAMES = frozenset({"features.yaml", "project.xml"})
_TEMPLATE_SEED_EXEMPT_PREFIXES = ("config/views/",)


def packaged_template_seed_files() -> frozenset[str]:
    """Packaged ``templates/config/**`` seed paths that V34 requires catalog rows for.

    Paths are data-repo relative (``config/...``). Excludes features synthesis,
    ``project.xml``, and ``config/views/`` observer defs.
    """
    root = resources.files("acumatica_cli").joinpath("templates", "config")
    out: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        for child in node.iterdir():
            name = child.name
            rel = f"{prefix}{name}" if not prefix else f"{prefix}/{name}"
            path = f"config/{rel}"
            if child.is_dir():
                walk(child, rel)
                continue
            if not name.endswith(".yaml"):
                continue
            if name in _TEMPLATE_SEED_EXEMPT_NAMES:
                continue
            if any(path.startswith(p) for p in _TEMPLATE_SEED_EXEMPT_PREFIXES):
                continue
            out.add(path)

    walk(root, "")
    return frozenset(out)


def catalog_seed_files(manifest: Manifest | None = None) -> frozenset[str]:
    """Catalog entity + setup emit paths (features synthesis is not a catalog row)."""
    m = manifest if manifest is not None else load_manifest()
    return frozenset(s.file for s in m.entities) | frozenset(s.file for s in m.setup)


def catalog_completeness_gap(
    manifest: Manifest | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """V34 completeness: (missing from catalog, extra in catalog).

    Empty pair means template seed set equals catalog file set.
    """
    templates = packaged_template_seed_files()
    catalog = catalog_seed_files(manifest)
    return templates - catalog, catalog - templates


def _expand_for(spec: EntitySpec) -> list[str]:
    """``$expand`` paths the catalog row needs for details + linked entities.

    Detail arrays travel only under ``$expand=<ListField>`` (T60); linked
    entities under ``$expand=<Field>`` / nested slash paths (T65). Without
    expand the plain list GET returns top-level scalars alone — extract
    would drop ``Locations`` / ``MainContact`` and re-apply on a virgin
    tenant 422s (Vendor ``Country`` cannot be empty) or loses warehouse
    bins. Detail fields come from ``detail_keys``; ``MainContact`` is the
    packaged linked-entity include (Address nested for Country write-path).
    """
    paths: list[str] = []
    if spec.detail_keys:
        paths.extend(spec.detail_keys)
    for field in spec.include or ():
        if field in (spec.detail_keys or {}):
            continue
        if field == "MainContact":
            paths.append("MainContact")
            paths.append("MainContact/Address")
    return sorted(set(paths))


def _fetch(client: AcumaticaClient, spec: EntitySpec) -> list[dict[str, Any]]:
    """Every live record of the entity, contract-API-wrapped.

    Primary read = the plain list GET. Entities mapping a BQL-delegate
    view 500 on that optimized export (B9); the fallback narrows the list
    GET to the key fields via $select (delegate fields out of scope), then
    reads each record through the key-URL single-record GET, which skips
    the optimizer (V4: read-back must survive delegate-view entities).

    A catalog filter rides both list reads, so the two paths serve the
    same record set and the per-key walk only visits filtered keys.
    ``$expand`` rides both paths when the catalog claims detail lists or
    linked entities (T60/T65) — same class of expand as seed.diff.
    """
    endpoint = resolve_endpoint(spec.endpoint, api_version=client.instance.api_version)
    params: dict[str, str] = {}
    if spec.filter:
        params["$filter"] = spec.filter
    expand = _expand_for(spec)
    if expand:
        params["$expand"] = ",".join(expand)
    try:
        return client.get_list(spec.entity, params=params or None, endpoint=endpoint)
    except RuntimeError as err:
        if OPTIMIZATION_500 not in str(err):
            raise
    key_rows = client.get_list(
        spec.entity,
        params={"$select": ",".join(spec.keys)}
        | ({"$filter": spec.filter} if spec.filter else {}),
        endpoint=endpoint,
    )
    expand_params = {"$expand": ",".join(expand)} if expand else None
    records: list[dict[str, Any]] = []
    for row in key_rows:
        values = unwrap(row)
        record = client.get_record(
            spec.entity,
            [values[k] for k in spec.keys],
            endpoint,
            params=expand_params,
        )
        if record is not None:
            records.append(record)
    return records


# Server-assigned / audit fields that must never enter seed (B11 class).
# Applied recursively under linked entities and detail rows after expand
# (T65/T119): MainContact.ContactID and AllowedCashAccounts.LastModified*
# would otherwise permanent-red-diff after re-apply on a fresh tenant.
_SERVER_DERIVED = frozenset(
    {
        "ContactID",
        "CreatedDateTime",
        "LastModifiedDateTime",
        "NoteID",
        "tstamp",
    }
)


def _elide_server_derived(value: Any) -> Any:
    """Drop server-derived keys recursively; elide empty nested containers."""
    if isinstance(value, dict):
        cleaned = {
            k: _elide_server_derived(v)
            for k, v in value.items()
            if k not in _SERVER_DERIVED
            and v is not None
            and v != ""
            and not (isinstance(v, (dict, list)) and not v)
        }
        return cleaned
    if isinstance(value, list):
        return [_elide_server_derived(v) for v in value]
    return value


def _kept_fields(spec: EntitySpec, record: dict[str, Any]) -> dict[str, Any]:
    """Apply include/strip + nested server-derived elision to one unwrapped row."""
    keep: dict[str, Any] = {}
    for field, value in record.items():
        if field in spec.keys:
            keep[field] = value
            continue
        if field in _SERVER_DERIVED:
            continue
        if spec.include:
            if field not in spec.include:
                continue
        elif field in spec.strip:
            continue
        # Nested ContactID / LastModifiedDateTime under expand (T65/T119).
        cleaned = _elide_server_derived(value)
        if cleaned is None or cleaned == "":
            continue
        if isinstance(cleaned, (dict, list)) and not cleaned:
            continue
        keep[field] = cleaned
    ordered = {k: keep[k] for k in spec.keys if k in keep}
    ordered |= {k: keep[k] for k in sorted(keep.keys() - set(spec.keys))}
    return ordered


def _shape(spec: EntitySpec, live: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Live records -> byte-stable seed records.

    Unwrap, apply the strip deny-list or include allow-list (key fields
    always survive), elide None and empty-string values, drop nested
    server-derived fields (B11), order fields key fields first (catalog
    order) then alphabetical, and sort records by key tuple - server
    order never leaks into the emitted bytes.

    Records duplicating the declared key tuple are a hard error (V25): an
    under-keyed file diffs as permanent false drift and apply collapses
    the dup records into one PUT target (B21) - the caller reports the
    row failure and never emits the file.
    """
    shaped: list[dict[str, Any]] = []
    for entity in live:
        record = unwrap(entity)
        missing = [k for k in spec.keys if k not in record]
        if missing:
            raise RuntimeError(
                f"{spec.entity}: live record missing key field(s) {', '.join(missing)}"
            )
        shaped.append(_kept_fields(spec, record))
    shaped.sort(key=lambda r: tuple(str(r[k]) for k in spec.keys))
    idents = [tuple(str(r[k]) for k in spec.keys) for r in shaped]
    for prev, cur in itertools.pairwise(idents):
        if prev == cur:
            raise RuntimeError(
                f"records duplicate key tuple [{', '.join(cur)}] - the "
                f"declared key ({', '.join(spec.keys)}) does not identify "
                "each record"
            )
    return shaped


def _render(spec: EntitySpec, records: list[dict[str, Any]]) -> str:
    """Seed records -> the baseline YAML document load_baseline parses."""
    doc: dict[str, Any] = {
        "entity": spec.entity,
        "key": spec.keys[0] if len(spec.keys) == 1 else spec.keys,
    }
    if spec.endpoint is not None:
        doc["endpoint"] = spec.endpoint
    if spec.detail_keys:
        doc["detail_keys"] = dict(spec.detail_keys)
    doc["records"] = records
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


# -- setup/ synthesis: derive action files back from the state they created --


def _years(rows: list[dict[str, Any]]) -> list[str]:
    """Sorted distinct FinancialYear values off a live row set."""
    return sorted({str(unwrap(r)["FinancialYear"]) for r in rows})


def _synth_financial_year(client: AcumaticaClient) -> dict[str, Any] | None:
    """The FinYearSetup singleton back as its GeneratePeriods action file."""
    ep = resolve_endpoint(_SYMBOLIC_BOOTSTRAP)
    live = client.get_list("FinancialYearSettings", endpoint=ep)
    if not live:
        return None
    settings = unwrap(live[0])
    return {
        "action": "GeneratePeriods",
        "entity": "FinancialYearSettings",
        "endpoint": _SYMBOLIC_BOOTSTRAP,
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
    ep = resolve_endpoint(_SYMBOLIC_BOOTSTRAP)
    live = client.get_list("MasterCalendar", endpoint=ep)
    if not live:
        return None
    years = _years(live)
    return {
        "action": "GenerateCalendar",
        "entity": "MasterCalendar",
        "endpoint": _SYMBOLIC_BOOTSTRAP,
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
    ep = resolve_endpoint(_SYMBOLIC_BOOTSTRAP)
    live = client.get_list(
        "CompanyPeriod",
        params={"$filter": "Status eq 'Open'"},
        endpoint=ep,
    )
    if not live:
        return None
    years = _years(live)
    # OrganizationID = the extracted Company's AcctCD: the reference
    # resolves inside the emitted set (V22 - config/bootstrap/company.yaml
    # creates the organization the action names)
    companies = client.get_list("Company", endpoint=ep)
    if not companies:
        raise RuntimeError("open-periods: no Company on tenant")
    org = sorted(str(unwrap(c)["AcctCD"]) for c in companies)[0]
    return {
        "action": "ProcessAll",
        "entity": "ManagePeriods",
        "endpoint": _SYMBOLIC_BOOTSTRAP,
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
# SetupSynth validates catalog kinds against this registry
type Synthesizer = Callable[[AcumaticaClient], dict[str, Any] | None]
SYNTHESIZERS: dict[str, tuple[Synthesizer, str]] = {
    "financial-year": (_synth_financial_year, "no financial year setup"),
    "master-calendar": (_synth_master_calendar, "no master calendar"),
    "open-periods": (_synth_open_periods, "no open periods"),
}


def render_features(gates: Iterable[str]) -> str:
    """The feature-closure config/bootstrap/features.yaml: built-in six + gates.

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
        # per-row tallies for the end summary (V24); failed drives exit 1
        self.written = 0
        self.skipped = 0
        self.failed = 0

    def _selected(self, name: str, file: str) -> bool:
        """The --only filter: row name (entity or kind) or file stem."""
        return not self.only or name in self.only or Path(file).stem in self.only

    def _progress(self, target: Path, name: str) -> None:
        """Per-row progress banner matching apply/diff (V9 / I.cmd extract)."""
        inst = self.client.instance
        output.data(f"{target} -> {inst.tenant} on {inst.base_url} ({name})")

    def _skip(self, target: Path, reason: str) -> None:
        """Report one clean per-file skip and tally it."""
        output.data(f"skip {target} ({reason})")
        self.skipped += 1

    def _skip_existing(self, target: Path) -> bool:
        """The per-file skip-if-exists gate; --force disarms it."""
        if target.exists() and not self.force:
            self._skip(target, "exists")
            return True
        return False

    def _row_failed(self, name: str, target: Path, err: RuntimeError) -> None:
        """Classify one row's live-read failure (V24: isolation, not abort).

        A PXSetupNotEnteredException 500 is the virgin-tenant empty-state
        class — the screen has no data to extract, same answer as 200 [] —
        so it skips clean. Anything else is a reported row failure; the
        run continues to the next catalog row either way.
        """
        if SETUP_NOT_ENTERED_500 in str(err):
            self._skip(target, "screen setup not entered")
            return
        output.error(f"{name}: {err}")
        self.failed += 1

    def _emit(self, target: Path, text: str, count: int) -> None:
        """Write one destination file, or report what would be written."""
        if self.dry_run:
            output.data(f"would write {target} ({count} records)")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
            output.data(f"write {target} ({count} records)")
        self.written += 1

    def entities(self) -> set[str]:
        """The entity pass; returns destination files written (or would be)."""
        produced: set[str] = set()
        active_name, active_entities = active_bootstrap()
        for spec in self.manifest.entities:
            if not self._selected(spec.entity, spec.file):
                continue
            target = self.out_dir / spec.file
            # Banner before any outcome (write/skip/would write) — apply shape
            self._progress(target, spec.entity)
            if self._skip_existing(target):
                continue
            # Hybrid contract (T69/T81): a bootstrap-endpoint row for an
            # entity the active package does not serve cannot be read -
            # skip clean rather than 404 (data-repo override may trim the
            # packaged full company surface).
            resolved = resolve_endpoint(
                spec.endpoint, api_version=self.client.instance.api_version
            )
            if resolved == active_name and spec.entity not in active_entities:
                self._skip(target, "entity not in active Bootstrap contract")
                continue
            try:
                live = _fetch(self.client, spec)
                records = _shape(spec, live)
            except RuntimeError as err:
                self._row_failed(spec.entity, target, err)
                continue
            if not live:
                self._skip(target, "no records")
                continue
            self._emit(target, _render(spec, records), len(records))
            produced.add(spec.file)
        return produced

    def setup(self) -> None:
        """The setup/ pass: synthesize each action file back from live state."""
        for synth in self.manifest.setup:
            if not self._selected(synth.kind, synth.file):
                continue
            target = self.out_dir / synth.file
            self._progress(target, synth.kind)
            if self._skip_existing(target):
                continue
            synthesize, skip_reason = SYNTHESIZERS[synth.kind]
            try:
                doc = synthesize(self.client)
            except RuntimeError as err:
                self._row_failed(synth.kind, target, err)
                continue
            if doc is None:
                self._skip(target, skip_reason)
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
        self._progress(target, "features")
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
) -> int:
    """Extract the catalog file set plus the feature closure under out_dir.

    Paths hard-cut under config/ SEED_DIRS (V30). Per file: skip when it
    exists (--force overwrites), skip when the tenant has no records,
    report-only under --dry-run. `only` filters rows by entity name,
    synthesizer kind, or file stem.

    A failing row is reported and the run continues (V24) - the return
    value is the failed-row count, 0 when every row wrote or skipped clean
    (the caller's exit-1 signal).
    """
    extraction = _Extraction(client, out_dir, only, force, dry_run)
    produced = extraction.entities()
    extraction.setup()
    extraction.features(produced)
    counts = f"{extraction.written} written, {extraction.skipped} skipped"
    suffix = " (dry run)" if dry_run else ""
    if extraction.failed:
        output.error(f"{counts}, {extraction.failed} failed{suffix}")
    else:
        output.success(f"{counts}{suffix}")
    return extraction.failed
