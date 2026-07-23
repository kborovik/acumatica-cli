"""The instance target: global flags over ACU_* environment over code defaults.

pydantic-settings owns resolution: ``Instance`` is a ``BaseSettings`` with
env prefix ``ACU_``, and the sole config file is ``.env`` (found by walking
up from cwd) carrying where + secrets as ``ACU_*`` vars. The file is
optional - flags plus the process environment can supply the full config.
Per key the first set value wins: flag, ``ACU_*`` var (process environment
over a found ``.env``), code default. ``base_url`` is the only required
address (REST data plane); ``ssh`` is optional control-plane address
(empty = data-plane only; tenant cmds hard-error when unresolved — V1/V3).
The password must resolve via ``--password`` or ``ACU_PASSWORD``.
"""

from collections.abc import Iterator, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

from .models import validation_summary

PLACEHOLDER_HOST = "erp.example.com"

ACU_INSTANCE_NAME = "AcumaticaERP"  # ac.exe -iname; IIS app-pool name
ACU_INSTANCE_PATH = "C:\\Acumatica\\AcumaticaERP"  # ac.exe -h
AC_EXE = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
DB_NAME = "AcumaticaDB"

# `acu config init` template set: (package resource, destination) pairs.
# Dotfiles are stored dotless (wheel tooling tends to drop dotfiles) and
# mapped to their real names on write.
#
# Layout under templates/ (symmetric flavors):
#   finance/       — default finance-minimal package set (V28)
#   distribution/  — overlays + master/scenario extras
# bootstrap/project.xml is not a templates/ file: scaffold copies the
# packaged full company contract (bootstrap_project.xml) so both flavors
# share Bootstrap/1.0.0 (T81/T82).
INIT_TEMPLATES = (
    ("finance/env", ".env"),
    ("finance/gitignore", ".gitignore"),
    ("finance/target", "target.yaml"),
    ("finance/baseline/10-subaccounts.yaml", "baseline/10-subaccounts.yaml"),
    ("finance/baseline/20-accounts.yaml", "baseline/20-accounts.yaml"),
    ("finance/baseline/40-ledger.yaml", "baseline/40-ledger.yaml"),
    ("finance/baseline/50-gl-preferences.yaml", "baseline/50-gl-preferences.yaml"),
    ("finance/baseline/60-ledger-company.yaml", "baseline/60-ledger-company.yaml"),
    ("finance/baseline/90-uoms.yaml", "baseline/90-uoms.yaml"),
    ("finance/bootstrap/company.yaml", "bootstrap/company.yaml"),
    ("finance/bootstrap/credit-terms.yaml", "bootstrap/credit-terms.yaml"),
    ("finance/bootstrap/features.yaml", "bootstrap/features.yaml"),
    (
        "finance/bootstrap/project.xml",
        "bootstrap/project.xml",
    ),  # sentinel; see scaffold
    ("finance/setup/10-financial-year.yaml", "setup/10-financial-year.yaml"),
    ("finance/setup/20-master-calendar.yaml", "setup/20-master-calendar.yaml"),
    ("finance/setup/30-open-periods.yaml", "setup/30-open-periods.yaml"),
)

