""".spec/scripts/check-all: the composed pre-commit audit runner.

One command runs every check-extras.md recipe (legs ascii, extras,
parity). Violations are pinned per leg via a synthetic repo tree in
tmp_path (runner + leg scripts copied under .spec/scripts/, minimal
src/acumatica_cli beside them): each synthetic violation fails exactly
its leg, names it, and exits 1; the clean tree and the real repo exit 0.
"""

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPTS = REPO / ".spec" / "scripts"

CLEAN_TENANT = (
    "import subprocess\n"
    "\n"
    "class TenantManager:\n"
    "    def _ssh(self, command: str) -> str:\n"
    '        return command + "\\nexit $LASTEXITCODE"\n'
)
CLEAN_CLIENT = "import httpx\n"
CLEAN_MODELS = "from pydantic import BaseModel\n\nclass Model(BaseModel):\n    pass\n"
PROJECT_XML = (
    "<Customization><EntityEndpoint>"
    '<Endpoint xmlns="x" name="Bootstrap" version="1.4.0"/>'
    "</EntityEndpoint></Customization>\n"
)


def make_repo(tmp_path: Path, **files: str) -> Path:
    """Synthetic repo: runner + leg scripts in place, clean src overridable."""
    scripts = tmp_path / ".spec" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("check-all", "check-ascii", "check-extras.sh"):
        shutil.copy(SCRIPTS / name, scripts / name)
    src = tmp_path / "src" / "acumatica_cli"
    src.mkdir(parents=True)
    content = {
        "tenant.py": CLEAN_TENANT,
        "client.py": CLEAN_CLIENT,
        "models.py": CLEAN_MODELS,
        "bootstrap_project.xml": PROJECT_XML,
    } | files
    for name, text in content.items():
        (src / name).write_text(text)
    return scripts / "check-all"


def run_all(
    script: Path = SCRIPTS / "check-all", *legs: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *legs],
        capture_output=True,
        text=True,
        check=False,
    )


def test_real_tree_every_leg_ok() -> None:
    r = run_all()
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout.splitlines() == [
        "check-all: ascii ok",
        "check-all: extras ok",
        "check-all: parity ok",
    ]
    r.stdout.encode("ascii")  # V9: the runner obeys the invariant it enforces


def test_synthetic_clean_tree_ok(tmp_path: Path) -> None:
    r = run_all(make_repo(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout


def test_ascii_violation_fails_ascii_leg_only(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"bad.py": 'MSG = "→"\n'})
    r = run_all(script)
    assert r.returncode == 1
    assert "check-all: ascii FAIL" in r.stdout
    assert "U+2192" in r.stdout  # the leg's surviving output streams through
    assert "check-all: extras ok" in r.stdout
    assert "check-all: parity ok" in r.stdout


def test_extras_violation_fails_extras_leg(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"tenant.py": "import httpx\n" + CLEAN_TENANT})
    r = run_all(script)
    assert r.returncode == 1
    assert "check-all: extras FAIL" in r.stdout
    assert "V1|VIOLATE" in r.stdout


# concatenated so this file never carries the literal itself - the real
# tree is inside the parity sweep's scope
STALE_REF = "Bootstrap/" + "0.9.9"


def test_parity_violation_fails_parity_leg(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"stale.py": f'EP = "{STALE_REF}"\n'})
    r = run_all(script)
    assert r.returncode == 1
    assert "check-all: parity FAIL" in r.stdout
    assert STALE_REF in r.stdout
    assert "check-all: ascii ok" in r.stdout


def test_parity_missing_version_fails(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"bootstrap_project.xml": "<Customization/>\n"})
    r = run_all(script)
    assert r.returncode == 1
    assert "check-all: parity FAIL" in r.stdout
    assert "no Endpoint version" in r.stdout


def test_leg_argument_runs_single_recipe(tmp_path: Path) -> None:
    r = run_all(make_repo(tmp_path), "parity")
    assert r.returncode == 0, r.stdout + r.stderr
    assert r.stdout == "check-all: parity ok\n"


def test_unknown_leg_errors(tmp_path: Path) -> None:
    r = run_all(make_repo(tmp_path), "bogus")
    assert r.returncode == 1
    assert "unknown leg 'bogus'" in r.stderr
    assert "ascii extras parity" in r.stderr
