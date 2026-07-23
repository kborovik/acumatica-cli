"""Transaction scenarios: scenario/*.yaml executed against the live tenant.

`acu run` is the third data-plane verb (SPEC I.cmd): apply/diff own
configuration (keyed idempotent upserts), run owns transactions - the
server assigns document numbers, so a scenario is executed forward,
never upserted. The scenario file is the contract for the future AI
scenario generator: declarative steps, checkable expectations.

Scenario file format (SPEC I.data):

    scenario: buy-sell
    description: Buy finished goods -> sell -> collect
    steps:
      - id: po-gateways
        put: PurchaseOrder
        record:
          VendorID: SHENZHEN
          Details:
            - { InventoryID: GW-EDGE, OrderQty: 10 }
        capture: { OrderNbr: po_gateways }     # server-assigned -> ${var}
      - id: release
        action: { entity: PurchaseReceipt, name: ReleasePurchaseReceipt }
        record: { ReceiptNbr: "${rcpt}" }
        wait:                                   # poll until field match
          entity: PurchaseReceipt
          keys: ["${rcpt}"]
          until: { Status: Released }
    expect:
      - get: { entity: Payment, keys: [Payment, "${pmt}"] }
        fields: { Status: Closed }
      - inquire: AccountSummaryInquiry          # delta = post - pre
        parameters: { Ledger: ACTUAL, Period: "062026" }
        match: { Account: "40000" }
        delta: { EndingBalance: 4138.00 }

Steps run in order; `capture` lifts server-assigned fields into ${var}
tokens for later steps. `expect` delta assertions snapshot before the
first step and re-probe after the last, comparing the difference - the
scenario re-runs safely on a warm tenant (document numbers differ, the
deltas hold). `get` assertions are absolute (statuses of documents
created in this run). Exit 0 = every expectation holds; 1 = any step
error or expectation miss (2 stays diff's drift code).
"""

import re
import time
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, ValidationError, model_validator

from . import output
from .client import AcumaticaClient, unwrap
from .models import Model, validation_summary
from .seed import _norm  # pyright: ignore[reportPrivateUsage]

_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_PATH = re.compile(r"([A-Za-z0-9_]+)(?:\[(\d+)\])?")


class ActionSpec(Model):
    """The contract action a step invokes: entity + action name."""

    entity: str
    name: str


class WaitSpec(Model):
    """Poll a record by key URL until every `until` field matches."""

    entity: str
    keys: list[Any]
    until: dict[str, Any]
    timeout: float = 120.0
    endpoint: str | None = None


class GetOp(Model):
    """Key-URL fetch a get step performs; expand pulls detail arrays."""

    entity: str
    keys: list[Any]
    expand: list[str] | None = None
    endpoint: str | None = None


class Step(Model):
    """One scenario step: exactly one op (put | action | get | wait-only)."""

    id: str
    put: str | None = None
    action: ActionSpec | None = None
    get: GetOp | None = None
    record: dict[str, Any] | None = None
    parameters: dict[str, Any] | None = None
    capture: dict[str, str] | None = None  # {Field-or-path: var_name}
    wait: WaitSpec | None = None
    endpoint: str | None = None

    @model_validator(mode="after")
    def _one_op(self) -> Step:
        ops = [op for op in (self.put, self.action, self.get) if op is not None]
        if len(ops) > 1:
            raise ValueError(f"step '{self.id}': put, action, get are exclusive")
        if not ops and self.wait is None:
            raise ValueError(f"step '{self.id}': needs one of put, action, get, wait")
        if self.put is not None and self.record is None:
            raise ValueError(f"step '{self.id}': put needs a record")
        if self.action is not None and self.record is None:
            raise ValueError(f"step '{self.id}': action needs a record")
        if self.capture is not None and self.put is None and self.get is None:
            raise ValueError(f"step '{self.id}': capture rides a put or get step")
        return self


class GetSpec(Model):
    """Key-URL record address for a get expectation."""

    entity: str
    keys: list[Any]
    endpoint: str | None = None


class Expect(Model):
    """One expectation: absolute `get` fields or `inquire` deltas."""

    get: GetSpec | None = None
    fields: dict[str, Any] | None = None
    inquire: str | None = None
    parameters: dict[str, Any] | None = None
    match: dict[str, Any] | None = None
    delta: dict[str, float] | None = None
    endpoint: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Expect:
        if (self.get is None) == (self.inquire is None):
            raise ValueError("expect: exactly one of get, inquire")
        if self.get is not None and self.fields is None:
            raise ValueError("expect: get needs fields")
        if self.inquire is not None and self.delta is None:
            raise ValueError("expect: inquire needs delta")
        return self

    def label(self) -> str:
        """Human-readable expectation address for report lines."""
        if self.get is not None:
            keys = ", ".join(str(k) for k in self.get.keys)
            return f"{self.get.entity} [{keys}]"
        match = ""
        if self.match:
            match = f" [{', '.join(f'{k}={v}' for k, v in self.match.items())}]"
        return f"{self.inquire}{match}"


