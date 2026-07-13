"""Reference data as code: baseline/*.yaml applied to the live tenant.

apply = entity PUTs (upsert by key); diff = live-vs-source comparison
(the drift proof).

Baseline file format:

    entity: Currency          # entity name in the contract endpoint
    key: CurrencyID           # key field(s), string or list
    endpoint: Bootstrap/1.7.0 # optional: override the instance endpoint
    records:
      - CurrencyID: "CAD"
        Description: Canadian Dollar

A record field holding a LIST is a detail array (T60) - each row a field
map, PUT with the whole record (the list itself never value-wrapped).
Every list field needs a detail_keys entry naming the field that
identifies its rows; diff matches rows by that key, order-insensitive,
and unlike top-level records an extra live detail row IS drift - the
record owns its list:

    entity: KitSpecification
    key: [KitInventoryID, RevisionID]
    detail_keys: { StockComponents: ComponentID }
    records:
      - KitInventoryID: GW-EDGE
        RevisionID: V1
        StockComponents:
          - { ComponentID: MB-CM4, ComponentQty: 1 }

Action file format (setup/*.yaml) - desired state realized by a contract
action plus a done_when live-state probe, for setup verbs a keyed PUT
cannot express (calendar generation and the like):

    action: GenerateCalendar          # action name on the endpoint entity
    entity: MasterCalendar            # entity the action hangs off
    endpoint: Bootstrap/1.7.0         # optional: override the instance endpoint
    record:     { FinancialYear: 2026 }
    parameters: { FromYear: 2026, ToYear: 2026 }   # optional
    done_when:  { filter: "FinancialYear eq '2026'" }

done_when's entity/endpoint default to the action's own. The probe is
coarse present/absent (an action leaves no keyed record to field-diff)
and gates both directions (V4): apply skips on non-empty, diff drifts on
empty. One record per file - multiple invocations author as multiple
numbered files.

`endpoint:` stops being optional when `entity` names one the packaged
Bootstrap endpoint serves (V20): the same name can mean different screens
per endpoint (B8 - Bootstrap Currency = CM202000 financial currency,
Default Currency = CM201000 list), so an ambiguous file is a hard error,
never a silent Default-endpoint PUT.
"""

import xml.etree.ElementTree as ET
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .client import OPTIMIZATION_500, AcumaticaClient, unwrap
from .config import Instance
from .models import Model, validation_summary


def _bootstrap_endpoint() -> tuple[str, frozenset[str]]:
    """Endpoint name/version + entity names from the packaged template.

    Parsed from bootstrap_project.xml rather than hand-listed (V2): the
    template is the single source of truth for what the Bootstrap endpoint
    serves, and the set tracks entity additions and version bumps for free.
    """
    root = ET.fromstring(
        (resources.files("acumatica_cli") / "bootstrap_project.xml").read_bytes()
    )
    ns = "{http://www.acumatica.com/entity/maintenance/5.31}"
    endpoint = root.find(f"EntityEndpoint/{ns}Endpoint")
    if endpoint is None:
        raise RuntimeError("bootstrap_project.xml: no EntityEndpoint/Endpoint item")
    name = f"{endpoint.get('name')}/{endpoint.get('version')}"
    entities = frozenset(
        e.get("name", "") for e in endpoint.findall(f"{ns}TopLevelEntity")
    )
    return name, entities


BOOTSTRAP_ENDPOINT, BOOTSTRAP_ENTITIES = _bootstrap_endpoint()
# The code-default instance endpoint, for the V20 error message - read off
# the Instance field default rather than hand-synced (V11: one spelling).
_DEFAULT_ENDPOINT: str = f"Default/{Instance.model_fields['api_version'].default}"


