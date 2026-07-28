"""The instance target: global flags over ACU_* environment over code defaults.

pydantic-settings owns resolution: ``Instance`` is a ``BaseSettings`` with
env prefix ``ACU_``, and the sole config file is ``.env`` (found by walking
up from cwd) carrying where + secrets as ``ACU_*`` vars. The file is
optional - flags plus the process environment can supply the full config.
Per key the first set value wins: flag, ``ACU_*`` var (process environment
over a found ``.env``), code default — exclusion ``api_version`` (V27/T125):
``--api-version`` flag ? → else data-repo ``target.yaml`` ``default_api``
when present → else code default ``25.200.001``; never ``ACU_API_VERSION``
(unknown ``ACU_*`` ignored). ``base_url`` is the only required address
(REST data plane). ``ssh`` defaults to ``Administrator@`` + the
``base_url`` hostname when the key is absent (V3/T124); a present blank
key is the hosted opt-out (empty = data-plane only; tenant cmds hard-error
when empty post-default — V1/V3). The password must resolve via
``--password`` or ``ACU_PASSWORD``.
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

ACU_INSTANCE_NAME = "AcumaticaERP"  # ac.exe -iname; IIS app-pool name
ACU_INSTANCE_PATH = "C:\\Acumatica\\AcumaticaERP"  # ac.exe -h
AC_EXE = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
DB_NAME = "AcumaticaDB"

# `acu config init` template set: (package resource, destination) pairs.
# Dotfiles are stored dotless (wheel tooling tends to drop dotfiles) and
# mapped to their real names on write.
#
# Single full seed (V28/T108/T109): no --flavor. Package resources mirror
# dest layout under templates/ (derived from sibling acumatica-gitops seed
# trees; prune demo/, Makefile, live .env, state/, packaged project.xml).
# bootstrap project.xml is not a templates/ file: scaffold copies the
# packaged full company contract (bootstrap_project.xml) → Bootstrap/1.0.0.
INIT_TEMPLATES = (
    ("env", ".env"),
    ("gitignore", ".gitignore"),
    ("target", "target.yaml"),
    ("README.md", "README.md"),
    ("config/bootstrap/company.yaml", "config/bootstrap/company.yaml"),
    ("config/bootstrap/credit-terms.yaml", "config/bootstrap/credit-terms.yaml"),
    ("config/bootstrap/features.yaml", "config/bootstrap/features.yaml"),
    (
        "config/bootstrap/project.xml",
        "config/bootstrap/project.xml",
    ),  # sentinel; see scaffold
    ("config/baseline/10-subaccounts.yaml", "config/baseline/10-subaccounts.yaml"),
    ("config/baseline/20-accounts.yaml", "config/baseline/20-accounts.yaml"),
    ("config/baseline/40-ledger.yaml", "config/baseline/40-ledger.yaml"),
    (
        "config/baseline/50-gl-preferences.yaml",
        "config/baseline/50-gl-preferences.yaml",
    ),
    (
        "config/baseline/60-ledger-company.yaml",
        "config/baseline/60-ledger-company.yaml",
    ),
    ("config/baseline/90-uoms.yaml", "config/baseline/90-uoms.yaml"),
    ("config/setup/10-financial-year.yaml", "config/setup/10-financial-year.yaml"),
    ("config/setup/20-master-calendar.yaml", "config/setup/20-master-calendar.yaml"),
    ("config/setup/30-open-periods.yaml", "config/setup/30-open-periods.yaml"),
    ("config/master/10-reason-codes.yaml", "config/master/10-reason-codes.yaml"),
    ("config/master/20-in-preferences.yaml", "config/master/20-in-preferences.yaml"),
    (
        "config/master/30-availability-rules.yaml",
        "config/master/30-availability-rules.yaml",
    ),
    ("config/master/40-posting-classes.yaml", "config/master/40-posting-classes.yaml"),
    ("config/master/50-warehouse.yaml", "config/master/50-warehouse.yaml"),
    (
        "config/master/51-warehouse-locations.yaml",
        "config/master/51-warehouse-locations.yaml",
    ),
    (
        "config/master/52-warehouse-defaults.yaml",
        "config/master/52-warehouse-defaults.yaml",
    ),
    ("config/master/53-tax-categories.yaml", "config/master/53-tax-categories.yaml"),
    ("config/master/54-item-classes.yaml", "config/master/54-item-classes.yaml"),
    ("config/master/56-so-preferences.yaml", "config/master/56-so-preferences.yaml"),
    ("config/master/57-po-preferences.yaml", "config/master/57-po-preferences.yaml"),
    ("config/master/58-order-types.yaml", "config/master/58-order-types.yaml"),
    ("config/master/60-ar-preferences.yaml", "config/master/60-ar-preferences.yaml"),
    ("config/master/61-ap-preferences.yaml", "config/master/61-ap-preferences.yaml"),
    ("config/master/62-ca-preferences.yaml", "config/master/62-ca-preferences.yaml"),
    ("config/master/63-cash-account.yaml", "config/master/63-cash-account.yaml"),
    ("config/master/64-payment-methods.yaml", "config/master/64-payment-methods.yaml"),
    (
        "config/master/65-statement-cycles.yaml",
        "config/master/65-statement-cycles.yaml",
    ),
    ("config/master/70-vendor-classes.yaml", "config/master/70-vendor-classes.yaml"),
    (
        "config/master/71-customer-classes.yaml",
        "config/master/71-customer-classes.yaml",
    ),
    ("config/master/75-vendors.yaml", "config/master/75-vendors.yaml"),
    ("config/master/76-customers.yaml", "config/master/76-customers.yaml"),
    (
        "config/master/80-stock-items-parts.yaml",
        "config/master/80-stock-items-parts.yaml",
    ),
    (
        "config/master/82-stock-items-kits.yaml",
        "config/master/82-stock-items-kits.yaml",
    ),
    (
        "config/master/85-kit-specifications.yaml",
        "config/master/85-kit-specifications.yaml",
    ),
    (
        "config/views/10-trial-balance.yaml",
        "config/views/10-trial-balance.yaml",
    ),
    ("scenario/10-seed-capital.yaml", "scenario/10-seed-capital.yaml"),
    ("scenario/20-buy.yaml", "scenario/20-buy.yaml"),
    ("scenario/30-build.yaml", "scenario/30-build.yaml"),
    ("scenario/40-sell.yaml", "scenario/40-sell.yaml"),
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
    # V11/V27: version half only; resolved in load_instance (flag → target →
    # code default), never ACU_API_VERSION env. Default here is the code pin
    # only; env values for this field are discarded post-build (T125).
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
    placeholder host inside the scaffolded .env ``ACU_BASE_URL`` only
    (``ACU_SSH`` is omitted — defaults from the base_url host at resolve;
    hosted opt-out = present blank ``ACU_SSH=``). Secrets stay placeholders
    (V2). Single full seed under ``config/`` + lifecycle ``scenario/``
    (V28/T108; no flavor). The directory is created if absent. No git init,
    no gpg - version control and secret encryption stay the operator's call.
    """
    pkg = resources.files("acumatica_cli") / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    for resource, dest in INIT_TEMPLATES:
        target = directory / dest
        if target.exists():
            yield "skip", target
            continue
        # Single full contract (T81/T82): scaffold from the packaged
        # bootstrap_project.xml so init cannot diverge from the fallback.
        if dest == "config/bootstrap/project.xml":
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
    over a found .env - code default). Exclusion ``api_version`` (V27/T125):
    ``--api-version`` flag ? → else ``target.yaml`` ``default_api`` when
    present → else ``DEFAULT_API_VERSION``; ``ACU_API_VERSION`` is ignored.
    No .env is fine (V3): the hard error comes only when a required value
    (base_url, password) is still unresolved after the merge, naming the
    missing key. ``ssh`` defaults from the base_url host when the key is
    absent; blank key = hosted path; tenant cmds hard-error when empty
    post-default.
    """
    flags = {k: v for k, v in dict(overrides or {}).items() if v is not None}
    root = find_data_root()
    # V27/T125: resolve api_version outside env. Init kwargs beat dotenv/env,
    # so always inject — ACU_API_VERSION (valid or invalid) is ignored.
    if "api_version" not in flags:
        # Local import avoids config↔target cycle (target imports Instance).
        from .target import load_target

        target = load_target(root)
        flags["api_version"] = (
            target.default_api if target is not None else DEFAULT_API_VERSION
        )
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
