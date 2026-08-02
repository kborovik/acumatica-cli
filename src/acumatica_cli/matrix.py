"""Dataset matrix: committed multi-host pin+where registry (V27).

``matrix.yaml`` is *what* + non-secret *where* (V2) — co-located with the
data-repo root found by ``.env`` discovery. Never secrets. Ordered
``cells`` list; each cell: ``id`` + ``erp`` + ``default_api`` + ``base_url``.
Unique ``id``s required. First cell is default when ``--cell`` is omitted
(T193). Present → ``load_instance`` sources ``Instance.api_version`` from
the active cell's ``default_api`` when ``--api-version`` is absent; missing
→ warn on check unless ``--strict``; invalid/empty → hard-fail any loader.
Never ``target.yaml`` (retired).
"""

from pathlib import Path

import yaml
from pydantic import Field, ValidationError, field_validator, model_validator

from .config import Instance, find_data_root
from .models import Model, validation_summary

MATRIX_FILENAME = "matrix.yaml"


class MatrixCell(Model):
    """One matrix cell: pin + non-secret where (V27)."""

    id: str  # unique within matrix; --cell selects this
    erp: str  # claimed product line/build; live compare when probe exists
    default_api: str  # Default contract version half only (e.g. 25.200.001)
    base_url: str  # REST root (scheme + host + site path); non-secret where

    @field_validator("id")
    @classmethod
    def _id_nonempty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("id must be a non-empty cell id")
        return v

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

    @field_validator("base_url")
    @classmethod
    def _base_url_nonempty(cls, v: str) -> str:
        v = v.strip().rstrip("/")
        if not v:
            raise ValueError(
                "base_url must be a non-empty REST root "
                "(e.g. https://erp.example.com/AcumaticaERP)"
            )
        return v


class DatasetMatrix(Model):
    """Ordered multi-cell pin+where registry (V27)."""

    cells: list[MatrixCell] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> DatasetMatrix:
        seen: dict[str, int] = {}
        dups: list[str] = []
        for cell in self.cells:
            if cell.id in seen:
                if cell.id not in dups:
                    dups.append(cell.id)
            else:
                seen[cell.id] = 1
        if dups:
            raise ValueError(
                f"duplicate cell id(s): {', '.join(dups)} "
                "(each cells[].id must be unique)"
            )
        return self


def find_matrix_path(root: Path | None = None) -> Path | None:
    """``{data-root}/matrix.yaml`` when the file exists; else None."""
    root = root if root is not None else find_data_root()
    if root is None:
        return None
    path = root / MATRIX_FILENAME
    return path if path.is_file() else None


def load_matrix(root: Path | None = None) -> DatasetMatrix | None:
    """Return DatasetMatrix, None if absent, or SystemExit on unreadable/invalid.

    Invalid/empty file is always a hard error for any caller that loads it —
    never silently ignored on apply while only failing on config check (V27).
    """
    path = find_matrix_path(root)
    if path is None:
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
    except OSError as exc:
        raise SystemExit(f"{path}: cannot read matrix.yaml: {exc}") from exc
    if data is None:
        raise SystemExit(f"{path}: matrix.yaml is empty")
    if not isinstance(data, dict):
        raise SystemExit(f"{path}: expected a mapping at the top level")
    try:
        return DatasetMatrix.model_validate(data)
    except ValidationError as exc:
        raise SystemExit(f"{path}: {validation_summary(exc)}") from exc


def active_cell(matrix: DatasetMatrix, cell_id: str | None = None) -> MatrixCell:
    """Select a cell by id, or the first cell when ``cell_id`` is omitted (V27).

    Unknown id → SystemExit naming known ids.
    """
    if cell_id is None:
        return matrix.cells[0]
    for cell in matrix.cells:
        if cell.id == cell_id:
            return cell
    known = ", ".join(c.id for c in matrix.cells)
    raise SystemExit(f"unknown matrix cell {cell_id!r}; known ids: {known}")


def assert_matrix_compatible(inst: Instance, root: Path | None = None) -> None:
    """Load matrix when present; invalid → hard-fail (V27).

    Version mismatch gate retired (T125): ``load_instance`` source-merges
    active-cell ``default_api`` into ``api_version`` when the flag is
    absent; flag override is intentional for ad-hoc probes. Missing matrix
    is not an error here (config check --strict owns that). Call only from
    the allowlisted data-plane commands — never from bare pass_instance.
    """
    del inst  # kept for call-site stability; version match no longer used
    load_matrix(root)