# Opt-in `--flavor distribution` overlays + extras (V28/V29). Resource paths
# live under templates/distribution/; dest paths are data-repo relative.
# Same dest as INIT_TEMPLATES replaces the finance-minimal file (company
# LAB5, expanded COA, features, open-periods, uoms). Contract identity is
# shared (T82) — no distribution-only project.xml.
DISTRIBUTION_TEMPLATES = (
    ("distribution/bootstrap/company.yaml", "bootstrap/company.yaml"),
    ("distribution/bootstrap/features.yaml", "bootstrap/features.yaml"),
    ("distribution/baseline/20-accounts.yaml", "baseline/20-accounts.yaml"),
    ("distribution/baseline/60-ledger-company.yaml", "baseline/60-ledger-company.yaml"),
    ("distribution/baseline/90-uoms.yaml", "baseline/90-uoms.yaml"),
    ("distribution/setup/30-open-periods.yaml", "setup/30-open-periods.yaml"),
    ("distribution/master/10-reason-codes.yaml", "master/10-reason-codes.yaml"),
    ("distribution/master/20-in-preferences.yaml", "master/20-in-preferences.yaml"),
    (
        "distribution/master/30-availability-rules.yaml",
        "master/30-availability-rules.yaml",
    ),
    ("distribution/master/40-posting-classes.yaml", "master/40-posting-classes.yaml"),
    ("distribution/master/50-warehouse.yaml", "master/50-warehouse.yaml"),
    (
        "distribution/master/51-warehouse-locations.yaml",
        "master/51-warehouse-locations.yaml",
    ),
    (
        "distribution/master/52-warehouse-defaults.yaml",
        "master/52-warehouse-defaults.yaml",
    ),
    ("distribution/master/53-tax-categories.yaml", "master/53-tax-categories.yaml"),
    ("distribution/master/54-item-classes.yaml", "master/54-item-classes.yaml"),
    ("distribution/master/56-so-preferences.yaml", "master/56-so-preferences.yaml"),
    ("distribution/master/57-po-preferences.yaml", "master/57-po-preferences.yaml"),
    ("distribution/master/58-order-types.yaml", "master/58-order-types.yaml"),
    ("distribution/master/60-ar-preferences.yaml", "master/60-ar-preferences.yaml"),
    ("distribution/master/61-ap-preferences.yaml", "master/61-ap-preferences.yaml"),
    ("distribution/master/62-ca-preferences.yaml", "master/62-ca-preferences.yaml"),
    ("distribution/master/63-cash-account.yaml", "master/63-cash-account.yaml"),
    ("distribution/master/64-payment-methods.yaml", "master/64-payment-methods.yaml"),
    ("distribution/master/65-statement-cycles.yaml", "master/65-statement-cycles.yaml"),
    ("distribution/master/70-vendor-classes.yaml", "master/70-vendor-classes.yaml"),
    ("distribution/master/71-customer-classes.yaml", "master/71-customer-classes.yaml"),
    ("distribution/master/75-vendors.yaml", "master/75-vendors.yaml"),
    ("distribution/master/76-customers.yaml", "master/76-customers.yaml"),
    (
        "distribution/master/80-stock-items-parts.yaml",
        "master/80-stock-items-parts.yaml",
    ),
    ("distribution/master/82-stock-items-kits.yaml", "master/82-stock-items-kits.yaml"),
    (
        "distribution/master/85-kit-specifications.yaml",
        "master/85-kit-specifications.yaml",
    ),
    ("distribution/scenario/buy-sell.yaml", "scenario/buy-sell.yaml"),
    ("distribution/README.md", "README.md"),
)

INIT_FLAVORS = frozenset({"distribution"})


class Instance(BaseSettings):
    """The resolved target: flags over ACU_* vars (.env or process) over defaults.

    Explicit addresses, no derivation (V1): ``base_url`` is the REST root
    (scheme + host + site path); ``ssh`` is the optional control-plane
    ``user@host`` (empty = data-plane only). Install-layout values are
    module constants, not fields. Unknown ``ACU_*`` vars are ignored,
    never errors - the environment and ``.env`` legitimately carry
    non-config vars (``ACU_DEBUG``).
    """

    model_config = SettingsConfigDict(
        env_prefix="ACU_",
        extra="ignore",
        frozen=True,
    )

    base_url: str  # REST root: scheme + host + site path
    ssh: str = ""  # control plane: full user@host; empty = data-plane only
    tenant: str = ""
    api_version: str = "25.200.001"  # V11: /entity/Default/<api_version>/
    user: str = "admin"  # ACU_USER; the --username flag maps here
    # required, but enforced in load_instance so a blank scaffolded
    # ACU_PASSWORD= placeholder and a missing var raise the same named error
    password: str = ""

    @model_validator(mode="before")
    @classmethod
    def _blank_required_is_unset(cls, data: Any) -> Any:
        # blank ACU_BASE_URL= / ACU_SSH= reads as unset (V3): base_url then
        # fails required; ssh falls through to the empty default (optional)
        if isinstance(data, dict):
            for key in ("base_url", "ssh"):
                if data.get(key) == "":
                    del data[key]
        return data

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("api_version")
    @classmethod
    def _api_version_half_only(cls, v: str) -> str:
        # V11: version half only (e.g. 25.200.001). A full path like
        # Default/25.200.001 would nest as /entity/Default/Default/...
        v = v.strip().strip("/")
        if not v:
            raise ValueError(
                "api_version must be the version half only (e.g. 25.200.001)"
            )
        if "/" in v or v.lower().startswith("default"):
            raise ValueError(
                "api_version must be the version half only "
                f"(e.g. 25.200.001), not a path like Default/{v}"
            )
        return v


