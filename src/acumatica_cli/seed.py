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

import queue
import re
import threading
from collections import Counter
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import httpx
import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from . import bootstrap, output
from .checkpoint import Checkpoint, digest
from .client import (
    OPTIMIZATION_500,
    RECORD_FAILURES,
    AcumaticaClient,
    SessionLostError,
    format_failure,
    unwrap,
)
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
# NumberingSequence runtime counters (V40/gh #25): LastNbr (+ advanced
# counter if ever exposed) = issued progress, not desired config. Extract
# hard-strips; diff never compares; apply never PUTs them (re-apply must
# not reset live counters).
NUMBERING_RUNTIME_FIELDS = frozenset({"LastNbr"})
# Fields omitted from source↔live compare (V39 write-only + V40 runtime).
# Field-name deny at every nesting level.
_DIFF_IGNORE_FIELDS = PASSWORD_FIELDS | NUMBERING_RUNTIME_FIELDS
# Path-qualified write-only GET-omit (V56): (entity, detail, field).
# Diff ignores; extract strips; apply still PUTs when present in YAML.
# Do not add bare ``Value`` to _DIFF_IGNORE_FIELDS — that would hide
# real drift on unrelated fields.
_DIFF_IGNORE_PATHS = frozenset({("LotSerialClass", "Segments", "Value")})
# Whole-detail skip (V57/B37): mapped GET Role.Users / User.Roles may be
# empty while UsersInRoles has session-company rows. Extra live members
# are not drift (AssignUser is additive-only).
_DIFF_SKIP_DETAILS = frozenset({("Role", "Users"), ("User", "Roles")})


def _diff_ignored(entity: str, *path: str) -> bool:
    """True when source↔live compare skips this field (V39/V40/V56/V57)."""
    if path and path[-1] in _DIFF_IGNORE_FIELDS:
        return True
    if len(path) == 1 and (entity, path[0]) in _DIFF_SKIP_DETAILS:
        return True
    return (entity, *path) in _DIFF_IGNORE_PATHS


def _drop_path_ignored(entity: str, field: str, rows: list[Any]) -> list[Any]:
    """Strip path-qualified GET-omit fields from detail rows (V56)."""
    drop = {
        sub for (ent, det, sub) in _DIFF_IGNORE_PATHS if ent == entity and det == field
    }
    if not drop:
        return rows
    return [
        {k: v for k, v in row.items() if k not in drop}
        if isinstance(row, dict)
        else row
        for row in rows
    ]


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
                "(ACU_API_VERSION or --api-version)"
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
            self._check_role_users(i, record)
        return self

    def _check_role_users(self, i: int, record: dict[str, Any]) -> None:
        # V53: Role.Users rows become AssignUser calls after the Role PUT;
        # a row with no Username must fail at load, not mid-apply (gh #54)
        users = record.get("Users")
        if self.entity != "Role" or not isinstance(users, list):
            return
        for j, row in enumerate(users):
            if isinstance(row, dict) and row.get("delete") is True:
                continue
            username = row.get("Username") if isinstance(row, dict) else None
            if not isinstance(username, str) or not username:
                raise ValueError(f"records[{i}].Users[{j}] missing Username")

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
    timeout: float | None = Field(default=None, gt=0)  # 202 poll ceiling (V63)
    done_when: DoneProbe


def load_baseline(
    path: Path, bootstrap_contract: tuple[str, frozenset[str]] | None = None
) -> BaselineFile | ActionFile:
    """Parse and validate one seed YAML file, dispatching on the action: key.

    Symbolic ``endpoint: bootstrap`` resolves to the packaged contract
    version at load (V20/V21/T178 package SoT).
    Symbolic ``endpoint: default`` stays on the model and resolves at HTTP
    time via ``client._url`` (never load-rewritten — version is where).
    An entity the active Bootstrap contract serves still needs an explicit
    endpoint (literal or symbolic); silent Default-endpoint PUTs are the
    B8 class. ``bootstrap_contract`` is a pre-read ``active_bootstrap()``
    (load_all reads it once for every file).
    """
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except (yaml.YAMLError, UnicodeDecodeError) as exc:
        raise SystemExit(f"{path}: invalid YAML: {exc}") from exc
    except OSError as exc:
        raise SystemExit(f"{path}: cannot read: {exc.strerror}") from exc
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    kind = ActionFile if "action" in data else BaselineFile
    try:
        parsed = kind.model_validate({**data, "path": path})
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc
    name, entities = bootstrap_contract or active_bootstrap()
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


