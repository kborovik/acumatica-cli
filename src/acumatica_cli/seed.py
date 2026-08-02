"""Reference data as code: baseline/*.yaml applied to the live tenant.

apply = entity PUTs (upsert by key); diff = live-vs-source comparison
(the drift proof).

Baseline file format:

    entity: Currency          # entity name in the contract endpoint
    key: CurrencyID           # key field(s), string or list
    endpoint: bootstrap       # optional: literal Bootstrap/<ver> or symbolic
    records:                  # bootstrap -> active package version (V20)
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
    endpoint: bootstrap               # optional: literal or symbolic (V20)
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

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import bootstrap, output
from .client import OPTIMIZATION_500, AcumaticaClient, unwrap
from .config import find_data_root
from .models import Model, validation_summary

# Packaged Bootstrap contract (V2/V21/T178 package SoT) — full company surface.
# active_bootstrap() rejects a present data-repo project.xml then returns
# the same packaged endpoint/entity set.
BOOTSTRAP_ENDPOINT, BOOTSTRAP_ENTITIES = bootstrap.parse_endpoint(
    bootstrap.packaged_contract_xml()
)
# Dual-serve error text prefers the symbolic Default form (V20) so operators
# do not read the code-default version as their configured api_version.
_DEFAULT_ENDPOINT_NAME = "Default"
_SYMBOLIC_BOOTSTRAP = "bootstrap"
_SYMBOLIC_DEFAULT = "default"
# Mid-session branch-selector failure (V5/B24): virgin-tenant apply opens
# the REST session pre-Company; INPreferences TransitBranchID (and kin)
# 500 with this message until a fresh login sees the new branch.
_BRANCH_EMPTY = "'Branch' cannot be empty"
# User seed password material (V39): write-only on apply when present in
# YAML; extract always strips; diff never compares (GET returns hashes
# or nothing — never a re-seedable value).
PASSWORD_FIELDS = frozenset({"Password", "b64__Password"})
# NumberingSequence runtime counters (V40/gh #25): bounds-only seed;
# LastNbr (+ advanced counter if ever exposed) = issued progress, not
# desired config. Extract hard-strips; diff never compares; apply never
# PUTs them (re-apply must not reset live counters).
NUMBERING_RUNTIME_FIELDS = frozenset({"LastNbr"})
# Fields omitted from source↔live compare (V39 write-only + V40 runtime).
_DIFF_IGNORE_FIELDS = PASSWORD_FIELDS | NUMBERING_RUNTIME_FIELDS


def active_bootstrap(root: Path | None = None) -> tuple[str, frozenset[str]]:
    """Active Bootstrap endpoint name/version + entity set (V2/V20/V21).

    Always the packaged full company contract. Present data-repo
    ``project.xml`` → hard error (package SoT). ``root`` defaults to the
    cwd walk-up discovery root (same as features.yaml).
    """
    if root is None:
        root = find_data_root()
    return bootstrap.parse_endpoint(bootstrap.load_contract_xml(root))


def resolve_endpoint(
    endpoint: str | None,
    root: Path | None = None,
    *,
    api_version: str | None = None,
) -> str | None:
    """Map symbolic endpoint names to versioned paths (V20).

    ``bootstrap`` → active ``Bootstrap/<ver>`` from contract XML (load-time
    what). ``default`` → ``Default/<api_version>``; ``api_version`` required
    when endpoint is default. Other values pass through (literals or None).

    Apply/diff primarily resolve symbolic default in ``client._url``; this
    helper serves extract pre-HTTP path building and offline unit tests.
    """
    if endpoint == _SYMBOLIC_BOOTSTRAP:
        return active_bootstrap(root)[0]
    if endpoint == _SYMBOLIC_DEFAULT:
        if not api_version:
            raise SystemExit(
                "endpoint: default requires a configured api_version "
                "(target.yaml default_api or --api-version)"
            )
        return f"{_DEFAULT_ENDPOINT_NAME}/{api_version}"
    return endpoint


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
    def _keys_identify_records(self) -> BaselineFile:
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
    """Parse and validate one seed YAML file, dispatching on the action: key.

    Symbolic ``endpoint: bootstrap`` resolves to the packaged contract
    version at load (V20/V21/T178 package SoT).
    Symbolic ``endpoint: default`` stays on the model and resolves at HTTP
    time via ``client._url`` (never load-rewritten — version is where).
    An entity the active Bootstrap contract serves still needs an explicit
    endpoint (literal or symbolic); silent Default-endpoint PUTs are the
    B8 class.
    """
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    kind = ActionFile if "action" in data else BaselineFile
    try:
        parsed = kind.model_validate({"path": path, **data})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc
    name, entities = active_bootstrap()
    if parsed.endpoint == _SYMBOLIC_BOOTSTRAP:
        parsed = parsed.model_copy(update={"endpoint": name})
    elif parsed.endpoint is None and parsed.entity in entities:
        raise SystemExit(
            f"{path}: entity '{parsed.entity}' is served by both "
            f"Default (use endpoint: default -> Default/<api_version>) "
            f"and the active {name} - add an explicit 'endpoint:' line to "
            f"pick one (literal or symbolic 'bootstrap' | 'default')"
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
) -> tuple[int, list[str]]:
    """Invoke the action unless done_when already verifies the desired state.

    On invoke failure, returns ``(0, [error])`` rather than raising so the
    caller can continue other files (V45 multi-error summary).
    """
    if dry_run:
        output.data(f"  would invoke {action.action}")
        return 1, []
    if _probe(client, action):
        output.data(f"  skip {action.action} (already done)")
        return 1, []
    try:
        client.invoke(
            action.entity,
            action.action,
            action.record,
            action.parameters,
            action.endpoint,
        )
    except RuntimeError as err:
        msg = f"action {action.action} [{action.entity}]: {err}"
        output.error(msg)
        return 0, [msg]
    output.data(f"  invoke {action.action} [{action.entity}]")
    return 1, []


def apply(
    client: AcumaticaClient, baseline: BaselineFile | ActionFile, dry_run: bool = False
) -> tuple[int, list[str]]:
    """PUT every record (upsert by key); an action file invokes its action.

    After the first successful Company PUT this session, re-login once so
    branch selectors resolve for later files (V5/B24). A PUT that still
    500s with ``'Branch' cannot be empty`` gets one re-login + retry.

    Password fields (V39/T148): write-only on virgin create when present in
    seed; warm re-apply (record already live) strips them so identity
    re-PUT does not reset passwords.

    Numbering runtime fields (V40/T152): never PUT ``LastNbr`` (or kin) so
    apply of bounds never resets live counters.

    Per-record failure isolation (V45): a failed PUT does not abort later
    records in the same file. Returns ``(ok_count, error_messages)`` —
    never silent partial; caller exits 1 when any errors remain. Exit 2
    stays drift (``diff`` only).
    """
    if isinstance(baseline, ActionFile):
        return _apply_action(client, baseline, dry_run)
    ok = 0
    errors: list[str] = []
    for record in baseline.records:
        label = ", ".join(str(record[k]) for k in baseline.keys)
        if dry_run:
            output.data(f"  would PUT {baseline.entity} [{label}]")
            ok += 1
            continue
        try:
            body = _put_body(client, baseline, record)
            _put(client, baseline.entity, body, baseline.endpoint)
            if baseline.entity == "Company":
                client.refresh_after_company()
            output.data(f"  PUT {baseline.entity} [{label}]")
            ok += 1
        except RuntimeError as err:
            msg = f"{baseline.entity} [{label}]: {err}"
            output.error(msg)
            errors.append(msg)
    return ok, errors


def _put(
    client: AcumaticaClient,
    entity: str,
    body: dict[str, Any],
    endpoint: str | None,
) -> None:
    """PUT one record; one re-login + retry on branch-empty 500 (V5/B24)."""
    try:
        client.put(entity, body, endpoint=endpoint)
    except RuntimeError as err:
        if _BRANCH_EMPTY not in str(err):
            raise
        output.info(f"re-login and retry {entity} (branch empty)")
        client.relogin()
        client.put(entity, body, endpoint=endpoint)


def _put_body(
    client: AcumaticaClient, baseline: BaselineFile, record: dict[str, Any]
) -> dict[str, Any]:
    """Build the PUT body: detail-id merge + warm password strip (T60/V39/T148).

    One live fetch when the record has detail lists and/or password fields.
    Virgin (no live) keeps seed Password; warm drops password material so
    re-apply is identity-only (package User seed never needs a password).

    Always drops numbering runtime counters (V40/T152) so bounds apply never
    resets live ``LastNbr`` even if hand-authored seed included them.
    """
    has_details = any(isinstance(v, list) for v in record.values())
    has_password = any(k in record for k in PASSWORD_FIELDS)
    live: dict[str, Any] | None = None
    if has_details or has_password:
        live = _fetch(client, baseline, record)
    if has_details and live is not None:
        body = _merge_detail_ids(baseline, record, live)
    else:
        body = dict(record)
    if live is not None and has_password:
        body = {k: v for k, v in body.items() if k not in PASSWORD_FIELDS}
    # V40: never send runtime numbering counters (field-name deny, any entity).
    return {k: v for k, v in body.items() if k not in NUMBERING_RUNTIME_FIELDS}


def _merge_detail_ids(
    baseline: BaselineFile,
    record: dict[str, Any],
    live: dict[str, Any],
) -> dict[str, Any]:
    """Inject live detail-row GUIDs into a source record (T60).

    The contract API matches detail rows by row GUID only: a re-PUT
    without ids re-INSERTS every row (live-verified on KitSpecification —
    500 "Component Item must be unique"). Matched source rows gain the
    live ``id``; live rows the source no longer claims ride as
    ``{id, delete: true}`` — the record owns its list (V4 detail
    semantics). Caller supplies the already-fetched live entity.
    """
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
        # V39/V40: never compare write-only password or runtime numbering counters
        fields = {k: v for k, v in record.items() if k not in _DIFF_IGNORE_FIELDS}
        for field, expected in fields.items():
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
        if field in _DIFF_IGNORE_FIELDS:
            continue
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
            if sub in _DIFF_IGNORE_FIELDS:
                continue
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
