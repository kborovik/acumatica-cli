"""V12 human-facing docs: env-sole-config, survey group, no wrap check."""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# L1 extract/inventory/reconcile after optional globals.
# `acu survey extract` is not a hit.
_L1_SURVEY_VERB = re.compile(
    r"\bacu(?:\s+(?:--[a-z-]+(?:\s+\S+)?))*\s+(extract|inventory|reconcile)\b"
)


def _unreleased(changelog: str) -> str:
    start = changelog.index("## Unreleased") + len("## Unreleased")
    rest = changelog[start:]
    nxt = rest.find("\n## ")
    return rest if nxt < 0 else rest[:nxt]


def test_no_retired_host_literals() -> None:
    """T261/V12/V27: live instance from .env; no host/sibling literals."""
    bits = (
        "acu" + "-dev1",
        "vm" + ".internal",
        "acumatica" + "-infra",
        "acumatica" + "-blog",
        "acumatica" + "-devops",
    )
    pat = re.compile("|".join(re.escape(b) for b in bits))
    hits: list[str] = []
    roots = [REPO / "README.md", REPO / "src", REPO / "tests"]
    files: list[Path] = []
    for root in roots:
        if root.is_file():
            files.append(root)
        else:
            files.extend(p for p in root.rglob("*") if p.is_file())
    for path in files:
        if path.suffix not in {".py", ".cs", ".md", ".yaml", ".yml"}:
            continue
        text = path.read_text()
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                rel = path.relative_to(REPO).as_posix()
                hits.append(f"{rel}:{i}: {line.strip()}")
    assert hits == [], "retired host/sibling literals:\n" + "\n".join(hits)


def test_docs_cli_test_seed_soak() -> None:
    """T258/V12/V2: soak from this checkout; never CNBN; e2e still templates."""
    agents = (REPO / "AGENTS.md").read_text()
    readme = (REPO / "README.md").read_text()
    assert "has no GitOps seed" not in agents
    assert "Do not `acu apply`" not in agents
    assert "cd ~/github/acu-gitops-qms" not in agents
    assert "--tenant ACUCLI" in agents
    assert "Do not apply, delete, or rebuild `CNBN`" in agents
    assert "Do not `cd` the sibling for CLI soak" in agents
    assert "packaged `config init` templates" in agents
    assert "--tenant ACUCLI" in readme
    assert "Never use tenant `CNBN`" in readme
    assert "Never `cd` the sibling GitOps repo for CLI soak" in readme
    assert "packaged `config init` templates" in readme


def test_cli_test_seed_trees_present() -> None:
    """T259/V2/V13: repo-root seed trees; no config/qms; e2e still 3 files."""
    config = REPO / "config"
    for name in ("bootstrap", "baseline", "setup", "master", "views"):
        d = config / name
        assert d.is_dir(), d
        assert list(d.glob("*.yaml")), d
    assert not (config / "qms").exists()
    scenario = REPO / "scenario"
    assert scenario.is_dir()
    assert list(scenario.glob("*.yaml"))
    names = sorted(p.name for p in (REPO / "tests" / "e2e").glob("test_*.py"))
    assert names == [
        "test_extract_roundtrip.py",
        "test_provision_lifecycle.py",
        "test_scenario_lifecycle.py",
    ]
    numbering = (config / "master" / "05-numbering-sequences.yaml").read_text()
    assert "NewSymbol: '<NEW>'" in numbering
    assert numbering.count("NumberingID:") == numbering.count("NewSymbol:")


def test_e2e_pipeline_files_only() -> None:
    """T256: live e2e is three pipeline files; per-bug probes fold onto them."""
    names = sorted(p.name for p in (REPO / "tests" / "e2e").glob("test_*.py"))
    assert names == [
        "test_extract_roundtrip.py",
        "test_provision_lifecycle.py",
        "test_scenario_lifecycle.py",
    ]
    readme = (REPO / "README.md").read_text()
    assert "three pipeline files" in readme
    assert "test_extract_roundtrip" in readme


def test_readme_help_unreleased_no_docs_links() -> None:
    """T262/V12/V19/V49: README, root help, Unreleased have no docs/*.md links."""
    pat = re.compile(r"docs/" + r"[\w.-]+\.md")
    readme = (REPO / "README.md").read_text()
    help_text = (REPO / "src" / "acumatica_cli" / "cli.py").read_text()
    unreleased = _unreleased((REPO / "CHANGELOG.md").read_text())
    hits: list[str] = []
    for label, text in (
        ("README.md", readme),
        ("src/acumatica_cli/cli.py", help_text),
        ("CHANGELOG.md Unreleased", unreleased),
    ):
        for i, line in enumerate(text.splitlines(), 1):
            if pat.search(line):
                hits.append(f"{label}:{i}: {line.strip()}")
    assert hits == [], "docs/*.md links:\n" + "\n".join(hits)
    changelog = (REPO / "CHANGELOG.md").read_text()
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "docs/ tree" in changelog


