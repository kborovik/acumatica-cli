"""Opt-in resume file for bulk ``apply`` / ``run`` (V67, gh #71).

The only marker the CLI ever keeps (V4 exception). The operator passes
``--checkpoint PATH``; nothing reads or writes it otherwise. One JSON line
per unit done: ``{"tenant", "unit", "digest"}``. A line written after a
unit succeeds makes the next run skip that unit - but only on the same
tenant with the same content digest, so an edited record re-applies.

Lines are appended and flushed one at a time, so a killed load keeps
every line it finished. A kill mid-write can leave a torn last line (no
newline): load drops it (that unit runs again) unless it parses, and the
next append starts on a fresh line. Workers of a ``--concurrency`` pool share one
file through a lock. Tenant names match case-insensitively, as login does.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from types import TracebackType
from typing import IO, Any

from . import output

_FIELDS = ("tenant", "unit", "digest")


def digest(content: Any) -> str:
    """sha256 of canonical JSON (sorted keys, no spaces)."""
    text = json.dumps(content, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(text.encode()).hexdigest()


class Checkpoint:
    """Units already done on ``tenant``; appends new ones as they finish.

    Construction reads and validates the whole file (absent = empty), so
    a malformed line fails before any login or HTTP.
    """

    def __init__(self, path: Path, tenant: str) -> None:
        self.path = path
        self.tenant = tenant
        self.skipped = 0
        self._done: set[tuple[str, str]] = set()
        self._lock = threading.Lock()
        self._file: IO[str] | None = None
        self._torn = False
        if path.exists():
            text = path.read_text(encoding="utf-8")
            lines = text.split("\n")
            last = lines.pop()  # "" when the file ends with a newline
            for n, line in enumerate(lines, start=1):
                if line.strip():
                    self._load(n, line)
            self._torn = last != ""
            if last.strip():
                self._load_last(len(lines) + 1, last)

    def _load(self, n: int, line: str) -> None:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as err:
            raise RuntimeError(f"{self.path}:{n}: malformed checkpoint line") from err
        if not isinstance(row, dict) or not all(
            isinstance(row.get(k), str) for k in _FIELDS
        ):
            need = ", ".join(_FIELDS)
            raise RuntimeError(
                f"{self.path}:{n}: malformed checkpoint line (needs {need})"
            )
        if row["tenant"].casefold() == self.tenant.casefold():
            self._done.add((row["unit"], row["digest"]))

    def _load_last(self, n: int, line: str) -> None:
        """A final line with no newline: keep it if whole, drop it if torn."""
        try:
            self._load(n, line)
        except RuntimeError:
            output.info(f"{self.path}:{n}: torn last line ignored (kill mid-write)")

    def done(self, unit: str, unit_digest: str) -> bool:
        """True when this unit with this content already finished; counts a skip."""
        with self._lock:
            hit = (unit, unit_digest) in self._done
            self.skipped += hit
        return hit

    def record(self, unit: str, unit_digest: str) -> None:
        """Append one line and flush it now (kill-safe)."""
        line = json.dumps(
            {"tenant": self.tenant, "unit": unit, "digest": unit_digest},
            separators=(",", ":"),
        )
        assert self._file is not None, "record() needs the context manager"
        with self._lock:
            self._file.write(line + "\n")
            self._file.flush()
            self._done.add((unit, unit_digest))

    def __enter__(self) -> Checkpoint:
        """Open for append; an absent file is created here."""
        self._file = self.path.open("a", encoding="utf-8")
        if self._torn:
            self._file.write("\n")  # keep the torn fragment off the next line
            self._file.flush()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc: BaseException | None = None,
        tb: TracebackType | None = None,
    ) -> None:
        if self._file is not None:
            self._file.close()
