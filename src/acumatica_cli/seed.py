"""Reference data as code: baseline/*.yaml applied to the live tenant.

apply = entity PUTs (upsert by key); diff = live-vs-source comparison
(the drift proof).

Baseline file format:

    entity: Currency          # entity name in the contract endpoint
    key: CurrencyID           # key field(s), string or list
    endpoint: Bootstrap/1.0.0 # optional: override the instance endpoint
    records:
      - CurrencyID: "CAD"
        Description: Canadian Dollar

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


def load_baseline(path: Path) -> BaselineFile:
    """Parse and validate one baseline YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    try:
        baseline = BaselineFile.model_validate({"path": path, **data})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc
    if baseline.endpoint is None and baseline.entity in BOOTSTRAP_ENTITIES:
        raise SystemExit(
            f"{path}: entity '{baseline.entity}' is served by both the instance "
            f"default endpoint ({_DEFAULT_ENDPOINT}) and the packaged "
            f"{BOOTSTRAP_ENDPOINT} - add an explicit 'endpoint:' line to pick one"
        )
    return baseline


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


def apply(
    client: AcumaticaClient, baseline: BaselineFile, dry_run: bool = False
) -> int:
    """PUT every record (upsert by key). Returns the record count."""
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


def diff(client: AcumaticaClient, baseline: BaselineFile) -> list[str]:
    """Compare each source record against the live tenant.

    Returns human-readable drift lines (empty = no drift).
    """
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
