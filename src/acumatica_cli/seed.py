"""Reference data as code: baseline/*.yaml applied to the live tenant.

apply = entity PUTs (upsert by key); diff = live-vs-source comparison
(the drift proof).

Baseline file format:

    entity: Currency          # entity name in the contract endpoint
    key: CurrencyID           # key field(s), string or list
    endpoint: Bootstrap/1.2.0 # optional: override the instance endpoint
    records:
      - CurrencyID: "CAD"
        Description: Canadian Dollar

Action file format (setup/*.yaml) - desired state realized by a contract
action plus a done_when live-state probe, for setup verbs a keyed PUT
cannot express (calendar generation and the like):

    action: GenerateCalendar          # action name on the endpoint entity
    entity: MasterCalendar            # entity the action hangs off
    endpoint: Bootstrap/1.2.0         # optional: override the instance endpoint
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
from .client import AcumaticaClient, unwrap
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
_DEFAULT_ENDPOINT: str = Instance.model_fields["endpoint"].default


class BaselineFile(Model):
    """A parsed baseline YAML: one entity, its key fields, its records."""

    path: Path
    entity: str
    keys: list[str] = Field(alias="key")
    records: list[dict[str, Any]]
    endpoint: str | None = None  # bootstrap YAML targets the custom endpoint

    @field_validator("keys", mode="before")
    @classmethod
    def _key_as_list(cls, v: object) -> object:
        return v if isinstance(v, list) else [v]

    @model_validator(mode="after")
    def _records_carry_keys(self) -> "BaselineFile":
        for i, record in enumerate(self.records):
            for k in self.keys:
                if k not in record:
                    raise ValueError(f"records[{i}] missing key field '{k}'")
        return self


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
    return " and ".join(f"{k} eq '{record[k]}'" for k in keys)


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
            client.put(baseline.entity, record, endpoint=baseline.endpoint)
            output.data(f"  PUT {baseline.entity} [{label}]")
    return len(baseline.records)


# The list GET's optimized-export failure (B9) - the one error diff retries
# via the key-URL single-record GET; any other error still raises
_OPTIMIZATION_500 = "Optimization cannot be performed"


def _fetch(
    client: AcumaticaClient, baseline: BaselineFile, record: dict[str, Any]
) -> dict[str, Any] | None:
    """The live record matching the source record's keys, or None.

    Primary read = list GET by $filter on the key fields. Entities mapping a
    BQL-delegate view (Bootstrap Currency GL fields -> CuryRecords, B9) 500
    on that optimized export; the key-URL single-record GET skips the
    optimizer, so diff falls back to it on exactly that error (V4: read-back
    must survive delegate-view entities).
    """
    try:
        live = client.get_list(
            baseline.entity,
            params={"$filter": _filter_for(record, baseline.keys)},
            endpoint=baseline.endpoint,
        )
        return live[0] if live else None
    except RuntimeError as err:
        if _OPTIMIZATION_500 not in str(err):
            raise
        return client.get_record(
            baseline.entity, [record[k] for k in baseline.keys], baseline.endpoint
        )


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
            if field not in actual:
                drifts.append(f"{label}.{field}: not returned by endpoint")
            elif _norm(actual[field]) != _norm(expected):
                drifts.append(
                    f"{label}.{field}: source={expected!r} live={actual[field]!r}"
                )
    return drifts
