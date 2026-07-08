"""Instance targets (acu.toml, found by walking up from cwd) + credentials (.env)."""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError, field_validator

from .models import Model, validation_summary


class Instance(Model):
    """A resolved target: an acu.toml [instances.<name>] table + credentials."""

    name: str
    base_url: str
    endpoint: str
    tenant: str = ""
    ssh: str
    ac_exe: str
    instance_name: str
    instance_path: str
    db_name: str
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


def data_root() -> Path:
    """Walk up from cwd to the first directory containing acu.toml."""
    for d in [Path.cwd(), *Path.cwd().parents]:
        if (d / "acu.toml").is_file():
            return d
    raise SystemExit(
        "acu.toml not found in the current directory or any parent - "
        "run acu from inside a data repo (e.g. acumatica-baseline)"
    )


def load_instance(name: str | None = None) -> Instance:
    """Resolve a target from acu.toml and merge credentials from .env/environment.

    With name=None the config's top-level ``default_instance`` is used.
    """
    root = data_root()
    load_dotenv(root / ".env")

    with open(root / "acu.toml", "rb") as f:
        config = tomllib.load(f)

    instances = config.get("instances", {})
    if name is None:
        name = config.get("default_instance")
        if not name:
            raise SystemExit("acu.toml: default_instance not set; pass -i/--instance")
    if name not in instances:
        known = ", ".join(sorted(instances)) or "none"
        raise SystemExit(f"acu.toml: no [instances.{name}] (known: {known})")

    password = os.environ.get("ACU_PASSWORD")
    if not password:
        raise SystemExit("ACU_PASSWORD not set (put it in .env or the environment)")

    try:
        return Instance(
            name=name,
            username=os.environ.get("ACU_USER", "admin"),
            password=password,
            **instances[name],
        )
    except ValidationError as exc:
        raise SystemExit(
            f"acu.toml [instances.{name}]: {validation_summary(exc)}"
        ) from exc