def load_all(paths: Sequence[Path]) -> list[BaselineFile | ActionFile]:
    """Load every seed file up front; report all failures together.

    apply/diff call this before the session opens, so a bad late file
    never leaves the tenant half-applied without a summary (V45, gh #54).
    """
    contract = active_bootstrap()  # environment error: abort, not per-file
    loaded: list[BaselineFile | ActionFile] = []
    failures: list[str] = []
    for path in paths:
        try:
            loaded.append(load_baseline(path, contract))
        except SystemExit as exc:
            failures.append(str(exc.code))
    if failures:
        raise SystemExit(
            "\n".join(
                [
                    f"{len(failures)} of {len(paths)} seed file(s) failed "
                    "validation - nothing sent to the tenant",
                    *failures,
                ]
            )
        )
    return loaded


# ISO date or DateTimeValue: calendar-day prefix only (V55). Random strings
# that merely contain T stay untouched.
_ISO_CALENDAR_DAY = re.compile(r"(\d{4}-\d{2}-\d{2})(T.*)?")


def _calendar_day(text: str) -> str | None:
    """YYYY-MM-DD when ``text`` is ISO date/datetime; else None. No TZ convert."""
    m = _ISO_CALENDAR_DAY.fullmatch(text)
    return None if m is None else m.group(1)


def _norm(value: Any) -> str:
    """Comparable form: bools case-folded, numbers by value, dates by day.

    Numbers compare by value, not spelling - a YAML `0` against the
    endpoint's `0.0` (DecimalValue fields come back as floats) is not
    drift (T13). Date-only ``YYYY-MM-DD`` matches a live DateTimeValue on
    the same calendar day (strip ``T...offset``); a different day still
    drifts (V55). No timezone conversion.
    """
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, int | float):
        return repr(float(value))
    text = str(value).strip()
    day = _calendar_day(text)
    return text if day is None else day


def _filter_for(record: dict[str, Any], keys: list[str]) -> str:
    # callers pass single-view key sets only: a conjunction spanning the
    # entity's views answers 200 [] (B14), so _fetch filters on the first
    # key alone and matches the rest client-side. Literal typing follows
    # the YAML scalar type: bools and numbers travel bare - a quoted
    # 'false' against Edm.Boolean answers 500 "binary operator with
    # incompatible types" (T61 - INPreferences keyed on HoldEntry), and
    # numeric Edm types are the same class. Strings stay quoted, so
    # numeric-looking codes ('000000') are unaffected; an embedded ' is
    # doubled per OData literal syntax (gh #49 - O'Brien).
    def literal(value: Any) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, int | float):
            return str(value)
        return "'" + str(value).replace("'", "''") + "'"

    return " and ".join(f"{k} eq {literal(record[k])}" for k in keys)


def _expand_paths(record: dict[str, Any], prefix: str = "") -> list[str]:
    """$expand paths a record's shape demands (T60/T65).

    A list field is a detail array - expands by name; a dict field is a
    linked entity - expands by name plus the slash path of every nested
    dict (`MainContact`, `MainContact/Address`).

    ``custom`` is not an $expand path — contract custom fields ride the
    ``$custom=View.Field`` query option (see ``_custom_param``).
    """
    paths: list[str] = []
    for field, value in record.items():
        if field == "custom" and not prefix:
            continue
        if isinstance(value, list):
            paths.append(f"{prefix}{field}")
        elif isinstance(value, dict):
            paths.append(f"{prefix}{field}")
            paths.extend(_expand_paths(value, f"{prefix}{field}/"))
    return sorted(paths)