class BaselineFile(Model):
    """A parsed baseline YAML: one entity, its key fields, its records."""

    path: Path
    entity: str
    keys: list[str] = Field(alias="key")
    records: list[dict[str, Any]]
    endpoint: str | None = None  # bootstrap YAML targets the custom endpoint
    detail_keys: dict[str, str] | None = None  # {ListField: RowKeyField} (T60)

    @field_validator("keys", mode="before")
    @classmethod
    def _key_as_list(cls, v: object) -> object:
        return v if isinstance(v, list) else [v]

    @model_validator(mode="after")
    def _keys_identify_records(self) -> "BaselineFile":
        # V25: the declared key tuple must uniquely identify records - a
        # dup-keyed file diffs as permanent false drift and apply collapses
        # the dup records into one PUT target (B21)
        seen: set[tuple[str, ...]] = set()
        for i, record in enumerate(self.records):
            for k in self.keys:
                if k not in record:
                    raise ValueError(f"records[{i}] missing key field '{k}'")
            ident = tuple(str(record[k]) for k in self.keys)
            if ident in seen:
                raise ValueError(
                    f"entity '{self.entity}': records[{i}] duplicates key "
                    f"tuple [{', '.join(ident)}] - the declared key must "
                    "identify each record"
                )
            seen.add(ident)
            self._check_details(i, record)
        return self

    def _check_details(self, i: int, record: dict[str, Any]) -> None:
        # T60, the V25 sibling for detail arrays: every list field needs a
        # detail_keys entry (diff cannot match rows without one) and that
        # key must identify each source row - a dup diffs as permanent
        # false drift exactly like B21's top-level class
        for field, value in record.items():
            if not isinstance(value, list):
                continue
            key = (self.detail_keys or {}).get(field)
            if key is None:
                raise ValueError(
                    f"entity '{self.entity}': records[{i}].{field} is a "
                    "detail list but has no detail_keys entry - add "
                    f"detail_keys: {{{field}: <RowKeyField>}}"
                )
            seen_rows: set[str] = set()
            for j, row in enumerate(value):
                if not isinstance(row, dict) or key not in row:
                    raise ValueError(
                        f"entity '{self.entity}': records[{i}].{field}[{j}] "
                        f"missing detail key field '{key}'"
                    )
                ident = str(row[key])
                if ident in seen_rows:
                    raise ValueError(
                        f"entity '{self.entity}': records[{i}].{field}[{j}] "
                        f"duplicates detail key [{ident}] - the detail key "
                        "must identify each row"
                    )
                seen_rows.add(ident)


class DoneProbe(Model):
    """The done_when live-state probe: entity/endpoint default to the action's."""

    entity: str | None = None
    endpoint: str | None = None
    filter: str | None = None


class ActionFile(Model):
    """A parsed action YAML: one contract action, its payloads, its probe."""

    path: Path
    action: str
    entity: str
    record: dict[str, Any]
    parameters: dict[str, Any] | None = None
    endpoint: str | None = None
    done_when: DoneProbe


def load_baseline(path: Path) -> BaselineFile | ActionFile:
    """Parse and validate one seed YAML file, dispatching on the action: key."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    kind = ActionFile if "action" in data else BaselineFile
    try:
        parsed = kind.model_validate({"path": path, **data})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc
    if parsed.endpoint is None and parsed.entity in BOOTSTRAP_ENTITIES:
        raise SystemExit(
            f"{path}: entity '{parsed.entity}' is served by both the instance "
            f"default endpoint ({_DEFAULT_ENDPOINT}) and the packaged "
            f"{BOOTSTRAP_ENDPOINT} - add an explicit 'endpoint:' line to pick one"
        )
    return parsed


def _norm(value: Any) -> str:
    """Comparable form: bools case-folded, numbers by value, rest stringified.

    Numbers compare by value, not spelling - a YAML `0` against the
    endpoint's `0.0` (DecimalValue fields come back as floats) is not
    drift (T13).
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return repr(float(value))
    return str(value).strip()


def _filter_for(record: dict[str, Any], keys: list[str]) -> str:
    # callers pass single-view key sets only: a conjunction spanning the
    # entity's views answers 200 [] (B14), so _fetch filters on the first
    # key alone and matches the rest client-side. Literal typing follows
    # the YAML scalar type: bools and numbers travel bare - a quoted
    # 'false' against Edm.Boolean answers 500 "binary operator with
    # incompatible types" (T61 - INPreferences keyed on HoldEntry), and
    # numeric Edm types are the same class. Strings stay quoted, so
    # numeric-looking codes ('000000') are unaffected.
    def literal(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int | float):
            return str(value)
        return f"'{value}'"

    return " and ".join(f"{k} eq {literal(record[k])}" for k in keys)


def _expand_paths(record: dict[str, Any], prefix: str = "") -> list[str]:
    """$expand paths a record's shape demands (T60/T65).

    A list field is a detail array - expands by name; a dict field is a
    linked entity - expands by name plus the slash path of every nested
    dict (`MainContact`, `MainContact/Address`).
    """
    paths: list[str] = []
    for field, value in record.items():
        if isinstance(value, list):
            paths.append(f"{prefix}{field}")
        elif isinstance(value, dict):
            paths.append(f"{prefix}{field}")
            paths.extend(_expand_paths(value, f"{prefix}{field}/"))
    return sorted(paths)


