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
      - id: find-bill                               # optional: list GET
        get: { entity: Bill, filter: "VendorRef eq 'PO-${po}'", top: 1 }
        capture: { ReferenceNbr: bill }
    expect:
      - get: { entity: Payment, keys: [Payment, "${pmt}"] }
        fields: { Status: Closed }
      - inquire: AccountSummaryInquiry          # delta = post - pre
        parameters: { Ledger: ACTUAL, Period: "${current_period}" }
        match: { Account: "40000" }
        delta: { EndingBalance: 4138.00 }

Steps run in order; `capture` lifts server-assigned fields into ${var}
tokens for later steps. Built-in ``${current_period}`` (V43) expands to
host-local ``MMyyyy`` at process start on every ``${var}`` site — steps,
expect params, and once.present params — so calendar month roll does not
require hand-editing Period pins. Unknown ``${…}`` is a hard error.
`expect` delta assertions snapshot before the first step and re-probe
after the last, comparing the difference - the scenario re-runs safely
on a warm tenant (document numbers differ, the deltas hold). `get`
assertions are absolute (statuses of documents created in this run).
`once: true` + authored `present` gate skips steps and expects when the
probe already holds: inquire-absolute (capital non-stack; V4/gh #19) or
`get` filter on an external reference (one migrated document; gh #73).
A file may hold many scenarios as YAML documents split by `---` - a
generated load of thousands of documents, each guarded on its own, so a
re-run skips what already exists and creates nothing twice.
Exit 0 = every expectation holds or once-skip; 1 = any step error or
expectation miss (2 stays diff's drift code). A step error stops the
run: later scenarios are listed as not run (gh #58).
"""

import re
import time
from collections.abc import Callable
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import Field, ValidationError, model_validator

from . import output
from .client import AcumaticaClient, SessionLostError, format_failure, unwrap
from .models import Model, validation_summary
from .seed import _norm  # pyright: ignore[reportPrivateUsage]

_VAR = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_PATH = re.compile(r"([A-Za-z0-9_]+)(?:\[(\d+)\])?")


def period_mmYYYY(d: date) -> str:  # noqa: N802  # SPEC V43 name (MMyyyy)
    """Format a calendar date as Acumatica financial period ``MMyyyy`` (V43).

    Host-local only — no ERP business-date probe. Sole format for the
    built-in ``${current_period}`` token on ``acu run``.
    """
    return f"{d.month:02d}{d.year:04d}"


def _builtin_variables(today: date | None = None) -> dict[str, Any]:
    """Built-in scenario variables available pre-steps (V43).

    ``current_period`` is pinned at process start from host-local date
    so present probes, expect params, and steps share one period string.
    """
    d = date.today() if today is None else today
    return {"current_period": period_mmYYYY(d)}


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
    """Fetch a get step performs: key-URL or OData list filter.

    Key-URL (`keys`) is the default path for known document numbers.
    `filter` is for post-action discovery (e.g. AP Bill auto-created from
    PurchaseReceipt CreateBill, found by VendorRef). Exactly one of
    `keys` / `filter`. Optional `top` / `orderby` narrow list gets;
    `expand` pulls detail arrays on either path.
    """

    entity: str
    keys: list[Any] | None = None
    filter: str | None = None
    top: int | None = None
    orderby: str | None = None
    expand: list[str] | None = None
    endpoint: str | None = None

    @model_validator(mode="after")
    def _keys_or_filter(self) -> GetOp:
        if (self.keys is None) == (self.filter is None):
            raise ValueError("get: exactly one of keys, filter")
        return self


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
    timeout: float | None = Field(default=None, gt=0)  # action 202 poll (V63)

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
        if self.timeout is not None and self.action is None:
            raise ValueError(
                f"step '{self.id}': timeout rides an action step (wait has its own)"
            )
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


class WhenPred(Model):
    """Absolute field predicate for a once-present inquire probe (eq | gte)."""

    eq: Any | None = None
    gte: float | int | None = None

    @model_validator(mode="after")
    def _one_op(self) -> WhenPred:
        if (self.eq is None) == (self.gte is None):
            raise ValueError("when field: exactly one of eq, gte")
        return self


class PresentGet(Model):
    """List-GET presence probe: any row matching ``filter`` (gh #73).

    The generated-load guard: filter on the document's external
    reference (e.g. ``CustomerOrder eq 'LEGACY-1042'``) - present when
    the tenant already holds it.
    """

    entity: str
    filter: str
    endpoint: str | None = None


class PresentSpec(Model):
    """Presence gate for ``once: true``: exactly one of inquire, get.

    ``inquire`` (V4/gh #19): author-owned inquiry + optional row ``match``
    + per-field ``when`` (eq|gte), absolute. ``get`` (gh #73): list GET
    by ``filter``, present when a row matches. Probe true → skip steps
    and expects; false → run cold. ``parameters`` / ``filter`` (and other
    ``${var}`` sites) interpolate built-ins such as ``${current_period}``
    (V43); capture ``${var}`` is not available pre-steps.
    """

    inquire: str | None = None
    get: PresentGet | None = None
    parameters: dict[str, Any] | None = None
    match: dict[str, Any] | None = None
    when: dict[str, WhenPred] | None = None
    endpoint: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> PresentSpec:
        if (self.inquire is None) == (self.get is None):
            raise ValueError("present: exactly one of inquire, get")
        if self.inquire is not None and not self.when:
            raise ValueError("present: when must name at least one field")
        if self.get is not None and (
            self.when is not None
            or self.parameters is not None
            or self.match is not None
            or self.endpoint is not None
        ):
            raise ValueError(
                "present: get takes entity, filter, endpoint only "
                "(when, parameters, match ride inquire)"
            )
        return self

    def label(self) -> str:
        """Human-readable probe address for dry-run lines."""
        if self.get is not None:
            return f"get {self.get.entity} filter {self.get.filter!r}"
        assert self.when is not None
        when = ", ".join(
            f"{field}={'gte ' + str(p.gte) if p.gte is not None else 'eq ' + str(p.eq)}"
            for field, p in self.when.items()
        )
        match = f" match {self.match}" if self.match else ""
        return f"{self.inquire}{match} when {when}"


class Scenario(Model):
    """A parsed scenario YAML: named steps plus expectations."""

    path: Path
    doc: int | None = None  # 1-based YAML document index; None = one-doc file
    scenario: str
    description: str | None = None
    steps: list[Step] = Field(default_factory=list)  # empty ok — stub (V28 30-build)
    expect: list[Expect] = Field(default_factory=list)
    once: bool = False
    present: PresentSpec | None = None

    @model_validator(mode="after")
    def _unique_step_ids(self) -> Scenario:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id '{step.id}'")
            seen.add(step.id)
        return self

    @model_validator(mode="after")
    def _once_present(self) -> Scenario:
        if self.once and self.present is None:
            raise ValueError("once: true requires present")
        if self.present is not None and not self.once:
            raise ValueError("present requires once: true")
        return self

    @property
    def label(self) -> str:
        """Report address: the path, plus ``#n`` in a multi-document file."""
        return str(self.path) if self.doc is None else f"{self.path}#{self.doc}"


# libyaml when built in: a generated load is thousands of documents (gh #73)
_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


def load_scenarios(path: Path) -> list[Scenario]:
    """Parse and validate every scenario in a YAML file (``---`` split).

    A one-document file yields one scenario labelled by its path; a
    multi-document file labels each ``<path>#<n>`` (1-based). Empty
    documents (a stray ``---``) are skipped; numbering counts them so
    ``#n`` still points at the n-th document in the file.
    """
    with open(path) as f:
        docs = list(yaml.load_all(f, Loader=_Loader))  # libyaml safe loader
    multi = len(docs) > 1
    scenarios: list[Scenario] = []
    for n, data in enumerate(docs, start=1):
        if data is None and multi:
            continue
        where = f"{path}#{n}" if multi else str(path)
        if not isinstance(data, dict):
            raise SystemExit(f"{where}: expected a mapping at the top level")
        try:
            scenarios.append(
                Scenario.model_validate(
                    {"path": path, "doc": n if multi else None, **data}
                )
            )
        except ValidationError as exc:
            raise SystemExit(f"{where}: {validation_summary(exc)}") from exc
    if not scenarios:
        raise SystemExit(f"{path}: expected a mapping at the top level")
    return scenarios


def load_scenario(path: Path) -> Scenario:
    """Parse and validate a one-scenario YAML file."""
    scenarios = load_scenarios(path)
    if len(scenarios) != 1:
        raise SystemExit(f"{path}: expected one scenario, found {len(scenarios)}")
    return scenarios[0]


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


def _inquiry_totals(
    client: AcumaticaClient,
    probe: Expect | PresentSpec,
    field_names: list[str],
    variables: dict[str, Any],
) -> dict[str, float]:
    """Probe a contract inquiry; sum named fields over matching Results rows.

    ``parameters`` (and ``match`` values) interpolate via ``variables`` so
    built-in ``${current_period}`` works on present + expect sites (V43).
    """
    assert probe.inquire is not None
    body = client.put(
        probe.inquire,
        _subst(probe.parameters or {}, variables),
        endpoint=probe.endpoint,
        params={"$expand": "Results"},
        retry=True,  # inquiry PUT is a read (gh #52)
    )
    totals = dict.fromkeys(field_names, 0.0)
    match = _subst(probe.match, variables) if probe.match else None
    for row in body.get("Results") or []:
        values = unwrap(row)
        if match and any(
            field not in values or _norm(values[field]) != _norm(want)
            for field, want in match.items()
        ):
            continue
        for field in totals:
            totals[field] += float(values.get(field) or 0.0)
    return totals


def _inquire(
    client: AcumaticaClient, expect: Expect, variables: dict[str, Any]
) -> dict[str, float]:
    """Probe a contract inquiry; sum each delta field over matching rows."""
    assert expect.inquire is not None
    assert expect.delta is not None
    return _inquiry_totals(client, expect, list(expect.delta), variables)


def _present(
    client: AcumaticaClient, present: PresentSpec, variables: dict[str, Any]
) -> bool:
    """True when the probe holds: a get row matches, or every when holds.

    ``get`` asks for ``$top=1`` - one row proves the document exists. The
    inquire form is absolute, not delta.
    """
    if present.get is not None:
        rows = client.get_list(
            present.get.entity,
            params={
                "$filter": str(_subst(present.get.filter, variables)),
                "$top": "1",
            },
            endpoint=present.get.endpoint,
        )
        return bool(rows)
    assert present.when is not None
    totals = _inquiry_totals(client, present, list(present.when), variables)
    for field, pred in present.when.items():
        got = totals[field]
        if pred.gte is not None and got < float(pred.gte):
            return False
        if pred.eq is not None and _norm(got) != _norm(pred.eq):
            return False
    return True


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
    """List steps and expectations without any HTTP; annotate once gates."""
    if scenario.once:
        assert scenario.present is not None
        output.data(f"  once: present {scenario.present.label()}")
    if not scenario.steps:
        output.data("  (no steps)")
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


def _run_get(
    client: AcumaticaClient, step: Step, variables: dict[str, Any]
) -> dict[str, Any]:
    """Fetch a get step via key-URL or list filter; return unwrapped row."""
    assert step.get is not None
    params: dict[str, str] = {}
    if step.get.expand:
        params["$expand"] = ",".join(sorted(step.get.expand))
    if step.get.filter is not None:
        params["$filter"] = str(_subst(step.get.filter, variables))
        if step.get.top is not None:
            params["$top"] = str(step.get.top)
        if step.get.orderby is not None:
            params["$orderby"] = str(_subst(step.get.orderby, variables))
        rows = client.get_list(
            step.get.entity, params=params or None, endpoint=step.get.endpoint
        )
        if not rows:
            raise RuntimeError(
                f"step '{step.id}': {step.get.entity} filter "
                f"{params.get('$filter')!r} matched no rows"
            )
        output.data(f"  get {step.get.entity} filter [{step.id}]")
        return unwrap(rows[0])
    assert step.get.keys is not None
    keys = _subst(step.get.keys, variables)
    record = client.get_record(step.get.entity, keys, step.get.endpoint, params or None)
    if record is None:
        raise RuntimeError(f"step '{step.id}': {step.get.entity} {keys} not found")
    output.data(f"  get {step.get.entity} [{step.id}]")
    return unwrap(record)


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
            timeout=step.timeout,
        )
        output.data(f"  invoke {step.action.name} [{step.id}]")
    elif step.get is not None:
        fetched = _run_get(client, step, variables)
        for path, var in (step.capture or {}).items():
            variables[var] = _resolve_path(fetched, path)
    if step.wait is not None:
        _wait(client, step.wait, variables)
        output.data(f"  wait ok [{step.id}]")


def _check_get(
    client: AcumaticaClient,
    expect: Expect,
    variables: dict[str, Any],
    misses: list[str],
) -> bool:
    """Absolute field assertions on a key-URL record; misses collect text."""
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
            misses.append(f"{label}.{field}: not found")
            output.data(f"  x {misses[-1]}")
        elif _norm(got) != _norm(want):
            ok = False
            misses.append(f"{label}.{field}: expected {want!r} got {got!r}")
            output.data(f"  x {misses[-1]}")
        else:
            output.data(f"  + {label}.{field} = {want!r}")
    return ok


def _check_delta(
    client: AcumaticaClient,
    expect: Expect,
    snapshot: dict[str, float],
    variables: dict[str, Any],
    misses: list[str],
) -> bool:
    """Delta assertions: post-run probe minus the pre-run snapshot."""
    assert expect.delta is not None
    after = _inquire(client, expect, variables)
    ok = True
    for field, want in expect.delta.items():
        got = after[field] - snapshot[field]
        if abs(got - want) > 0.005:
            ok = False
            misses.append(
                f"{expect.label()}.{field}: expected delta {want:+g} got {got:+g}"
            )
            output.data(f"  x {misses[-1]}")
        else:
            output.data(f"  + {expect.label()}.{field} delta {want:+g}")
    return ok


class Outcome(StrEnum):
    """How one scenario ended (gh #58 summary buckets)."""

    PASSED = "passed"
    FAILED = "failed"  # an expectation missed; later scenarios still run
    SKIPPED = "skipped"  # once: already present


class StepError(RuntimeError):
    """A step (or once/expect probe) raised: names file and step (gh #58).

    The caller stops the run here - lifecycle scenarios build on each
    other, so later ones would fail on missing documents.
    """

    def __init__(self, label: str, step_id: str, message: str) -> None:
        super().__init__(f"{label} [{step_id}]: {message}")
        self.label = label
        self.step_id = step_id


# Everything a live step can raise for a reason outside the code: REST
# errors, transport, a lost session, an unknown ${var} (SystemExit).
_STEP_FAILURES = (RuntimeError, httpx.HTTPError, SessionLostError, SystemExit)


def _guard[**P, T](
    scenario: Scenario,
    step_id: str,
    fn: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> T:
    """Call fn, re-raising a step-class failure as StepError for step_id."""
    try:
        return fn(*args, **kwargs)
    except _STEP_FAILURES as err:
        raise StepError(scenario.label, step_id, format_failure(err)) from err


def run(
    client: AcumaticaClient | None,
    scenario: Scenario,
    dry_run: bool = False,
    misses: list[str] | None = None,
) -> Outcome:
    """Execute the scenario; PASSED when every step and expectation held.

    client may be None only under dry_run - parsing and step listing
    never touch HTTP (a preview costs nothing live). Delta expectations
    snapshot BEFORE the first step (V4): the comparison is post minus
    pre, so a warm tenant re-runs clean.

    Built-in ``${current_period}`` (V43) is seeded at process start from
    host-local date and expands on every ``${var}`` site (steps, expect
    params, once.present params). Capture variables layer on top after
    each step.

    ``once: true`` (V4/gh #19, gh #73): ``present`` probe first (inquire
    absolute or get filter). Probe true → stdout ``skip <label> (once:
    already present)``, no step HTTP, no expects, exit 0. Probe false →
    steps + expects as usual.

    A step failure raises StepError naming the file and step id (gh #58);
    a failing once probe or expect probe names ``once`` / ``expect``.
    Expectation misses append their text to ``misses`` (gh #57 JSON).
    """
    missed: list[str] = [] if misses is None else misses
    output.data(f"{scenario.label} -> {scenario.scenario}")
    if dry_run:
        _dry_run(scenario)
        return Outcome.PASSED
    assert client is not None  # non-dry-run callers pass a live session
    variables: dict[str, Any] = _builtin_variables()
    if scenario.once:
        present = scenario.present
        assert present is not None
        if _guard(scenario, "once", _present, client, present, variables):
            output.data(f"skip {scenario.label} (once: already present)")
            return Outcome.SKIPPED
    before = [
        _guard(scenario, "expect", _inquire, client, expect, variables)
        if expect.inquire
        else {}
        for expect in scenario.expect
    ]
    for step in scenario.steps:
        _guard(scenario, step.id, _run_step, client, step, variables)
    ok = True
    for expect, snapshot in zip(scenario.expect, before, strict=True):
        if expect.get is not None:
            held = _guard(
                scenario, "expect", _check_get, client, expect, variables, missed
            )
        else:
            held = _guard(
                scenario,
                "expect",
                _check_delta,
                client,
                expect,
                snapshot,
                variables,
                missed,
            )
        ok = held and ok
    return Outcome.PASSED if ok else Outcome.FAILED
