"""The instance target: global flags over ACU_* environment over code defaults.

pydantic-settings owns resolution: ``Instance`` is a ``BaseSettings`` with
env prefix ``ACU_``, and the sole config file is ``.env`` (found by walking
up from cwd) carrying where + pin + secrets as ``ACU_*`` vars. The file is
optional - flags plus the process environment can supply the full config.
Per key the first set value wins: flag, ``ACU_*`` var (process environment
over a found ``.env``), code default. ``api_version`` (V27):
``--api-version`` flag ? → else ``ACU_API_VERSION`` env → else code default
``25.200.001``. ``base_url`` is the only required address (REST data plane):
``--url`` flag ? → else ``ACU_BASE_URL`` → else hard error naming sources.
Leftover ``matrix.yaml`` is never loaded. ``ssh`` defaults to
``Administrator@`` + the ``base_url`` hostname when the key is absent
(V3/T124); a present blank key is the hosted opt-out (empty = data-plane
only; tenant cmds hard-error when empty post-default — V1/V3). The
password must resolve via ``--password`` or ``ACU_PASSWORD``.
"""

from collections.abc import Iterator, Mapping
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pydantic import ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict

from .models import validation_summary

PLACEHOLDER_HOST = "erp.example.com"
DEFAULT_SSH_USER = "Administrator"
DEFAULT_API_VERSION = "25.200.001"
# V44: data-repo overlays keyed by resolved Default half (api_version)
OVERLAYS_DIRNAME = "overlays"
OVERLAY_DIR_PREFIX = "default-"

ACU_INSTANCE_NAME = "AcumaticaERP"  # ac.exe -iname; IIS app-pool name
ACU_INSTANCE_PATH = "C:\\Acumatica\\AcumaticaERP"  # ac.exe -h
AC_EXE = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
DB_NAME = "AcumaticaDB"

