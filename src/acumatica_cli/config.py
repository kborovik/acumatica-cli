"""The instance target (acu.yaml, found by walking up from cwd) + credentials (.env).

Layered defaults: ``base_url`` and ``ssh`` are the only required acu.yaml
keys — one explicit address per plane (V1), never derived. Everything else
is a code default transcribed from the verified references (docs/ac-exe.md,
docs/rest-api.md — V12), overridable per instance for nonstandard installs.
"""

import os
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import ValidationError, field_validator

from .models import Model, validation_summary

PLACEHOLDER_HOST = "erp.example.com"

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
    """The resolved target: the acu.yaml top-level map + credentials.

    One explicit address per plane (V1), no derivation: ``base_url`` is the
    REST root (scheme + host + site path), ``ssh`` the control-plane
    ``user@host``. Everything else is a code default for a stock install.
    """

    base_url: str  # REST root: scheme + host + site path
    ssh: str  # control plane: full user@host
    tenant: str = ""
    acu_instance_name: str = "AcumaticaERP"  # ac.exe -iname; IIS app-pool
    # name coupling = acumatica-infra convention (see recycle_app_pool)
    acu_instance_path: str = "C:\\Acumatica\\AcumaticaERP"  # ac.exe -h
    ac_exe: str = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
    db_name: str = "AcumaticaDB"
    api_version: str = "25.200.001"  # V11: /entity/Default/<api_version>/
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


def data_root() -> Path:
    """Walk up from cwd to the first directory containing acu.yaml."""
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "acu.yaml").is_file():
            return d
    raise SystemExit(
        "acu.yaml not found in the current directory or any parent - "
        "run acu from inside a data repo (e.g. acumatica-baseline)"
    )


def read_config(root: Path) -> dict[str, Any]:
    """Parse the acu.yaml at root; hard error unless it is a mapping."""
    with open(root / "acu.yaml") as f:
        config = yaml.safe_load(f)
    if not isinstance(config, dict):
        raise SystemExit(
            "acu.yaml: expected a mapping (base_url + ssh + optional overrides)"
        )
    return config


def load_instance() -> Instance:
    """Resolve the target from acu.yaml and merge credentials from .env/environment."""
    root = data_root()
    load_dotenv(root / ".env")

    config = read_config(root)

    password = os.environ.get("ACU_PASSWORD")
    if not password:
        raise SystemExit("ACU_PASSWORD not set (put it in .env or the environment)")

    try:
        return Instance(
            username=os.environ.get("ACU_USER", "admin"),
            password=password,
            **config,
        )
    except ValidationError as exc:
        raise SystemExit(f"acu.yaml: {validation_summary(exc)}") from exc
