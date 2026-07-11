"""The instance target: global flags over acu.yaml over code defaults.

Layered resolution, first set wins per key: global flag, acu.yaml (found by
walking up from cwd — optional, flags plus environment can supply the full
config), code default. ``base_url`` and ``ssh`` are the only required
values — one explicit address per plane (V1), never derived. Credentials
resolve flag over environment (.env loads only beside a found acu.yaml).
"""

import os
from collections.abc import Iterator, Mapping
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError, field_validator

from .models import Model, validation_summary

PLACEHOLDER_HOST = "erp.example.com"

# Install-layout constants for a stock acumatica-infra build (docs/ac-exe.md,
# verified live - V12). Deliberately not config surface: acu.yaml keys of
# these names are rejected (extra="forbid"), keeping the config a target
# address, not an install description.
ACU_INSTANCE_NAME = "AcumaticaERP"  # ac.exe -iname; IIS app-pool name
# coupling = acumatica-infra convention (see recycle_app_pool)
ACU_INSTANCE_PATH = "C:\\Acumatica\\AcumaticaERP"  # ac.exe -h
AC_EXE = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
DB_NAME = "AcumaticaDB"

# `acu config init` template set: (package resource, destination) pairs.
# Dotfiles are stored dotless (wheel tooling tends to drop dotfiles) and
# mapped to their real names on write.
INIT_TEMPLATES = (
    ("acu.yaml", "acu.yaml"),
    ("env", ".env"),
    ("gitignore", ".gitignore"),
    ("baseline/10-subaccounts.yaml", "baseline/10-subaccounts.yaml"),
    ("baseline/20-accounts.yaml", "baseline/20-accounts.yaml"),
    ("baseline/40-ledger.yaml", "baseline/40-ledger.yaml"),
    ("baseline/50-gl-preferences.yaml", "baseline/50-gl-preferences.yaml"),
    ("baseline/60-ledger-company.yaml", "baseline/60-ledger-company.yaml"),
    ("baseline/90-uoms.yaml", "baseline/90-uoms.yaml"),
    ("bootstrap/company.yaml", "bootstrap/company.yaml"),
    ("bootstrap/credit-terms.yaml", "bootstrap/credit-terms.yaml"),
    ("bootstrap/features.yaml", "bootstrap/features.yaml"),
    ("setup/10-financial-year.yaml", "setup/10-financial-year.yaml"),
    ("setup/20-master-calendar.yaml", "setup/20-master-calendar.yaml"),
    ("setup/30-open-periods.yaml", "setup/30-open-periods.yaml"),
)


class Instance(Model):
    """The resolved target: flags over the acu.yaml top-level map + credentials.

    One explicit address per plane (V1), no derivation: ``base_url`` is the
    REST root (scheme + host + site path), ``ssh`` the control-plane
    ``user@host``. Install-layout values are module constants, not fields.
    """

    api_version: str = "25.200.001"  # V11: /entity/Default/<api_version>/
    base_url: str  # REST root: scheme + host + site path
    ssh: str  # control plane: full user@host
    tenant: str = ""
    username: str
    password: str

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("api_version")
    @classmethod
    def _no_surrounding_slashes(cls, v: str) -> str:
        return v.strip("/")


def scaffold(directory: Path, host: str | None = None) -> Iterator[tuple[str, Path]]:
    """Write the data-repo template set into ``directory``, never overwriting.

    Yields ("write" | "skip", path) per template file. ``host`` replaces the
    placeholder host inside the scaffolded acu.yaml ``base_url``/``ssh``
    values; secrets stay placeholders (V2). The directory
    is created if absent. No git init, no gpg - version control and secret
    encryption stay the operator's call.
    """
    pkg = resources.files("acumatica_cli") / "templates"
    directory.mkdir(parents=True, exist_ok=True)
    for resource, dest in INIT_TEMPLATES:
        target = directory / dest
        if target.exists():
            yield "skip", target
            continue
        content = (pkg / resource).read_text(encoding="utf-8")
        if host and dest == "acu.yaml":
            content = content.replace(PLACEHOLDER_HOST, host)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        yield "write", target


def find_data_root() -> Path | None:
    """Walk up from cwd to the first directory containing acu.yaml, if any.

    None is not an error (V3): flags plus environment can supply the full
    config; only commands needing data files (provision, schema) require a
    data repo and go through data_root instead.
    """
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "acu.yaml").is_file():
            return d
    return None


def data_root() -> Path:
    """The data repo root, for commands that need its files, not just config."""
    root = find_data_root()
    if root is None:
        raise SystemExit(
            "acu.yaml not found in the current directory or any parent - "
            "run acu from inside a data repo (e.g. acumatica-baseline)"
        )
    return root


def read_config(root: Path) -> dict[str, Any]:
    """Parse the acu.yaml at root; hard error unless it is a mapping."""
    with open(root / "acu.yaml") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise SystemExit(
            "acu.yaml: expected a mapping (base_url + ssh + optional overrides)"
        )
    return config


def load_instance(overrides: Mapping[str, str | None] | None = None) -> Instance:
    """Resolve the target: global flags over acu.yaml over code defaults.

    ``overrides`` carries the global flags keyed by Instance field name;
    per key the first set value wins (flag, acu.yaml, code default).
    Credentials resolve flag over environment - .env loads only from the
    directory of a found acu.yaml, and no acu.yaml is fine (V3): the hard
    error comes only when a required value (base_url, ssh, password) is
    still unresolved after the merge, naming the missing key.
    """
    flags = {k: v for k, v in dict(overrides or {}).items() if v is not None}
    root = find_data_root()
    config: dict[str, Any] = {}
    if root is not None:
        load_dotenv(root / ".env")
        config = read_config(root)
        if creds := sorted({"username", "password"} & config.keys()):
            raise SystemExit(
                f"acu.yaml: credentials never live in config (V2) - "
                f"remove {', '.join(creds)}; use flags or .env instead"
            )

    username = flags.pop("username", None) or os.environ.get("ACU_USER", "admin")
    password = flags.pop("password", None) or os.environ.get("ACU_PASSWORD")
    if not password:
        raise SystemExit(
            "password not set (pass --password, "
            "or put ACU_PASSWORD in .env or the environment)"
        )

    try:
        return Instance(
            username=username,
            password=password,
            **{**config, **flags},
        )
    except ValidationError as exc:
        source = "acu.yaml" if root is not None else "config (no acu.yaml found)"
        raise SystemExit(f"{source}: {validation_summary(exc)}") from exc