def test_docs_tree_dropped() -> None:
    """T260/V12: never docs/ tree as SoT."""
    docs = REPO / "docs"
    assert not docs.exists() or not list(docs.glob("*.md"))


def test_src_tests_drop_docs_path_cites() -> None:
    """T260/V12: remaining docs/*.md cites swept from src/ and tests/."""
    stems = "ac-exe", "demo-seed", "rest-api"
    pat = re.compile("docs/" + "(?:" + "|".join(stems) + r")\.md")
    hits: list[str] = []
    for root in (REPO / "src", REPO / "tests"):
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in {".py", ".cs", ".md"}:
                continue
            text = path.read_text()
            for i, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    rel = path.relative_to(REPO).as_posix()
                    hits.append(f"{rel}:{i}: {line.strip()}")
    assert hits == [], "docs/ path cites:\n" + "\n".join(hits)


def test_docs_user_role_membership_persist() -> None:
    """T229/V12/V19/V53: human-facing notes name AssignUser persist."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    readme = (REPO / "README.md").read_text()
    templates = (REPO / "src" / "acumatica_cli" / "templates" / "README.md").read_text()
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "gh #35" in changelog
    assert "AssignUser" in changelog
    assert "92-role-users" in readme
    assert "92-role-users" in templates


def test_docs_numbering_newsymbol_insert() -> None:
    """T255/V12/V19/V58: changelog NumberingSequence NewSymbol."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "gh #44" in changelog
    assert "NewSymbol" in changelog
    assert "Bootstrap `1.11.0`" in changelog
    assert "not returned by endpoint" in changelog


def test_docs_membership_diff_empty() -> None:
    """T250/V12/V19/V57: changelog skip empty membership GET."""
    changelog = (REPO / "CHANGELOG.md").read_text()
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "gh #39" in changelog
    assert "acu diff` skips those details" in changelog
    assert "Users GET is empty" in changelog


def test_docs_drop_root_check() -> None:
    """T232/V12/V19/V47: human-facing docs compose cold rebuild; no acu check verb."""
    readme = (REPO / "README.md").read_text()
    templates = (REPO / "src" / "acumatica_cli" / "templates" / "README.md").read_text()
    changelog = (REPO / "CHANGELOG.md").read_text()
    assert "acu check --yes" not in readme
    assert "check [--yes]" not in readme
    assert "| `check` |" not in readme
    assert "then `apply` then `run` then `diff`" in readme
    assert "acu check --yes" not in templates
    assert "tenant create" in templates
    # V19 promote empties Unreleased; persist note lives in the versioned section.
    assert "`acu check` (V47):** dropped" in changelog
    assert "`tenant create` then" in changelog


def test_docs_env_sole_config() -> None:
    """T219/V12/V27: human-facing docs pin via .env, not matrix.yaml."""
    readme = (REPO / "README.md").read_text()
    assert "[--cell" not in readme
    assert "check --all" not in readme
    assert "ACU_API_VERSION" in readme
    assert "| `matrix.yaml` |" not in readme
    assert "check [--all]" not in readme
    assert "--strict" not in readme
    assert "default_api" not in readme


def test_docs_survey_group() -> None:
    """T235/V15/V48/V19: docs nest extract/inventory/reconcile under survey."""
    scoped = [REPO / "README.md"]
    scoped.extend(
        sorted((REPO / "src" / "acumatica_cli" / "templates").rglob("README.md"))
    )
    hits: list[str] = []
    for path in scoped:
        text = path.read_text()
        for m in _L1_SURVEY_VERB.finditer(text):
            rel = path.relative_to(REPO).as_posix()
            line = text[: m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}: {m.group(0)}")
    assert hits == [], "L1 extract/inventory/reconcile CLI:\n" + "\n".join(hits)

    readme = (REPO / "README.md").read_text()
    assert re.search(r"^├── survey", readme, re.M)
    assert "`survey extract`" in readme
    assert "`survey inventory`" in readme
    assert "`survey reconcile`" in readme
    assert re.search(r"^├── extract", readme, re.M) is None
    assert re.search(r"^├── inventory", readme, re.M) is None
    assert re.search(r"^├── reconcile", readme, re.M) is None

    changelog = (REPO / "CHANGELOG.md").read_text()
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "`acu survey`" in changelog
    assert "`survey` group (V15)" in changelog
    assert "extract" in changelog
    assert "inventory" in changelog
    assert "reconcile" in changelog
    assert _L1_SURVEY_VERB.search(_unreleased(changelog)) is None
