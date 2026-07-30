"""scripts/changelog — V19 Keep-a-Changelog promote / notes / empty hard-fail.

Offline only: fixture CHANGELOG via CHANGELOG_PATH. Makefile + release.yml
wiring is asserted as source contracts (no live tag push).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).parent.parent
SCRIPT = REPO / "scripts" / "changelog"
MAKEFILE = REPO / "Makefile"

SAMPLE = """\
# Changelog

## Unreleased

### Added

- **Ship feature:** does the thing.

### Fixed

- Typo in help.

## [v0.1.0] - 2026-01-01

### Added

- First cut.
"""

EMPTY_UNRELEASED = """\
# Changelog

## Unreleased

## [v0.1.0] - 2026-01-01

### Added

- First cut.
"""

EMPTY_HEADERS_ONLY = """\
# Changelog

## Unreleased

### Added

### Changed

## [v0.1.0] - 2026-01-01

### Added

- First cut.
"""


@pytest.fixture
def cl(tmp_path: Path) -> Path:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(SAMPLE)
    return path


def run(
    *args: str, changelog: Path | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if changelog is not None:
        env["CHANGELOG_PATH"] = str(changelog)
    return subprocess.run(
        [str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
        check=check,
    )


def test_check_ok_when_bullets(cl: Path) -> None:
    r = run("check", changelog=cl)
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_check_fails_when_empty_unreleased(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(EMPTY_UNRELEASED)
    r = run("check", changelog=path)
    assert r.returncode == 1
    assert "nothing to ship" in r.stderr


def test_check_fails_when_only_empty_h3(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(EMPTY_HEADERS_ONLY)
    r = run("check", changelog=path)
    assert r.returncode == 1
    assert "nothing to ship" in r.stderr


def test_promote_moves_body_leaves_empty_unreleased(cl: Path) -> None:
    r = run("promote", "0.2.0", "2026-07-30", changelog=cl)
    assert r.returncode == 0, r.stderr
    text = cl.read_text()
    assert text.index("## Unreleased") < text.index("## [v0.2.0] - 2026-07-30")
    assert text.index("## [v0.2.0] - 2026-07-30") < text.index("## [v0.1.0]")
    # Unreleased body is empty (no bullets between Unreleased and next H2)
    after = text.split("## Unreleased", 1)[1]
    before_next = after.split("## [", 1)[0]
    assert "- " not in before_next
    assert "Ship feature" in text
    assert "Ship feature" in text.split("## [v0.2.0]", 1)[1]
    assert "First cut" in text.split("## [v0.1.0]", 1)[1]


def test_promote_empty_fails_no_write(tmp_path: Path) -> None:
    path = tmp_path / "CHANGELOG.md"
    path.write_text(EMPTY_UNRELEASED)
    before = path.read_text()
    r = run("promote", "0.2.0", "2026-07-30", changelog=path)
    assert r.returncode == 1
    assert path.read_text() == before


def test_promote_accepts_v_prefix(cl: Path) -> None:
    r = run("promote", "v0.2.0", "2026-07-30", changelog=cl)
    assert r.returncode == 0, r.stderr
    assert "## [v0.2.0] - 2026-07-30" in cl.read_text()


def test_notes_extracts_version_section(cl: Path) -> None:
    r = run("notes", "0.1.0", changelog=cl)
    assert r.returncode == 0, r.stderr
    assert "First cut" in r.stdout
    assert "Ship feature" not in r.stdout
    assert "## [" not in r.stdout  # body only, no H2


def test_notes_accepts_tag_form(cl: Path) -> None:
    r = run("notes", "v0.1.0", changelog=cl)
    assert r.returncode == 0, r.stderr
    assert "First cut" in r.stdout


def test_notes_missing_section_fails(cl: Path) -> None:
    r = run("notes", "9.9.9", changelog=cl)
    assert r.returncode == 1
    assert "no " in r.stderr


def test_notes_after_promote(cl: Path) -> None:
    assert run("promote", "0.2.0", "2026-07-30", changelog=cl).returncode == 0
    r = run("notes", "v0.2.0", changelog=cl)
    assert r.returncode == 0, r.stderr
    assert "Ship feature" in r.stdout
    assert "Typo in help" in r.stdout
    assert "First cut" not in r.stdout


def test_makefile_release_promotes_changelog() -> None:
    """V19: make release checks + promotes CHANGELOG and commits it w/ pyproject."""
    text = MAKEFILE.read_text()
    assert "scripts/changelog check" in text
    assert "scripts/changelog promote" in text
    assert "CHANGELOG.md" in text
    # never local gh release create
    assert "gh release create" not in text