def templates_for(flavor: str | None) -> tuple[tuple[str, str], ...]:
    """Resolve (resource, dest) pairs for ``config init`` (V28).

    Absent flavor → finance-minimal ``INIT_TEMPLATES`` only. ``distribution``
    merges ``DISTRIBUTION_TEMPLATES`` over the same dest keys (overlay wins),
    then appends destinations that finance-minimal does not scaffold.
    """
    if flavor is None:
        return INIT_TEMPLATES
    if flavor not in INIT_FLAVORS:
        raise SystemExit(
            f"unknown config init flavor {flavor!r}; "
            f"known: {', '.join(sorted(INIT_FLAVORS))}"
        )
    by_dest = {dest: res for res, dest in INIT_TEMPLATES}
    order = [dest for _, dest in INIT_TEMPLATES]
    if flavor == "distribution":
        for res, dest in DISTRIBUTION_TEMPLATES:
            if dest not in by_dest:
                order.append(dest)
            by_dest[dest] = res
    return tuple((by_dest[dest], dest) for dest in order)


def scaffold(
    directory: Path, host: str | None = None, flavor: str | None = None
) -> Iterator[tuple[str, Path]]:
    """Write the data-repo template set into ``directory``, never overwriting.

    Yields ("write" | "skip", path) per template file. ``host`` replaces the
    placeholder host inside the scaffolded .env ``ACU_BASE_URL``/``ACU_SSH``
    values; secrets stay placeholders (V2). ``flavor`` selects the template
    set (V28; default finance-minimal). The directory is created if absent.
    No git init, no gpg - version control and secret encryption stay the
    operator's call.
    """
    pkg = resources.files("acumatica_cli") / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    for resource, dest in templates_for(flavor):
        target = directory / dest
        if target.exists():
            yield "skip", target
            continue
        # Single full contract (T81/T82): scaffold from the packaged
        # bootstrap_project.xml so init cannot diverge from the fallback.
        if dest == "bootstrap/project.xml":
            content = (
                resources.files("acumatica_cli") / "bootstrap_project.xml"
            ).read_text(encoding="utf-8")
        else:
            content = (pkg / resource).read_text(encoding="utf-8")
        if host and dest == ".env":
            content = content.replace(PLACEHOLDER_HOST, host)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        yield "write", target


def find_data_root() -> Path | None:
    """Walk up from cwd to the first directory containing .env, if any.

    None is not an error (V3): flags plus the process environment can supply
    the full config; only commands needing data files (schema, a bare
    apply/diff) require a data repo and go through data_root instead.
    """
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / ".env").is_file():
            return d
    return None


def data_root() -> Path:
    """The data repo root, for commands that need its files, not just config."""
    root = find_data_root()
    if root is None:
        raise SystemExit(
            ".env not found in the current directory or any parent - "
            "run acu from inside a data repo (e.g. acumatica-baseline)"
        )
    return root


def read_env_values(env_file: Path) -> dict[str, Any]:
    """Peek at a .env through the same source pydantic-settings resolves with.

    config check's discovery and secrets probes need per-key visibility
    (did the file supply ACU_BASE_URL / ACU_PASSWORD?) that a full Instance
    build deliberately hides; reusing DotEnvSettingsSource keeps the parse
    identical to live resolution, never a parallel one. Keys come back as
    Instance field names.
    """
    return DotEnvSettingsSource(Instance, env_file=env_file)()


def load_instance(overrides: Mapping[str, str | None] | None = None) -> Instance:
    """Resolve the target: global flags over ACU_* environment over defaults.

    ``overrides`` carries the global flags keyed by Instance field name;
    per key the first set value wins (flag, ACU_* var - process environment
    over a found .env - code default). No .env is fine (V3): the hard error
    comes only when a required value (base_url, password) is still
    unresolved after the merge, naming the missing key. ``ssh`` is optional
    (hosted / data-plane-only path); tenant cmds hard-error when it is empty.
    """
    flags = {k: v for k, v in dict(overrides or {}).items() if v is not None}
    root = find_data_root()
    env_file = root / ".env" if root is not None else None
    try:
        # _env_file is a real BaseSettings init override; the synthesized
        # field-only __init__ signature hides it from the type checker
        inst = Instance(_env_file=env_file, **flags)  # pyright: ignore[reportCallIssue]
    except ValidationError as exc:
        source = str(env_file) if env_file is not None else "config (no .env found)"
        raise SystemExit(f"{source}: {validation_summary(exc)}") from exc
    if not inst.password:
        raise SystemExit(
            "password not set (pass --password, "
            "or put ACU_PASSWORD in .env or the environment)"
        )
    return inst
