"""Reference data as code: baseline/*.yaml applied to the live tenant.

apply = entity PUTs (upsert by key); diff = live-vs-source comparison
(the drift proof).

Baseline file format:

    entity: Currency          # entity name in the contract endpoint
    key: CurrencyID           # key field(s), string or list
    records:
      - CurrencyID: "CAD"
        Description: Canadian Dollar
"""

from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import output
from .client import AcumaticaClient, unwrap
from .models import Model, validation_summary


class BaselineFile(Model):
    """A parsed baseline YAML: one entity, its key fields, its records."""

    path: Path
    entity: str
    keys: list[str] = Field(alias="key")
    records: list[dict[str, Any]]

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
        return BaselineFile.model_validate({"path": path, **data})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def _norm(value: Any) -> str:
    """Comparable form: booleans case-folded, everything else stringified."""
    if isinstance(value, bool):
        return str(value).lower()
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
            client.put(baseline.entity, record)
            output.data(f"  PUT {baseline.entity} [{label}]")
    return len(baseline.records)


def diff(client: AcumaticaClient, baseline: BaselineFile) -> list[str]:
    """Compare each source record against the live tenant.

    Returns human-readable drift lines (empty = no drift).
    """
    drifts: list[str] = []
    for record in baseline.records:
        label = (
            f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
        )
        live = client.get_list(
            baseline.entity, params={"$filter": _filter_for(record, baseline.keys)}
        )
        if not live:
            drifts.append(f"{label}: missing on tenant")
            continue
        actual = unwrap(live[0])
        for field, expected in record.items():
            if field not in actual:
                drifts.append(f"{label}.{field}: not returned by endpoint")
            elif _norm(actual[field]) != _norm(expected):
                drifts.append(
                    f"{label}.{field}: source={expected!r} live={actual[field]!r}"
                )
    return drifts
