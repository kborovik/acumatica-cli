"""Opt-in resume file (V67, gh #71): load, match, append."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from acumatica_cli.checkpoint import Checkpoint, digest


def _line(tenant: str, unit: str, content: object) -> str:
    return json.dumps({"tenant": tenant, "unit": unit, "digest": digest(content)})


def test_digest_ignores_key_order() -> None:
    """Canonical JSON: same content in another key order = same digest."""
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
    assert digest({"a": 1}) != digest({"a": 2})


def test_absent_file_is_empty_and_created_on_enter(tmp_path: Path) -> None:
    """V67: no file = nothing done; entering creates it for append."""
    path = tmp_path / "ckpt.jsonl"
    ckpt = Checkpoint(path, "T1")
    assert not ckpt.done("u", digest({}))
    with ckpt:
        assert path.exists()


def test_match_needs_same_tenant_unit_and_digest(tmp_path: Path) -> None:
    """V67: another tenant or an edited record (new digest) is not done."""
    path = tmp_path / "ckpt.jsonl"
    path.write_text(
        _line("T1", "UOM [KG]", {"UOM": "KG"})
        + "\n\n"
        + _line("T2", "UOM [LB]", {"UOM": "LB"})
        + "\n"
    )
    ckpt = Checkpoint(path, "T1")

    assert ckpt.done("UOM [KG]", digest({"UOM": "KG"}))
    assert not ckpt.done("UOM [KG]", digest({"UOM": "KG", "Descr": "x"}))
    assert not ckpt.done("UOM [LB]", digest({"UOM": "LB"}))
    assert ckpt.skipped == 1


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("not json\n", "ckpt.jsonl:1: malformed checkpoint line"),
        ('{"tenant": "T1", "unit": "u"}\n', "(needs tenant, unit, digest)"),
        ("[1, 2]\n", "(needs tenant, unit, digest)"),
    ],
)
def test_malformed_line_raises_with_line_number(
    tmp_path: Path, text: str, fragment: str
) -> None:
    """V67: a bad line fails at load (before any login), naming file:line."""
    path = tmp_path / "ckpt.jsonl"
    path.write_text(text)
    with pytest.raises(RuntimeError, match="malformed checkpoint line") as err:
        Checkpoint(path, "T1")
    assert fragment in str(err.value)


def test_record_appends_and_flushes_each_line(tmp_path: Path) -> None:
    """V67: each line is on disk once record() returns (kill-safe)."""
    path = tmp_path / "ckpt.jsonl"
    path.write_text(_line("T1", "old", {}) + "\n")
    with Checkpoint(path, "T1") as ckpt:
        ckpt.record("UOM [KG]", digest({"UOM": "KG"}))
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        assert rows[-1] == {
            "tenant": "T1",
            "unit": "UOM [KG]",
            "digest": digest({"UOM": "KG"}),
        }
        assert len(rows) == 2
        assert ckpt.done("UOM [KG]", digest({"UOM": "KG"}))
