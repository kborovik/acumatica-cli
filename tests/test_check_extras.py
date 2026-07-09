""".claude/scripts/check-extras.sh: the mechanized V1/V10/V18 drift greps.

The hook resolves the repo root from its own location, so VIOLATE branches
are pinned via a synthetic repo tree in tmp_path (script copied under
.claude/scripts/, minimal src/acumatica_cli/ beside it). Row shape follows
the /sdd:check extras-hook contract: bare `id|verdict|evidence`, no header,
exit 1 on any VIOLATE.
"""

import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).parent.parent
SCRIPT = REPO / ".claude" / "scripts" / "check-extras.sh"

CLEAN_TENANT = (
    "import subprocess\n"
    "\n"
    "class TenantManager:\n"
    "    def _ssh(self, command: str) -> str:\n"
    '        return command + "\\nexit $LASTEXITCODE"\n'
)
CLEAN_CLIENT = "import httpx\n"
CLEAN_MODELS = "from pydantic import BaseModel\n\nclass Model(BaseModel):\n    pass\n"


def make_repo(tmp_path: Path, **files: str) -> Path:
    """Synthetic repo: hook in place, clean src overridden per keyword."""
    scripts = tmp_path / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / "check-extras.sh")
    src = tmp_path / "src" / "acumatica_cli"
    src.mkdir(parents=True)
    content = {
        "tenant.py": CLEAN_TENANT,
        "client.py": CLEAN_CLIENT,
        "models.py": CLEAN_MODELS,
    } | files
    for name, text in content.items():
        (src / name).write_text(text)
    return scripts / "check-extras.sh"


def run_hook(script: Path = SCRIPT) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script)],
        capture_output=True,
        text=True,
        check=False,
    )


def rows(r: subprocess.CompletedProcess[str]) -> dict[str, tuple[str, str]]:
    out: dict[str, tuple[str, str]] = {}
    for line in r.stdout.splitlines():
        rid, verdict, evidence = line.split("|", 2)
        out[rid] = (verdict, evidence)
    return out


def test_real_tree_emits_three_hold_rows() -> None:
    r = run_hook()
    assert r.returncode == 0, r.stdout + r.stderr
    table = rows(r)
    assert set(table) == {"V1", "V10", "V18"}
    assert all(verdict == "HOLD" for verdict, _ in table.values())
    assert len(r.stdout.splitlines()) == 3  # no header, no prose
    r.stdout.encode("ascii")  # V9: the audit obeys the invariant it enforces


def test_synthetic_clean_tree_holds(tmp_path: Path) -> None:
    r = run_hook(make_repo(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
    assert all(verdict == "HOLD" for verdict, _ in rows(r).values())


def test_v1_httpx_in_tenant_violates(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"tenant.py": "import httpx\n" + CLEAN_TENANT})
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V1"]
    assert verdict == "VIOLATE"
    assert "tenant.py:1" in evidence


def test_v1_subprocess_in_client_violates(tmp_path: Path) -> None:
    script = make_repo(tmp_path, **{"client.py": "import subprocess\nimport httpx\n"})
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V1"]
    assert verdict == "VIOLATE"
    assert "client.py:1" in evidence


def test_v10_basemodel_outside_models_violates(tmp_path: Path) -> None:
    script = make_repo(
        tmp_path,
        **{
            "seed.py": (
                "from pydantic import BaseModel\n\nclass Foo(BaseModel):\n    pass\n"
            )
        },
    )
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V10"]
    assert verdict == "VIOLATE"
    assert "seed.py:3" in evidence
    # models.py's own `class Model(BaseModel)` stays exempt
    assert rows(r)["V1"][0] == "HOLD"


def test_v18_hand_appended_call_site_violates(tmp_path: Path) -> None:
    script = make_repo(
        tmp_path,
        **{"bootstrap.py": 'CMD = "publish\\nexit $LASTEXITCODE"\n'},
    )
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V18"]
    assert verdict == "VIOLATE"
    assert "bootstrap.py:1" in evidence


def test_v18_missing_suffix_violates(tmp_path: Path) -> None:
    script = make_repo(
        tmp_path,
        **{"tenant.py": "import subprocess\n\nclass TenantManager:\n    pass\n"},
    )
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V18"]
    assert verdict == "VIOLATE"
    assert "choke point gone" in evidence


def test_v18_suffix_outside_ssh_def_violates(tmp_path: Path) -> None:
    script = make_repo(
        tmp_path,
        **{
            "tenant.py": (
                "class TenantManager:\n"
                "    def list(self) -> str:\n"
                '        return "q\\nexit $LASTEXITCODE"\n'
            )
        },
    )
    r = run_hook(script)
    assert r.returncode == 1
    verdict, evidence = rows(r)["V18"]
    assert verdict == "VIOLATE"
    assert "list" in evidence
