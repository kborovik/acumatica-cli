"""Dataset target matrix: committed verified ERP + Default API versions (V27).

``target.yaml`` is *what* (V2) — co-located with the data-repo root found by
``.env`` discovery. Never secrets. Present → ``load_instance`` sources
``Instance.api_version`` from ``default_api`` when ``--api-version`` is
absent (source-merge; dual-source match gate retired — T125); missing →
warn on check unless ``--strict``; invalid → hard-fail any loader.
"""

from pathlib import Path

import yaml
from pydantic import ValidationError, field_validator

from .config import Instance, find_data_root
from .models import Model, validation_summary

TARGET_FILENAME = "target.yaml"


class DatasetTarget(Model):
    """Committed verified target for a data repo (V2 what — never secrets)."""

    erp: str  # claimed product line/build; live compare only when a probe exists
    default_api: str  # Default contract version half only (e.g. 25.200.001)

    @field_validator("default_api")
    @classmethod
    def _api_version_half_only(cls, v: str) -> str:
        v = v.strip().strip("/")
        if not v:
            raise ValueError(
                "default_api must be the version half only (e.g. 25.200.001)"
            )
        if "/" in v or v.lower().startswith("default"):
            raise ValueError(
                "default_api must be the version half only "
                f"(e.g. 25.200.001), not a path like Default/{v}"
            )
        return v

    @field_validator("erp")
    @classmethod
    def _erp_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("erp must be a non-empty build id (e.g. 26.101.0225)")
        return v


def find_target_path(root: Path | None = None) -> Path | None:
    """``{data-root}/target.yaml`` when the file exists; else None."""
    root = root if root is not None else find_data_root()
    if root is None:
        return None
    path = root / TARGET_FILENAME
    return path if path.is_file() else None


def load_target(root: Path | None = None) -> DatasetTarget | None:
    """Return DatasetTarget, None if absent, or SystemExit on unreadable/invalid.

    Invalid file is always a hard error for any caller that loads it — never
    silently ignored on apply while only failing on config check (V27).
    """
    path = find_target_path(root)
    if path is None:
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise SystemExit(f"{path}: cannot read target.yaml: {exc}") from exc
    if data is None:
        raise SystemExit(f"{path}: target.yaml is empty")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    try:
        return DatasetTarget.model_validate(data)
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def assert_target_compatible(inst: Instance, root: Path | None = None) -> None:
    """Load target when present; invalid → hard-fail (V27).

    Version mismatch gate retired (T125): ``load_instance`` source-merges
    ``default_api`` into ``api_version`` when the flag is absent; flag
    override is intentional for ad-hoc probes. Missing target is not an
    error here (config check --strict owns that). Call only from the
    allowlisted data-plane commands — never from bare pass_instance.
    """
    del inst  # kept for call-site stability; version match no longer used
    load_target(root)
