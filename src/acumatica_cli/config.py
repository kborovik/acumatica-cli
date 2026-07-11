"""The instance target (acu.yaml, found by walking up from cwd) + credentials (.env).

Layered defaults: ``host`` is the only required acu.yaml key. Everything else
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
from pydantic import ValidationError, field_validator, model_validator

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

    ``host`` drives both planes (V1): REST ``base_url`` and control-plane
    ``ssh`` derive from it unless the acu.yaml map overrides them
    explicitly (split-horizon DNS, port forwards, jump hosts, nonroot sites).
    """

    host: str
    tenant: str = ""
    scheme: str = "http"  # docs/rest-api.md: http://acu-dev1.vm.internal/...
    ssh_user: str = "Administrator"
    instance_name: str = "AcumaticaERP"
    instance_path: str = "C:\\Acumatica\\AcumaticaERP"
    ac_exe: str = "C:\\Program Files\\Acumatica ERP\\Data\\ac.exe"
    db_name: str = "AcumaticaDB"
    endpoint: str = "Default/25.200.001"  # V11: versioned path only
    base_url: str = ""  # default derived: <scheme>://<host>/<instance_name>
    ssh: str = ""  # default derived: <ssh_user>@<host>
    username: str
    password: str

    @field_validator("base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("endpoint")
    @classmethod
    def _no_surrounding_slashes(cls, v: str) -> str:
        return v.strip("/")

    @model_validator(mode="before")
    @classmethod
    def _derive_urls(cls, data: Any) -> Any:
        """Construct base_url/ssh from host; an explicit override wins."""
        if not isinstance(data, dict) or not data.get("host"):
            return data  # let field validation report the missing host

        def resolved(key: str) -> object:
            return data.get(key) or cls.model_fields[key].default

        data = dict(data)
        host = data["host"]
        if not data.get("base_url"):
            data["base_url"] = (
                f"{resolved('scheme')}://{host}/{resolved('instance_name')}"
            )
        if not data.get("ssh"):
            data["ssh"] = f"{resolved('ssh_user')}@{host}"
        return data


def scaffold(directory: Path, host: str | None = None) -> Iterator[tuple[str, Path]]:
    """Write the data-repo template set into ``directory``, never overwriting.

    Yields ("write" | "skip", path) per template file. ``host`` replaces the
    acu.yaml placeholder host; secrets stay placeholders (V2). The directory
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
        raise SystemExit("acu.yaml: expected a mapping (host + optional overrides)")
    return config


def load_instance(host: str | None = None) -> Instance:
    """Resolve the target from acu.yaml and merge credentials from .env/environment.

    ``host`` (the global --host flag) replaces the acu.yaml host before the
    Instance is built, so derived base_url/ssh follow it; a post-hoc
    model_copy would leave them pointing at the old host. Explicit acu.yaml
    base_url/ssh overrides still win, exactly as they do over the file's own
    host.
    """
    root = data_root()
    load_dotenv(root / ".env")

    config = read_config(root)
    if host is not None:
        config["host"] = host

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
