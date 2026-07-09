"""scripts/check-ascii: the mechanized V9 ASCII audit, fully offline.

Fixture files pin the exemption semantics the eye used to apply: comments
and docstrings never reach output, everything else does. The report format
is itself V9-bound - `file:line: U+XXXX`, never the offending glyph.
"""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "check-ascii"
SRC = Path(__file__).parent.parent / "src" / "acumatica_cli"


def run_script(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def test_py_string_literal_flags(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text('MSG = "done → next"\n')
    r = run_script(str(f))
    assert r.returncode == 1
    assert r.stdout == f"{f}:1: U+2192\n"


def test_py_comments_and_docstrings_exempt(tmp_path: Path) -> None:
    f = tmp_path / "ok.py"
    f.write_text(
        '"""Module docstring — exempt.\n\nSecond line → also.\n"""\n'
        "# comment — exempt\n"
        "def fn() -> None:\n"
        '    """Fn docstring — exempt."""\n'
        "    return None\n"
    )
    r = run_script(str(f))
    assert r.returncode == 0, r.stdout
    assert r.stdout == ""


def test_py_trailing_comment_exempt_code_on_same_line_flags(tmp_path: Path) -> None:
    exempt = tmp_path / "trail.py"
    exempt.write_text("x = 1  # arrow → in trailing comment\n")
    assert run_script(str(exempt)).returncode == 0
    flagged = tmp_path / "code.py"
    flagged.write_text('x = "é"  # ascii comment\n')
    r = run_script(str(flagged))
    assert r.returncode == 1
    assert r.stdout == f"{flagged}:1: U+00E9\n"


def test_py_non_docstring_string_statement_flags(tmp_path: Path) -> None:
    # a bare string that is NOT the first statement is no docstring
    f = tmp_path / "mid.py"
    f.write_text('x = 1\n"stray → string"\n')
    r = run_script(str(f))
    assert r.returncode == 1
    assert r.stdout == f"{f}:2: U+2192\n"


def test_cs_comment_line_exempt_code_flags(tmp_path: Path) -> None:
    f = tmp_path / "plugin.cs"
    f.write_text(
        '// comment — exempt\n    // indented comment → exempt\nvar s = "café";\n'
    )
    r = run_script(str(f))
    assert r.returncode == 1
    assert r.stdout == f"{f}:3: U+00E9\n"


def test_xml_comment_block_exempt_element_text_flags(tmp_path: Path) -> None:
    f = tmp_path / "project.xml"
    f.write_text(
        "<root>\n"
        "  <!-- multi-line comment —\n"
        "       still inside → exempt -->\n"
        "  <name>café</name>\n"
        "</root>\n"
    )
    r = run_script(str(f))
    assert r.returncode == 1
    assert r.stdout == f"{f}:4: U+00E9\n"


def test_dir_arg_recurses_over_known_suffixes(tmp_path: Path) -> None:
    sub = tmp_path / "pkg"
    sub.mkdir()
    (sub / "a.py").write_text('x = "→"\n')
    (sub / "b.cs").write_text('var s = "é";\n')
    (sub / "c.txt").write_text("ignored → not a known suffix\n")
    r = run_script(str(tmp_path))
    assert r.returncode == 1
    lines = r.stdout.splitlines()
    assert len(lines) == 2
    assert lines[0].endswith("a.py:1: U+2192")
    assert lines[1].endswith("b.cs:1: U+00E9")


def test_report_is_ascii_only(tmp_path: Path) -> None:
    f = tmp_path / "bad.py"
    f.write_text('x = "→—é"\n')
    r = run_script(str(f))
    assert r.returncode == 1
    r.stdout.encode("ascii")  # V9: the audit obeys the invariant it enforces


def test_missing_path_is_one_x_line(tmp_path: Path) -> None:
    r = run_script(str(tmp_path / "nope"))
    assert r.returncode == 1
    assert r.stdout == ""
    assert r.stderr.startswith("x ")


def test_real_src_tree_is_match_free() -> None:
    # pins the em-dash comments/docstrings in src/ as exempt (the check-extras
    # SV.9 recipe cmd runs exactly this)
    r = run_script(str(SRC))
    assert r.returncode == 0, r.stdout
    assert r.stdout == ""