def _probe(client: AcumaticaClient, action: ActionFile) -> bool:
    """Run the done_when live-state probe; non-empty = action already realized.

    A live probe, never a marker (V4) - a marker outlives state loss, the
    probe answers whether the state the action creates exists right now.
    """
    probe = action.done_when
    params = {"$filter": probe.filter} if probe.filter else None
    live = client.get_list(
        probe.entity or action.entity,
        params=params,
        endpoint=probe.endpoint or action.endpoint,
    )
    return bool(live)


def _apply_action(
    client: AcumaticaClient, action: ActionFile, dry_run: bool = False
) -> int:
    """Invoke the action unless done_when already verifies the desired state."""
    if dry_run:
        output.data(f"  would invoke {action.action}")
    elif _probe(client, action):
        output.data(f"  skip {action.action} (already done)")
    else:
        client.invoke(
            action.entity,
            action.action,
            action.record,
            action.parameters,
            action.endpoint,
        )
        output.data(f"  invoke {action.action} [{action.entity}]")
    return 1


def apply(
    client: AcumaticaClient, baseline: BaselineFile | ActionFile, dry_run: bool = False
) -> int:
    """PUT every record (upsert by key); an action file invokes its action.

    Returns the record count.
    """
    if isinstance(baseline, ActionFile):
        return _apply_action(client, baseline, dry_run)
    for record in baseline.records:
        label = ", ".join(str(record[k]) for k in baseline.keys)
        if dry_run:
            output.data(f"  would PUT {baseline.entity} [{label}]")
        else:
            body = record
            if any(isinstance(v, list) for v in record.values()):
                body = _with_detail_ids(client, baseline, record)
            client.put(baseline.entity, body, endpoint=baseline.endpoint)
            output.data(f"  PUT {baseline.entity} [{label}]")
    return len(baseline.records)


def _with_detail_ids(
    client: AcumaticaClient, baseline: BaselineFile, record: dict[str, Any]
) -> dict[str, Any]:
    """Source record + live detail-row ids — the detail upsert handle (T60).

    The contract API matches detail rows by row GUID only: a re-PUT
    without ids re-INSERTS every row (live-verified on KitSpecification —
    500 "Component Item must be unique"). So apply pre-fetches the live
    record, injects each matching live row's `id` (matched by the
    detail_keys field), and appends `{id, delete: true}` rows for live
    rows the source no longer claims — the record owns its list (V4
    detail semantics), so apply converges exactly what diff would flag.
    Record absent live → first PUT creates, rows travel id-less.
    """
    live = _fetch(client, baseline, record)
    if live is None:
        return record
    out = dict(record)
    for field, rows in record.items():
        if not isinstance(rows, list):
            continue
        key = (baseline.detail_keys or {})[field]  # load-validated
        live_ids: dict[str, Any] = {}
        for live_row in live.get(field) or []:
            value = live_row.get(key)
            if isinstance(value, dict) and "value" in value and "id" in live_row:
                live_ids[_norm(value["value"])] = live_row["id"]
        merged: list[dict[str, Any]] = []
        for row in rows:
            row_id = live_ids.pop(_norm(row[key]), None)
            merged.append({**row, "id": row_id} if row_id is not None else row)
        merged.extend({"id": row_id, "delete": True} for row_id in live_ids.values())
        out[field] = merged
    return out