# `acu config init` template set: (package resource, destination) pairs.
# Dotfiles are stored dotless (wheel tooling tends to drop dotfiles) and
# mapped to their real names on write.
#
# Single full seed (V28/T108/T109/T179): no --flavor. Package resources
# mirror dest layout under templates/ (derived from sibling acumatica-gitops
# seed trees; prune demo/, Makefile, live .env, state/, project.xml).
# Bootstrap contract stays packaged SoT (V2/V21) — never scaffolded.
INIT_TEMPLATES = (
    ("env", ".env"),
    ("gitignore", ".gitignore"),
    ("README.md", "README.md"),
    ("acu-config/bootstrap/company.yaml", "acu-config/bootstrap/company.yaml"),
    (
        "acu-config/bootstrap/credit-terms.yaml",
        "acu-config/bootstrap/credit-terms.yaml",
    ),
    (
        "acu-config/bootstrap/segmented-key.yaml",
        "acu-config/bootstrap/segmented-key.yaml",
    ),
    ("acu-config/bootstrap/features.yaml", "acu-config/bootstrap/features.yaml"),
    (
        "acu-config/baseline/10-subaccounts.yaml",
        "acu-config/baseline/10-subaccounts.yaml",
    ),
    ("acu-config/baseline/20-accounts.yaml", "acu-config/baseline/20-accounts.yaml"),
    ("acu-config/baseline/40-ledger.yaml", "acu-config/baseline/40-ledger.yaml"),
    (
        "acu-config/baseline/50-gl-preferences.yaml",
        "acu-config/baseline/50-gl-preferences.yaml",
    ),
    (
        "acu-config/baseline/60-ledger-company.yaml",
        "acu-config/baseline/60-ledger-company.yaml",
    ),
    ("acu-config/baseline/90-uoms.yaml", "acu-config/baseline/90-uoms.yaml"),
    (
        "acu-config/baseline/91-company-packaging.yaml",
        "acu-config/baseline/91-company-packaging.yaml",
    ),
    (
        "acu-config/setup/10-financial-year.yaml",
        "acu-config/setup/10-financial-year.yaml",
    ),
    (
        "acu-config/setup/20-master-calendar.yaml",
        "acu-config/setup/20-master-calendar.yaml",
    ),
    ("acu-config/setup/30-open-periods.yaml", "acu-config/setup/30-open-periods.yaml"),
    (
        "acu-config/master/05-numbering-sequences.yaml",
        "acu-config/master/05-numbering-sequences.yaml",
    ),
    (
        "acu-config/master/10-reason-codes.yaml",
        "acu-config/master/10-reason-codes.yaml",
    ),
    (
        "acu-config/master/20-in-preferences.yaml",
        "acu-config/master/20-in-preferences.yaml",
    ),
    (
        "acu-config/master/30-availability-rules.yaml",
        "acu-config/master/30-availability-rules.yaml",
    ),
    (
        "acu-config/master/40-posting-classes.yaml",
        "acu-config/master/40-posting-classes.yaml",
    ),
    ("acu-config/master/50-warehouse.yaml", "acu-config/master/50-warehouse.yaml"),
    (
        "acu-config/master/51-warehouse-locations.yaml",
        "acu-config/master/51-warehouse-locations.yaml",
    ),
    (
        "acu-config/master/52-warehouse-defaults.yaml",
        "acu-config/master/52-warehouse-defaults.yaml",
    ),
    (
        "acu-config/master/53-tax-categories.yaml",
        "acu-config/master/53-tax-categories.yaml",
    ),
    (
        "acu-config/master/54-item-classes.yaml",
        "acu-config/master/54-item-classes.yaml",
    ),
    (
        "acu-config/master/56-so-preferences.yaml",
        "acu-config/master/56-so-preferences.yaml",
    ),
    (
        "acu-config/master/57-po-preferences.yaml",
        "acu-config/master/57-po-preferences.yaml",
    ),
    ("acu-config/master/58-order-types.yaml", "acu-config/master/58-order-types.yaml"),
    (
        "acu-config/master/60-ar-preferences.yaml",
        "acu-config/master/60-ar-preferences.yaml",
    ),
    (
        "acu-config/master/61-ap-preferences.yaml",
        "acu-config/master/61-ap-preferences.yaml",
    ),
    (
        "acu-config/master/62-ca-preferences.yaml",
        "acu-config/master/62-ca-preferences.yaml",
    ),
    (
        "acu-config/master/63-cash-account.yaml",
        "acu-config/master/63-cash-account.yaml",
    ),
    (
        "acu-config/master/64-payment-methods.yaml",
        "acu-config/master/64-payment-methods.yaml",
    ),
    (
        "acu-config/master/65-statement-cycles.yaml",
        "acu-config/master/65-statement-cycles.yaml",
    ),
    (
        "acu-config/master/70-vendor-classes.yaml",
        "acu-config/master/70-vendor-classes.yaml",
    ),
    (
        "acu-config/master/71-customer-classes.yaml",
        "acu-config/master/71-customer-classes.yaml",
    ),
    ("acu-config/master/75-vendors.yaml", "acu-config/master/75-vendors.yaml"),
    ("acu-config/master/76-customers.yaml", "acu-config/master/76-customers.yaml"),
    (
        "acu-config/master/80-stock-items-parts.yaml",
        "acu-config/master/80-stock-items-parts.yaml",
    ),
    (
        "acu-config/master/82-stock-items-kits.yaml",
        "acu-config/master/82-stock-items-kits.yaml",
    ),
    (
        "acu-config/master/85-kit-specifications.yaml",
        "acu-config/master/85-kit-specifications.yaml",
    ),
    ("acu-config/master/90-roles.yaml", "acu-config/master/90-roles.yaml"),
    ("acu-config/master/91-users.yaml", "acu-config/master/91-users.yaml"),
    ("acu-config/master/92-role-users.yaml", "acu-config/master/92-role-users.yaml"),
    (
        "acu-config/views/10-trial-balance.yaml",
        "acu-config/views/10-trial-balance.yaml",
    ),
    ("acu-scenario/10-seed-capital.yaml", "acu-scenario/10-seed-capital.yaml"),
    ("acu-scenario/20-buy.yaml", "acu-scenario/20-buy.yaml"),
    ("acu-scenario/30-build.yaml", "acu-scenario/30-build.yaml"),
    ("acu-scenario/40-sell.yaml", "acu-scenario/40-sell.yaml"),
    # Default-half overlays (V44): keyed by resolved api_version
    ("overlays/README.md", "overlays/README.md"),
    (
        "overlays/default-24.200.001/README.md",
        "overlays/default-24.200.001/README.md",
    ),
    (
        "overlays/default-24.200.001/acu-scenario/30-build.yaml",
        "overlays/default-24.200.001/acu-scenario/30-build.yaml",
    ),
)


def default_ssh_for_base_url(base_url: str | None) -> str:
    """``Administrator@<hostname>`` from a REST root, or empty when unparseable.

    T124/V3 code default when ``ACU_SSH`` is absent. Blank key stays empty
    (hosted opt-out) and never calls this.
    """
    if not base_url:
        return ""
    host = urlparse(base_url).hostname
    return f"{DEFAULT_SSH_USER}@{host}" if host else ""