class Scenario(Model):
    """A parsed scenario YAML: named steps plus expectations."""

    path: Path
    scenario: str
    description: str | None = None
    steps: list[Step]
    expect: list[Expect] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Scenario:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id '{step.id}'")
            seen.add(step.id)
        return self


def load_scenario(path: Path) -> Scenario:
    """Parse and validate one scenario YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    try:
        return Scenario.model_validate({"path": path, **data})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def _subst(value: Any, variables: dict[str, Any]) -> Any:
    """Interpolate ${var} tokens; unknown names are a hard error."""

    def _one(text: str) -> Any:
        whole = _VAR.fullmatch(text)
        if whole:  # whole-value token keeps the captured value's type
            return _lookup(whole.group(1))
        return _VAR.sub(lambda m: str(_lookup(m.group(1))), text)

    def _lookup(name: str) -> Any:
        if name not in variables:
            raise SystemExit(
                f"unknown scenario variable '${{{name}}}' - captured so far: "
                f"{', '.join(sorted(variables)) or '(none)'}"
            )
        return variables[name]

    if isinstance(value, str):
        return _one(value)
    if isinstance(value, dict):
        return {k: _subst(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst(v, variables) for v in value]
    return value


def _resolve_path(record: dict[str, Any], path: str) -> Any:
    """Resolve a dotted/indexed capture path (`Shipments[0].ShipmentNbr`).

    Action-created documents (T66) assign numbers readable only off the
    parent's expanded detail rows - the path walks unwrapped nesting:
    a bare segment reads a field, `Seg[i]` indexes a detail array.
    Missing segments raise with the path named.
    """
    value: Any = record
    for segment in path.split("."):
        m = _PATH.fullmatch(segment)
        if m is None:
            raise RuntimeError(f"capture path {path!r}: bad segment {segment!r}")
        field, index = m.group(1), m.group(2)
        if not isinstance(value, dict) or field not in value:
            raise RuntimeError(
                f"capture path {path!r}: field {field!r} not in the record"
            )
        value = value[field]
        if index is not None:
            if not isinstance(value, list) or int(index) >= len(value):
                raise RuntimeError(
                    f"capture path {path!r}: index [{index}] out of range"
                )
            value = value[int(index)]
    return value


def _inquire(client: AcumaticaClient, expect: Expect) -> dict[str, float]:
    """Probe a contract inquiry; sum each delta field over matching rows."""
    assert expect.inquire is not None
    assert expect.delta is not None
    body = client.put(
        expect.inquire,
        expect.parameters or {},
        endpoint=expect.endpoint,
        params={"$expand": "Results"},
    )
    totals = dict.fromkeys(expect.delta, 0.0)
    for row in body.get("Results") or []:
        values = unwrap(row)
        if expect.match and any(
            field not in values or _norm(values[field]) != _norm(want)
            for field, want in expect.match.items()
        ):
            continue
        for field in totals:
            totals[field] += float(values.get(field) or 0.0)
    return totals


def _wait(client: AcumaticaClient, spec: WaitSpec, variables: dict[str, Any]) -> None:
    """Poll the record until every `until` field matches (live state, V4)."""
    keys = _subst(spec.keys, variables)
    until = _subst(spec.until, variables)
    deadline = time.monotonic() + spec.timeout
    state: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        record = client.get_record(spec.entity, keys, spec.endpoint)
        state = unwrap(record) if record is not None else None
        if state is not None and all(
            field in state and _norm(state[field]) == _norm(want)
            for field, want in until.items()
        ):
            return
        time.sleep(client.poll_interval)
    raise RuntimeError(
        f"wait timed out after {spec.timeout:g}s: {spec.entity} "
        f"{keys} never reached {until} (last state: "
        + (
            ", ".join(f"{k}={state.get(k)!r}" for k in until)
            if state is not None
            else "record missing"
        )
        + ")"
    )


def _dry_run(scenario: Scenario) -> None:
    """List steps and expectations without any HTTP."""
    for step in scenario.steps:
        op = (
            f"PUT {step.put}"
            if step.put
            else f"invoke {step.action.entity}/{step.action.name}"
            if step.action
            else f"get {step.get.entity}"
            if step.get
            else f"wait {step.wait.entity}"  # pyright: ignore[reportOptionalMemberAccess]
        )
        output.data(f"  would {op} [{step.id}]")
    for expect in scenario.expect:
        output.data(f"  would check {expect.label()}")


def _run_step(client: AcumaticaClient, step: Step, variables: dict[str, Any]) -> None:
    """Execute one step, folding captured fields into the variable set."""
    if step.put is not None:
        assert step.record is not None
        body = client.put(
            step.put, _subst(step.record, variables), endpoint=step.endpoint
        )
        output.data(f"  put {step.put} [{step.id}]")
        for field, var in (step.capture or {}).items():
            echoed = unwrap(body)
            if field not in echoed:
                raise RuntimeError(
                    f"step '{step.id}': capture field '{field}' not in the PUT response"
                )
            variables[var] = echoed[field]
    elif step.action is not None:
        assert step.record is not None
        client.invoke(
            step.action.entity,
            step.action.name,
            _subst(step.record, variables),
            _subst(step.parameters, variables),
            step.endpoint,
        )
        output.data(f"  invoke {step.action.name} [{step.id}]")
    elif step.get is not None:
        keys = _subst(step.get.keys, variables)
        params = (
            {"$expand": ",".join(sorted(step.get.expand))} if step.get.expand else None
        )
        record = client.get_record(step.get.entity, keys, step.get.endpoint, params)
        if record is None:
            raise RuntimeError(f"step '{step.id}': {step.get.entity} {keys} not found")
        fetched = unwrap(record)
        output.data(f"  get {step.get.entity} [{step.id}]")
        for path, var in (step.capture or {}).items():
            variables[var] = _resolve_path(fetched, path)
    if step.wait is not None:
        _wait(client, step.wait, variables)
        output.data(f"  wait ok [{step.id}]")


def _check_get(
    client: AcumaticaClient, expect: Expect, variables: dict[str, Any]
) -> bool:
    """Absolute field assertions on a key-URL record."""
    assert expect.get is not None
    assert expect.fields is not None
    keys = _subst(expect.get.keys, variables)
    # label from the substituted keys - the raw ${var} token is opaque
    label = f"{expect.get.entity} [{', '.join(str(k) for k in keys)}]"
    record = client.get_record(expect.get.entity, keys, expect.get.endpoint)
    state = unwrap(record) if record is not None else {}
    ok = True
    for field, want in _subst(expect.fields, variables).items():
        got = state.get(field)
        if record is None or field not in state:
            ok = False
            output.data(f"  x {label}.{field}: not found")
        elif _norm(got) != _norm(want):
            ok = False
            output.data(f"  x {label}.{field}: expected {want!r} got {got!r}")
        else:
            output.data(f"  + {label}.{field} = {want!r}")
    return ok


def _check_delta(
    client: AcumaticaClient, expect: Expect, snapshot: dict[str, float]
) -> bool:
    """Delta assertions: post-run probe minus the pre-run snapshot."""
    assert expect.delta is not None
    after = _inquire(client, expect)
    ok = True
    for field, want in expect.delta.items():
        got = after[field] - snapshot[field]
        if abs(got - want) > 0.005:
            ok = False
            output.data(
                f"  x {expect.label()}.{field}: expected delta {want:+g} got {got:+g}"
            )
        else:
            output.data(f"  + {expect.label()}.{field} delta {want:+g}")
    return ok


def run(
    client: AcumaticaClient | None, scenario: Scenario, dry_run: bool = False
) -> bool:
    """Execute the scenario; True when every step and expectation held.

    client may be None only under dry_run - parsing and step listing
    never touch HTTP (a preview costs nothing live). Delta expectations
    snapshot BEFORE the first step (V4): the comparison is post minus
    pre, so a warm tenant re-runs clean.
    """
    output.data(f"{scenario.path} -> {scenario.scenario}")
    if dry_run:
        _dry_run(scenario)
        return True
    assert client is not None  # non-dry-run callers pass a live session
    before = [
        _inquire(client, expect) if expect.inquire else {} for expect in scenario.expect
    ]
    variables: dict[str, Any] = {}
    for step in scenario.steps:
        _run_step(client, step, variables)
    ok = True
    for expect, snapshot in zip(scenario.expect, before, strict=True):
        if expect.get is not None:
            ok = _check_get(client, expect, variables) and ok
        else:
            ok = _check_delta(client, expect, snapshot) and ok
    return ok