def _fetch(
    client: AcumaticaClient, baseline: BaselineFile, record: dict[str, Any]
) -> dict[str, Any] | None:
    """The live record matching the source record's keys, or None.

    Primary read = list GET by $filter on the FIRST key field only, any
    remaining key fields matched client-side: a $filter conjunction that
    spans the entity's views answers 200 [] while each predicate alone
    matches (B14), so a multi-key filter can never be trusted - the first
    key names a primary-view field by seed-file convention (B21, the
    multi-org LedgerCompany read). Single-key files behave as before.

    Entities mapping a BQL-delegate view (Bootstrap Currency GL fields ->
    CuryRecords, B9) 500 on that optimized export; the key-URL
    single-record GET skips the optimizer, so diff falls back to it on
    exactly that error (V4: read-back must survive delegate-view entities).

    Detail arrays and linked entities only travel under $expand
    (T60/T65): without it every GET answers top-level scalars alone,
    diff would report each source detail row or nested field missing
    and apply's id-injection would see nothing to match. The expand set
    derives from the record's own shape - a list field expands by name,
    a dict field by its slash path (`MainContact/Address`).
    """
    params = {"$filter": _filter_for(record, baseline.keys[:1])}
    expand = _expand_paths(record)
    if expand:
        params["$expand"] = ",".join(expand)
    try:
        live = client.get_list(
            baseline.entity, params=params, endpoint=baseline.endpoint
        )
    except RuntimeError as err:
        if OPTIMIZATION_500 not in str(err):
            raise
        return client.get_record(
            baseline.entity,
            [record[k] for k in baseline.keys],
            baseline.endpoint,
            params={"$expand": params["$expand"]} if expand else None,
        )
    for row in live:
        actual = unwrap(row)
        if all(
            k in actual and _norm(actual[k]) == _norm(record[k])
            for k in baseline.keys[1:]
        ):
            return row
    return None


def diff(client: AcumaticaClient, baseline: BaselineFile | ActionFile) -> list[str]:
    """Compare each source record against the live tenant.

    Returns human-readable drift lines (empty = no drift). An action file
    diffs through its done_when probe - coarse present/absent (V4): a
    tenant that lost the action's effect must not diff false-green, but an
    action leaves no keyed record to compare field by field.
    """
    if isinstance(baseline, ActionFile):
        if _probe(client, baseline):
            return []
        return [f"action {baseline.action}: not applied"]
    drifts: list[str] = []
    for record in baseline.records:
        label = (
            f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
        )
        live = _fetch(client, baseline, record)
        if live is None:
            drifts.append(f"{label}: missing on tenant")
            continue
        actual = unwrap(live)
        for field, expected in record.items():
            if isinstance(expected, list):
                key = (baseline.detail_keys or {})[field]  # load-validated
                live_rows = actual.get(field, [])
                drifts.extend(_diff_details(label, field, key, expected, live_rows))
            elif isinstance(expected, dict):
                drifts.extend(
                    _diff_nested(f"{label}.{field}", expected, actual.get(field))
                )
            elif field not in actual:
                drifts.append(f"{label}.{field}: not returned by endpoint")
            elif _norm(actual[field]) != _norm(expected):
                drifts.append(
                    f"{label}.{field}: source={expected!r} live={actual[field]!r}"
                )
    return drifts


def _diff_nested(path: str, expected: dict[str, Any], live: Any) -> list[str]:
    """Linked-entity drift (T65): recurse source-side fields only."""
    if not isinstance(live, dict):
        return [f"{path}: not returned by endpoint"]
    drifts: list[str] = []
    for field, want in expected.items():
        if isinstance(want, dict):
            drifts.extend(_diff_nested(f"{path}.{field}", want, live.get(field)))
        elif field not in live:
            drifts.append(f"{path}.{field}: not returned by endpoint")
        elif _norm(live[field]) != _norm(want):
            drifts.append(f"{path}.{field}: source={want!r} live={live[field]!r}")
    return drifts


def _diff_details(
    label: str,
    field: str,
    key: str,
    expected: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
) -> list[str]:
    """Detail-array drift (T60): rows matched by detail key, order-insensitive.

    Unlike top-level records (V4 exemption), an extra live detail row IS
    drift - the record owns its list, apply cannot converge a live row the
    source never claimed. Within a matched row only source-side fields
    compare (server-derived LineNbr and ids stay omitted from source).
    """
    drifts: list[str] = []
    live_by_key = {_norm(row[key]): row for row in live_rows if key in row}
    for row in expected:
        ident = _norm(row[key])
        live_row = live_by_key.pop(ident, None)
        if live_row is None:
            drifts.append(f"{label}.{field}[{row[key]}]: missing on tenant")
            continue
        for sub, want in row.items():
            if sub not in live_row:
                drifts.append(
                    f"{label}.{field}[{row[key]}].{sub}: not returned by endpoint"
                )
            elif _norm(live_row[sub]) != _norm(want):
                drifts.append(
                    f"{label}.{field}[{row[key]}].{sub}: "
                    f"source={want!r} live={live_row[sub]!r}"
                )
    for ident in live_by_key:
        drifts.append(f"{label}.{field}[{ident}]: extra on tenant")
    return drifts