class Instance(BaseSettings):
    """The resolved target: flags over ACU_* vars (.env or process) over defaults.

    ``base_url`` is the REST root (scheme + host + site path). ``ssh`` is the
    control-plane ``user@host``: flag/env when the key is present win
    (including blank = hosted/data-plane only); key absent → code default
    ``Administrator@`` + base_url hostname (V3/T124). Install-layout values
    are module constants, not fields. Unknown ``ACU_*`` vars are ignored,
    never errors - the environment and ``.env`` legitimately carry
    non-config vars (``ACU_DEBUG``).
    """

    model_config = SettingsConfigDict(
        env_prefix="ACU_",
        extra="ignore",
        frozen=True,
    )

    base_url: str  # REST root: scheme + host + site path
    ssh: str = ""  # control plane: full user@host; empty post-default = data-plane only
    tenant: str = ""
    # V11/V27: version half only; flag → ACU_API_VERSION env → code default.
    api_version: str = DEFAULT_API_VERSION  # V11: /entity/Default/<api_version>/
    user: str = "admin"  # ACU_USER; the --username flag maps here
    # required, but enforced in load_instance so a blank scaffolded
    # ACU_PASSWORD= placeholder and a missing var raise the same named error
    password: str = ""

    @model_validator(mode="before")
    @classmethod
    def _blank_required_and_ssh_default(cls, data: Any) -> Any:
        # blank ACU_BASE_URL= reads as unset (V3): base_url then fails required.
        # ACU_SSH: key absent → Administrator@<base_url host>; key present blank
        # stays empty (hosted opt-out) — do not strip blank ssh (T124).
        if isinstance(data, dict):
            if data.get("base_url") == "":
                del data["base_url"]
            if "ssh" not in data:
                data["ssh"] = default_ssh_for_base_url(data.get("base_url"))
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


def scaffold(directory: Path, host: str | None = None) -> Iterator[tuple[str, Path]]:
    """Write the data-repo template set into ``directory``, never overwriting.

    Yields ("write" | "skip", path) per template file. ``host`` replaces the
    placeholder host inside scaffolded ``ACU_BASE_URL``. ``ACU_SSH`` is
    omitted — defaults from the resolved base_url host at resolve; hosted
    opt-out = present blank ``ACU_SSH=``. Secrets stay placeholders (V2).
    Single full seed under ``acu-config/`` + lifecycle ``acu-scenario/`` + ``.env``
    with ``ACU_BASE_URL`` + ``ACU_API_VERSION`` + pin-keyed ``overlays/``
    (V27/V28/T108/V44; no flavor; no ``matrix.yaml``; no ``target.yaml``;
    no customization tree).
    The directory is created if absent. No git init, no gpg - version
    control and secret encryption stay the operator's call.
    """
    pkg = resources.files("acumatica_cli") / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    for resource, dest in INIT_TEMPLATES:
        target = directory / dest
        if target.exists():
            yield "skip", target
            continue
        content = (pkg / resource).read_text(encoding="utf-8")
        if host and dest == ".env":
            content = content.replace(PLACEHOLDER_HOST, host)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        yield "write", target


def pin_overlay_dir(root: Path, api_version: str) -> Path | None:
    """``{root}/overlays/default-<api_version>`` when that directory exists.

    Overlay identity is the Default contract half (resolved
    ``Instance.api_version``), not the ERP marketing line. Missing dir
    → None (trunk-only host).
    """
    path = root / OVERLAYS_DIRNAME / f"{OVERLAY_DIR_PREFIX}{api_version}"
    return path if path.is_dir() else None


def find_data_root() -> Path | None:
    """Walk up from cwd to the first directory containing .env, if any.

    None is not an error (V3): flags plus the process environment can supply
    the full config; only commands needing data-repo files (schema dump)
    require a data repo and go through data_root instead.
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

    ``overrides`` carries the global flags keyed by Instance field name.
    Per key the first set value wins (flag, ACU_* var - process environment
    over a found .env - code default). V27:

    - ``api_version``: ``--api-version`` flag ? → else ``ACU_API_VERSION``
      → else ``DEFAULT_API_VERSION``.
    - ``base_url``: ``--url`` flag ? → else ``ACU_BASE_URL`` (env/.env) →
      else hard error naming sources.

    Leftover ``matrix.yaml`` is never loaded. No .env is fine (V3): the
    hard error comes only when a required value (base_url, password) is
    still unresolved after the merge, naming the missing key. ``ssh``
    defaults from the base_url host when the key is absent; blank key =
    hosted path; tenant cmds hard-error when empty post-default.
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