def _custom_param(record: dict[str, Any]) -> str | None:
    """Build ``$custom=View.Field,…`` from a seed ``custom`` bag.

    Shape (plain YAML, value-wrapped on PUT by ``wrap``)::

        custom:
          AccountRecords:
            ControlAccountModule: AR

    Returns ``AccountRecords.ControlAccountModule`` or None when the bag
    is absent/empty. View→field leaves only; nested linked-entity style
    under custom is not supported (contract custom fields are flat per view).
    """
    bag = record.get("custom")
    if not isinstance(bag, dict) or not bag:
        return None
    paths: list[str] = []
    for view, fields in bag.items():
        if not isinstance(fields, dict):
            continue
        for field in fields:
            paths.append(f"{view}.{field}")
    return ",".join(sorted(paths)) if paths else None


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


# V45 hard-stop classes: the host refuses connections or the session is
# gone, so every later record would fail the same way.
_STOP_FAILURES = (SessionLostError, httpx.ConnectError)


class ApplyStopped(Exception):  # noqa: N818 - control flow, not an error class
    """V45 hard-stop: record ``record`` hit a session/host loss.

    Carries what this file already did (``ok``, ``errors``) so the caller
    can print the stopped-at record plus the not-applied remainder.
    """

    def __init__(self, record: str, reason: str, ok: int, errors: list[str]):
        super().__init__(f"{record}: {reason}")
        self.record = record
        self.reason = reason
        self.ok = ok
        self.errors = errors


class ApplyMode(StrEnum):
    """apply write policy (gh #55): PUT all (V4), PLAN (read-only), CHANGED."""

    ALL = "all"
    PLAN = "plan"
    CHANGED = "changed"


def _apply_action(  # noqa: PLR0913 — checkpoint keyword-only (gh #71)
    client: AcumaticaClient | None,
    action: ActionFile,
    dry_run: bool = False,
    plan: bool = False,
    counts: Counter[str] | None = None,
    *,
    checkpoint: Checkpoint | None = None,
) -> tuple[int, list[str]]:
    """Invoke the action unless done_when already verifies the desired state.

    On probe or invoke failure (REST or transport), returns ``(0, [error])``
    rather than raising so the caller can continue other files (V45
    multi-error summary). A refused connect raises ApplyStopped. Under
    plan (gh #55) only the probe runs: ``unchanged`` or ``invoke``.
    """
    tally = counts if counts is not None else Counter[str]()
    if dry_run:
        # gh #63: no done_when probe, so dry-run stays offline (no session)
        output.data(f"  would invoke {action.action}")
        return 1, []
    assert client is not None, "live apply needs a client"
    unit = f"action {action.action} [{action.entity}]"
    unit_digest = digest(action.model_dump(mode="json", exclude={"path"}))
    if _resumed(checkpoint, unit, unit_digest, tally):
        return 1, []
    try:
        if _probe(client, action):
            output.data(f"  skip {action.action} (already done)")
            tally["unchanged"] += 1
            if checkpoint is not None:
                checkpoint.record(unit, unit_digest)
            return 1, []
        if plan:
            output.data(f"  invoke {action.action} [{action.entity}]")
            tally["invoke"] += 1
            return 1, []
        client.invoke(
            action.entity,
            action.action,
            action.record,
            action.parameters,
            action.endpoint,
            timeout=action.timeout,
        )
    except _STOP_FAILURES as err:
        record = f"action {action.action} [{action.entity}]"
        raise ApplyStopped(record, format_failure(err), 0, []) from err
    except RECORD_FAILURES as err:
        msg = f"action {action.action} [{action.entity}]: {format_failure(err)}"
        output.error(msg)
        return 0, [msg]
    output.data(f"  invoke {action.action} [{action.entity}]")
    tally["invoke"] += 1
    if checkpoint is not None:
        checkpoint.record(unit, unit_digest)
    return 1, []


def _resumed(
    checkpoint: Checkpoint | None, unit: str, unit_digest: str, tally: Counter[str]
) -> bool:
    """V67: unit already done on this tenant with this content → skip line."""
    if checkpoint is None or not checkpoint.done(unit, unit_digest):
        return False
    output.data(f"  skip {unit} (checkpoint)")
    tally["checkpoint"] += 1
    return True


