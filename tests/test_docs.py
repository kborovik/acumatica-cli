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


def test_docs_user_role_membership_persist() -> None:
    """T229/V12/V19/V53: docs name AssignUser persist; drop T189 limit + Selected."""
    demo = (REPO / "docs" / "demo-seed.md").read_text()
    changelog = (REPO / "CHANGELOG.md").read_text()
    readme = (REPO / "README.md").read_text()
    templates = (REPO / "src" / "acumatica_cli" / "templates" / "README.md").read_text()
    assert "membership limit (T189)" not in demo
    assert "Selected: true" not in demo
    assert "AssignUser" in demo
    assert "92-role-users.yaml" in demo
    assert "UsersInRoles" in demo
    assert "RolesByUser" in demo
    # V19 promote empties Unreleased; persist notes live in the versioned section.
    assert "gh #35" in changelog
    assert "AssignUser" in changelog
    assert "92-role-users" in readme
    assert "92-role-users" in templates


def test_docs_membership_diff_empty() -> None:
    """T250/V12/V19/V57: Unreleased + demo-seed skip empty membership GET."""
    demo = (REPO / "docs" / "demo-seed.md").read_text()
    unreleased = _unreleased((REPO / "CHANGELOG.md").read_text())
    assert "gh #39" in unreleased
    assert "acu diff` skips those details" in unreleased
    assert "Users GET is empty" in unreleased
    assert "read path is confirmed" not in demo
    assert "`acu diff` skips those details" in demo
    assert "Users GET is empty" in demo
    assert "GET/diff detail `Roles`" not in demo
    assert "Roles GET/diff shape" not in demo


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
    rest = (REPO / "docs" / "rest-api.md").read_text()
    demo = (REPO / "docs" / "demo-seed.md").read_text()
    for text in (readme, rest, demo):
        assert "[--cell" not in text
        assert "check --all" not in text
        assert "ACU_API_VERSION" in text
    assert "| `matrix.yaml` |" not in readme
    assert "check [--all]" not in readme
    assert "--strict" not in readme
    assert "default_api" not in readme
    assert "There is no `ACU_API_VERSION`" not in rest
    assert "matrix.yaml `default_api`" not in demo
    assert "matrix.yaml default_api" not in demo


def test_docs_survey_group() -> None:
    """T235/V15/V48/V19: docs nest extract/inventory/reconcile under survey."""
    scoped = [REPO / "README.md"]
    scoped.extend(sorted((REPO / "docs").glob("*.md")))
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