def apply(  # noqa: PLR0913 — pool keyword-only (gh #71)
    client: AcumaticaClient | None,
    baseline: BaselineFile | ActionFile,
    dry_run: bool = False,
    mode: ApplyMode = ApplyMode.ALL,
    counts: Counter[str] | None = None,
    *,
    pool: Sequence[AcumaticaClient] = (),
    checkpoint: Checkpoint | None = None,
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

    Per-record failure isolation (V45): a failed PUT — REST error or
    transport failure (timeout, dropped) — does not abort later records
    in the same file. A refused connect or failed mid-run re-login raises
    ApplyStopped instead (hard-stop; later records would all fail).
    Returns ``(ok_count, error_messages)`` — never silent partial; caller
    exits 1 when any errors remain. Exit 2 stays drift (``diff`` only).

    Role.Users mapped-detail PUT is a no-op (V53/B33). After the Role PUT,
    apply invokes contract action ``AssignUser`` per Username so persist
    is PXDatabase UsersInRoles, not the mapped detail.

    client may be None only under dry_run: the preview never touches HTTP
    (gh #63), so it needs no session or credentials.

    PLAN / CHANGED mode (gh #55) read live first with the ``diff`` compare
    and label each record create / update (fields) / unchanged into
    ``counts``. PLAN writes nothing; CHANGED skips the PUT of an
    unchanged record (Role user assignment still runs: V57 membership is
    not diffable). Default stays PUT-everything (V4).

    ``pool`` (gh #71, V66) holds every ``--concurrency`` session, ``client``
    first. More than one → records of this file PUT in parallel, one
    session per worker. Action and Company files stay serial on
    ``client``; after a Company PUT every pooled session re-logs in (V5).

    ``checkpoint`` (V67): a record or action already done on this tenant
    with the same content skips (``counts["checkpoint"]``, counted ok);
    each success appends its line. Never under plan or dry run.
    """
    tally = counts if counts is not None else Counter[str]()
    if isinstance(baseline, ActionFile):
        plan = mode is ApplyMode.PLAN
        return _apply_action(
            client, baseline, dry_run, plan, tally, checkpoint=checkpoint
        )
    if len(pool) > 1 and not dry_run:
        if baseline.entity != "Company":
            return _apply_pooled(pool, baseline, mode, tally, checkpoint=checkpoint)
        output.info(f"{baseline.path}: Company file applies serially (V5 re-login)")
        try:
            return _apply_serial(
                client, baseline, dry_run, mode, tally, checkpoint=checkpoint
            )
        finally:
            _refresh_pool(pool)
    return _apply_serial(client, baseline, dry_run, mode, tally, checkpoint=checkpoint)


def _refresh_pool(pool: Sequence[AcumaticaClient]) -> None:
    """V5: once the first session bounced after a Company PUT, bounce the rest."""
    if not pool[0].refreshed_after_company:
        return
    for i, client in enumerate(pool[1:], start=2):
        try:
            client.refresh_after_company()
        except SessionLostError as err:
            raise ApplyStopped(
                f"pooled session {i}/{len(pool)}", str(err), 0, []
            ) from err


@dataclass(frozen=True)
class _Pooled:
    """One dispatched record's outcome: ok, error message, or stop (at, why)."""

    tally: Counter[str]
    error: str | None = None
    stop: tuple[str, str] | None = None


def _apply_pooled(
    pool: Sequence[AcumaticaClient],
    baseline: BaselineFile,
    mode: ApplyMode,
    tally: Counter[str],
    *,
    checkpoint: Checkpoint | None,
) -> tuple[int, list[str]]:
    """Records of one file across N sessions; V45 semantics as serial.

    Each worker checks a session out for one record, so no session ever
    carries two requests at once. A record failure reports and continues.
    A hard-stop stops new dispatch; in-flight records finish, then the
    first stop (record order) raises ApplyStopped with what got done.
    """
    index = _prefetch(pool[0], baseline) if mode is not ApplyMode.ALL else {}
    idle: queue.SimpleQueue[AcumaticaClient] = queue.SimpleQueue()
    for client in pool:
        idle.put(client)
    stop = threading.Event()

    def one(record: dict[str, Any]) -> _Pooled | None:
        if stop.is_set():
            return None  # not dispatched
        at = f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
        local = Counter[str]()
        unit_digest = digest(record)
        if _resumed(checkpoint, at, unit_digest, local):
            return _Pooled(local)
        client = idle.get()
        try:
            _apply_record(client, baseline, record, index=index, mode=mode, tally=local)
            if checkpoint is not None:
                checkpoint.record(at, unit_digest)
        except _STOP_FAILURES as err:
            stop.set()
            return _Pooled(local, stop=(at, format_failure(err)))
        except RECORD_FAILURES as err:
            msg = f"{at}: {format_failure(err)}"
            output.error(msg)
            return _Pooled(local, error=msg)
        finally:
            idle.put(client)
        return _Pooled(local)

    with ThreadPoolExecutor(max_workers=len(pool)) as workers:
        results = [r for r in workers.map(one, baseline.records) if r is not None]
    return _merge_pooled(results, tally)


def _merge_pooled(results: list[_Pooled], tally: Counter[str]) -> tuple[int, list[str]]:
    """Fold dispatched outcomes in record order; first stop raises ApplyStopped."""
    ok = 0
    errors: list[str] = []
    for result in results:
        tally.update(result.tally)
        ok += result.error is None and result.stop is None
        if result.error is not None:
            errors.append(result.error)
    stops = [r.stop for r in results if r.stop is not None]
    if stops:
        raise ApplyStopped(*stops[0], ok, errors)
    return ok, errors


def _apply_serial(  # noqa: PLR0913 — checkpoint keyword-only (gh #71)
    client: AcumaticaClient | None,
    baseline: BaselineFile,
    dry_run: bool,
    mode: ApplyMode,
    tally: Counter[str],
    *,
    checkpoint: Checkpoint | None,
) -> tuple[int, list[str]]:
    """The single-session record loop (``--concurrency 1``, Company files)."""
    ok = 0
    errors: list[str] = []
    index = (
        _prefetch(client, baseline)
        if client is not None and mode is not ApplyMode.ALL
        else {}
    )
    for record in baseline.records:
        label = ", ".join(str(record[k]) for k in baseline.keys)
        if dry_run:
            output.data(f"  would PUT {baseline.entity} [{label}]")
            try:
                _assign_role_users(client, baseline, record, dry_run=True)
                ok += 1
            except RECORD_FAILURES as err:
                msg = f"{baseline.entity} [{label}]: {format_failure(err)}"
                output.error(msg)
                errors.append(msg)
            continue
        assert client is not None, "live apply needs a client"
        at = f"{baseline.entity} [{label}]"
        unit_digest = digest(record)
        if _resumed(checkpoint, at, unit_digest, tally):
            ok += 1
            continue
        try:
            _apply_record(client, baseline, record, index=index, mode=mode, tally=tally)
            ok += 1
            if checkpoint is not None:
                checkpoint.record(at, unit_digest)
        except _STOP_FAILURES as err:
            raise ApplyStopped(at, format_failure(err), ok, errors) from err
        except RECORD_FAILURES as err:
            msg = f"{baseline.entity} [{label}]: {format_failure(err)}"
            output.error(msg)
            errors.append(msg)
    return ok, errors


def _apply_record(  # noqa: PLR0913 — keyword-only; gh #55 plan bundle
    client: AcumaticaClient,
    baseline: BaselineFile,
    record: dict[str, Any],
    *,
    index: dict[tuple[str, ...], dict[str, Any]],
    mode: ApplyMode,
    tally: Counter[str],
) -> None:
    """One live record: classify (PLAN/CHANGED), then PUT unless skipped."""
    at = f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
    kind = ""
    if mode is not ApplyMode.ALL:
        kind, fields = _classify(client, baseline, index, record)
        if mode is ApplyMode.PLAN or kind == "unchanged":
            output.data(
                f"  {kind} {at}" + (f" ({', '.join(fields)})" if fields else "")
            )
            if mode is ApplyMode.CHANGED:
                _assign_role_users(client, baseline, record, dry_run=False)
            tally[kind] += 1
            return
    body = _put_body(client, baseline, record)
    if baseline.entity == "Role":
        body.pop("Users", None)
    _put(client, baseline.entity, body, baseline.endpoint)
    if baseline.entity == "Company":
        client.refresh_after_company()
    output.data(f"  PUT {at}" + (f" ({kind})" if kind else ""))
    _assign_role_users(client, baseline, record, dry_run=False)
    tally[kind or "put"] += 1


def _assign_role_users(
    client: AcumaticaClient | None,
    baseline: BaselineFile,
    record: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    """V53/B33: persist Role.Users via AssignUser, not mapped-detail PUT.

    Additive-only: ``delete: true`` merge rows are skipped. Mapped GET of
    Users may be empty, so extra live members are not a persist delete.
    Role PUT strips ``Users`` so additive-only does not depend on B33.
    """
    if baseline.entity != "Role":
        return
    users = record.get("Users")
    if not isinstance(users, list):
        return
    rolename = record.get("Rolename")
    if not isinstance(rolename, str) or not rolename:
        return
    to_assign: list[str] = []
    for i, row in enumerate(users):
        if isinstance(row, dict) and row.get("delete") is True:
            continue
        if not isinstance(row, dict):
            raise RuntimeError(f"Users[{i}] is not a mapping")
        username = row.get("Username")
        if not isinstance(username, str) or not username:
            raise RuntimeError(f"Users[{i}] missing Username")
        to_assign.append(username)
    for username in to_assign:
        if dry_run:
            output.data(f"  would invoke AssignUser [{rolename}/{username}]")
            continue
        assert client is not None, "live apply needs a client"
        client.invoke(
            "Role",
            "AssignUser",
            {"Rolename": rolename},
            {"Username": username},
            baseline.endpoint,
        )
        output.data(f"  invoke AssignUser [{rolename}/{username}]")


def _put(
    client: AcumaticaClient,
    entity: str,
    body: dict[str, Any],
    endpoint: str | None,
) -> None:
    """PUT one record; one re-login + retry on branch-empty 500 (V5/B24).

    Keyed upsert is idempotent (V4), so it opts into the client's blip
    retry (gh #52).
    """
    try:
        client.put(entity, body, endpoint=endpoint, retry=True)
    except RuntimeError as err:
        if _BRANCH_EMPTY not in str(err):
            raise
        output.info(f"re-login and retry {entity} (branch empty)")
        client.relogin()
        client.put(entity, body, endpoint=endpoint, retry=True)


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
    params: dict[str, str] = {"$filter": _filter_for(record, baseline.keys[:1])}
    expand = _expand_paths(record)
    if expand:
        params["$expand"] = ",".join(expand)
    custom = _custom_param(record)
    if custom:
        params["$custom"] = custom
    try:
        live = client.get_list(
            baseline.entity, params=params, endpoint=baseline.endpoint
        )
    except RuntimeError as err:
        if OPTIMIZATION_500 not in str(err):
            raise
        key_params = {
            k: params[k] for k in ("$expand", "$custom") if k in params
        } or None
        return client.get_record(
            baseline.entity,
            [record[k] for k in baseline.keys],
            baseline.endpoint,
            params=key_params,
        )
    for row in live:
        actual = unwrap(row)
        if all(
            k in actual and _norm(actual[k]) == _norm(record[k])
            for k in baseline.keys[1:]
        ):
            return row
    return None


def _prefetch(
    client: AcumaticaClient, baseline: BaselineFile
) -> dict[tuple[str, ...], dict[str, Any]]:
    """One list GET for the whole file, indexed by normalized key tuple (gh #56).

    No $filter (so no B14 cross-view conjunction); $expand / $custom are
    the union of what each record's shape demands. A best-effort read:
    any REST error (B9 optimization 500, an entity that will not list)
    or an empty answer (B22 org-scoped views) yields no index, and a key
    tuple seen on more than one live row is dropped - callers fall back
    to the per-record ``_fetch`` for every record not indexed here, so
    the result never differs from the per-record path. Single-record
    files skip it (the filtered read is already one GET).
    """
    if len(baseline.records) < 2:
        return {}
    params: dict[str, str] = {}
    expand = sorted({p for r in baseline.records for p in _expand_paths(r)})
    if expand:
        params["$expand"] = ",".join(expand)
    custom = sorted(
        {c for r in baseline.records for c in (_custom_param(r) or "").split(",") if c}
    )
    if custom:
        params["$custom"] = ",".join(custom)
    try:
        rows = client.get_list(
            baseline.entity, params=params or None, endpoint=baseline.endpoint
        )
    except RuntimeError:
        return {}
    index: dict[tuple[str, ...], dict[str, Any]] = {}
    dups: set[tuple[str, ...]] = set()
    for row in rows:
        actual = unwrap(row)
        if not all(k in actual for k in baseline.keys):
            continue
        ident = tuple(_norm(actual[k]) for k in baseline.keys)
        if ident in index:
            dups.add(ident)
        index[ident] = row
    for ident in dups:
        del index[ident]
    return index


def _live_for(
    client: AcumaticaClient,
    baseline: BaselineFile,
    index: dict[tuple[str, ...], dict[str, Any]],
    record: dict[str, Any],
) -> dict[str, Any] | None:
    """The prefetched live row for record, else the per-record ``_fetch``."""
    row = index.get(tuple(_norm(record[k]) for k in baseline.keys))
    return row if row is not None else _fetch(client, baseline, record)


def _classify(
    client: AcumaticaClient,
    baseline: BaselineFile,
    index: dict[tuple[str, ...], dict[str, Any]],
    record: dict[str, Any],
) -> tuple[str, list[str]]:
    """``create`` / ``update`` + drifted top-level fields / ``unchanged`` (gh #55).

    Same live read and compare as ``diff``; a field the endpoint does not
    return counts as an update (conservative: it is PUT).
    """
    live = _live_for(client, baseline, index, record)
    if live is None:
        return "create", []
    label = f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
    fields: list[str] = []
    for drift in _record_drifts(baseline, label, record, live):
        name = re.split(r"[.\[]", drift.where[len(label) + 1 :], maxsplit=1)[0]
        if name not in fields:
            fields.append(name)
    return ("update", fields) if fields else ("unchanged", [])


_DRIFT_TEXT = {
    "missing": "missing on tenant",
    "extra": "extra on tenant",
    "not_returned": "not returned by endpoint",
    "not_applied": "not applied",
}


class Drift(str):
    """One drift line: the str is the human text, attributes the parts.

    ``where`` is ``<Entity> [<keys>][.<field path>]`` (or ``action X``);
    ``kind`` one of changed | missing | extra | not_returned | not_applied;
    ``source`` / ``live`` set for ``changed``. ``diff`` fills ``keys``
    (record key values) and ``field`` (path below the record, "" for the
    record itself) for ``--output json`` (gh #57).
    """

    where: str
    kind: str
    source: Any
    live: Any
    keys: dict[str, Any]
    field: str

    def __new__(
        cls, where: str, kind: str, source: Any = None, live: Any = None
    ) -> Drift:
        """Render the human text from where + kind (+ source/live)."""
        text = (
            f"{where}: source={source!r} live={live!r}"
            if kind == "changed"
            else f"{where}: {_DRIFT_TEXT[kind]}"
        )
        self = super().__new__(cls, text)
        self.where, self.kind, self.source, self.live = where, kind, source, live
        self.keys, self.field = {}, ""
        return self


def diff(client: AcumaticaClient, baseline: BaselineFile | ActionFile) -> list[Drift]:
    """Compare each source record against the live tenant.

    Reads the file with one list GET when it can (gh #56, ``_prefetch``),
    per record otherwise. Returns drift lines (empty = no drift; each a
    ``Drift``, str = the human text). An action file
    diffs through its done_when probe - coarse present/absent (V4): a
    tenant that lost the action's effect must not diff false-green, but an
    action leaves no keyed record to compare field by field.
    """
    if isinstance(baseline, ActionFile):
        if _probe(client, baseline):
            return []
        return [Drift(f"action {baseline.action}", "not_applied")]
    drifts: list[Drift] = []
    index = _prefetch(client, baseline)
    for record in baseline.records:
        label = (
            f"{baseline.entity} [{', '.join(str(record[k]) for k in baseline.keys)}]"
        )
        live = _live_for(client, baseline, index, record)
        found = (
            [Drift(label, "missing")]
            if live is None
            else _record_drifts(baseline, label, record, live)
        )
        for drift in found:
            drift.keys = {k: record[k] for k in baseline.keys}
            drift.field = drift.where[len(label) + 1 :]
        drifts.extend(found)
    return drifts


def _record_drifts(
    baseline: BaselineFile, label: str, record: dict[str, Any], live: dict[str, Any]
) -> list[Drift]:
    """Field drift lines for one source record against its live row."""
    drifts: list[Drift] = []
    actual = unwrap(live)
    # V39/V40/V56/V57: never compare write-only, runtime, GET-omit,
    # or membership details whose mapped GET may stay empty.
    fields = {k: v for k, v in record.items() if not _diff_ignored(baseline.entity, k)}
    for field, expected in fields.items():
        if isinstance(expected, list):
            key = (baseline.detail_keys or {})[field]  # load-validated
            want_rows = _drop_path_ignored(baseline.entity, field, expected)
            live_rows = _drop_path_ignored(
                baseline.entity, field, actual.get(field, [])
            )
            drifts.extend(_diff_details(label, field, key, want_rows, live_rows))
        elif isinstance(expected, dict):
            drifts.extend(_diff_nested(f"{label}.{field}", expected, actual.get(field)))
        elif field not in actual:
            drifts.append(Drift(f"{label}.{field}", "not_returned"))
        elif _norm(actual[field]) != _norm(expected):
            drifts.append(Drift(f"{label}.{field}", "changed", expected, actual[field]))
    return drifts


def _diff_nested(path: str, expected: dict[str, Any], live: Any) -> list[Drift]:
    """Linked-entity drift (T65): recurse source-side fields only."""
    if not isinstance(live, dict):
        return [Drift(path, "not_returned")]
    drifts: list[Drift] = []
    for field, want in expected.items():
        if field in _DIFF_IGNORE_FIELDS:
            continue
        if isinstance(want, dict):
            drifts.extend(_diff_nested(f"{path}.{field}", want, live.get(field)))
        elif field not in live:
            drifts.append(Drift(f"{path}.{field}", "not_returned"))
        elif _norm(live[field]) != _norm(want):
            drifts.append(Drift(f"{path}.{field}", "changed", want, live[field]))
    return drifts


def _diff_details(
    label: str,
    field: str,
    key: str,
    expected: list[dict[str, Any]],
    live_rows: list[dict[str, Any]],
) -> list[Drift]:
    """Detail-array drift (T60): rows matched by detail key, order-insensitive.

    Unlike top-level records (V4 exemption), an extra live detail row IS
    drift - the record owns its list, apply cannot converge a live row the
    source never claimed. Within a matched row only source-side fields
    compare (server-derived LineNbr and ids stay omitted from source).
    """
    drifts: list[Drift] = []
    live_by_key = {_norm(row[key]): row for row in live_rows if key in row}
    for row in expected:
        ident = _norm(row[key])
        live_row = live_by_key.pop(ident, None)
        where = f"{label}.{field}[{row[key]}]"
        if live_row is None:
            drifts.append(Drift(where, "missing"))
            continue
        for sub, want in row.items():
            if sub in _DIFF_IGNORE_FIELDS:
                continue
            if sub not in live_row:
                drifts.append(Drift(f"{where}.{sub}", "not_returned"))
            elif _norm(live_row[sub]) != _norm(want):
                drifts.append(Drift(f"{where}.{sub}", "changed", want, live_row[sub]))
    for ident in live_by_key:
        drifts.append(Drift(f"{label}.{field}[{ident}]", "extra"))
    return drifts
