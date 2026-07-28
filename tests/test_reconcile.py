"""acu reconcile — offline findings from inventory/ + optional config/ (T129/T130)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from click.testing import CliRunner
from pydantic import ValidationError

from acumatica_cli import cli, inventory, reconcile

ACCOUNT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Description" type="NVarChar(60)"/>
    <col name="UsrMyFlag" type="Bit"/>
  </table>
  <rows>
    <row AccountCD="10100" Description="Cash" UsrMyFlag="1"/>
    <row AccountCD="20000" Description="AP" UsrMyFlag="0"/>
  </rows>
</data>
"""

ORPHAN_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="SomeInternalTable">
    <col name="ID" type="Int"/>
    <col name="Name" type="NVarChar(30)"/>
  </table>
  <rows>
    <row ID="1" Name="x"/>
  </rows>
</data>
"""

ACCOUNT_SEED = """\
entity: Account
key: AccountCD
records:
  - AccountCD: "10100"
    Description: Cash
  - AccountCD: "20000"
    Description: Accounts Payable
"""


def _inventory_tree(tmp: Path, *xml_bodies: str) -> Path:
    """Parse synthetic XML folder → emit inventory tree under tmp/inventory."""
    src = tmp / "export"
    src.mkdir(parents=True, exist_ok=True)
    for body in xml_bodies:
        name = "Table"
        if 'name="' in body:
            name = body.split('name="', 1)[1].split('"', 1)[0]
        (src / f"{name}.xml").write_text(body, encoding="utf-8")
    art = inventory.parse_artifact(src)
    out = tmp / "inventory"
    inventory.emit(art, out)
    return out


def _write_config_account(tmp: Path, body: str = ACCOUNT_SEED) -> Path:
    cfg = tmp / "config" / "baseline"
    cfg.mkdir(parents=True, exist_ok=True)
    path = cfg / "20-accounts.yaml"
    path.write_text(body, encoding="utf-8")
    return tmp / "config"


# -- loaders --


def test_load_inventory_tree(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    assert tree.by_name["Account"].rows[0]["AccountCD"] == "10100"
    assert "UsrMyFlag" in [c.name for c in tree.by_name["Account"].columns]


def test_load_inventory_missing_summary(tmp_path: Path) -> None:
    d = tmp_path / "inventory"
    d.mkdir()
    with pytest.raises(SystemExit, match=r"summary\.yaml"):
        reconcile.load_inventory_tree(d)


def test_load_config_seeds(tmp_path: Path) -> None:
    cfg = _write_config_account(tmp_path)
    seeds = reconcile.load_config_seeds(cfg)
    assert len(seeds) == 1
    assert seeds[0].entity == "Account"
    assert seeds[0].keys == ["AccountCD"]
    assert len(seeds[0].records) == 2


def test_load_config_skips_action_files(tmp_path: Path) -> None:
    setup = tmp_path / "config" / "setup"
    setup.mkdir(parents=True)
    (setup / "10-financial-year.yaml").write_text(
        "action: GenerateCalendar\nentity: MasterCalendar\n"
        "record: {FinancialYear: 2026}\n"
        "done_when: {filter: \"FinancialYear eq '2026'\"}\n",
        encoding="utf-8",
    )
    assert reconcile.load_config_seeds(tmp_path / "config") == []


# -- reconcile engine --


def test_unmapped_and_custom_columns(tmp_path: Path) -> None:
    """Unmapped table + Usr* on mapped Account (V35/V36 findings)."""
    inv = _inventory_tree(tmp_path, ACCOUNT_XML, ORPHAN_XML)
    tree = reconcile.load_inventory_tree(inv)
    bundle = reconcile.reconcile(tree, [], config_dir=None)
    names = {u.name for u in bundle.unmapped}
    assert "SomeInternalTable" in names
    assert "Account" not in names  # identity-mapped to catalog Account
    # config absent → rest_gaps for Account catalog rows
    assert any(g.entity == "Account" for g in bundle.rest_gaps)
    assert any(g.reason == "config absent" for g in bundle.rest_gaps)
    usr = [c for c in bundle.custom_columns if c.name == "Account"]
    assert usr
    assert "UsrMyFlag" in usr[0].columns


def test_rest_gap_seed_file_missing(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    # empty config/ tree present but no accounts seed
    cfg = tmp_path / "config"
    (cfg / "baseline").mkdir(parents=True)
    bundle = reconcile.reconcile(tree, [], config_dir=cfg)
    assert any(
        g.entity == "Account" and g.reason == "seed file missing"
        for g in bundle.rest_gaps
    )


def test_deltas_on_field_mismatch(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    cfg = _write_config_account(tmp_path)
    seeds = reconcile.load_config_seeds(cfg)
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg)
    # 20000 Description: seed "Accounts Payable" vs inventory "AP"
    assert any(d.field == "Description" and d.key == ["20000"] for d in bundle.deltas)
    # 10100 Description matches "Cash" — no delta
    assert not any(d.key == ["10100"] for d in bundle.deltas)
    # config present + seed found → no rest_gap for Account catalog file
    assert not any(
        g.entity == "Account" and "20-accounts" in g.file for g in bundle.rest_gaps
    )


def test_deltas_equal_when_values_match(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    body = """\
entity: Account
key: AccountCD
records:
  - AccountCD: "10100"
    Description: Cash
  - AccountCD: "20000"
    Description: AP
"""
    cfg = _write_config_account(tmp_path, body)
    seeds = reconcile.load_config_seeds(cfg)
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg)
    assert bundle.deltas == []


def test_snapshot_map_overrides_identity(tmp_path: Path) -> None:
    """snapshot_map can rename a table onto a catalog entity."""
    inv = _inventory_tree(tmp_path, ORPHAN_XML)
    tree = reconcile.load_inventory_tree(inv)
    smap = reconcile.SnapshotMap.model_validate(
        {"tables": [{"table": "SomeInternalTable", "entity": "Account"}]}
    )
    bundle = reconcile.reconcile(tree, [], config_dir=None, snapshot_map=smap)
    assert not any(u.name == "SomeInternalTable" for u in bundle.unmapped)
    assert any(g.entity == "Account" for g in bundle.rest_gaps)


def test_models_forbid_extra() -> None:
    with pytest.raises(ValidationError):
        reconcile.UnmappedTable.model_validate({"name": "X", "rows": 0, "bogus": 1})


def test_refuse_findings_under_config(tmp_path: Path) -> None:
    """V35/V36: never write findings into config/."""
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    cfg = _write_config_account(tmp_path)
    with pytest.raises(SystemExit, match=r"never writes seed|refuse"):
        reconcile.run(
            inventory_dir=inv,
            config_dir=cfg,
            out_dir=cfg / "findings",
        )


# -- emit --


def test_emit_writes_finding_files(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML, ORPHAN_XML)
    tree = reconcile.load_inventory_tree(inv)
    bundle = reconcile.reconcile(tree, [], config_dir=None)
    out = tmp_path / "findings"
    reconcile.emit(bundle, out)
    for name in reconcile.FINDING_FILES:
        assert (out / name).is_file(), name
    summary = yaml.safe_load((out / "summary.yaml").read_text(encoding="utf-8"))
    assert summary["tables"] == 2
    assert summary["findings"]["unmapped"] >= 1
    assert "endpoint" not in summary
    unmapped = yaml.safe_load((out / "unmapped.yaml").read_text(encoding="utf-8"))
    assert unmapped["kind"] == "unmapped"
    assert any(t["name"] == "SomeInternalTable" for t in unmapped["tables"])


def test_emit_skip_unless_force(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    bundle = reconcile.reconcile(tree, [], config_dir=None)
    out = tmp_path / "findings"
    reconcile.emit(bundle, out)
    (out / "summary.yaml").write_text("erp: dirty\n", encoding="utf-8")
    reconcile.emit(bundle, out)
    assert (out / "summary.yaml").read_text(encoding="utf-8") == "erp: dirty\n"
    reconcile.emit(bundle, out, force=True)
    assert "findings:" in (out / "summary.yaml").read_text(encoding="utf-8")


def test_emit_dry_run(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    bundle = reconcile.reconcile(tree, [], config_dir=None)
    out = tmp_path / "findings"
    reconcile.emit(bundle, out, dry_run=True)
    assert not out.exists()


# -- CLI --


def test_reconcile_help_documents_offline() -> None:
    result = CliRunner().invoke(cli.cli, ["reconcile", "--help"])
    assert result.exit_code == 0
    assert "offline" in result.output.lower() or "No REST" in result.output
    assert "--inventory" in result.output
    assert "--config" in result.output
    assert "--out" in result.output
    assert "--force" in result.output
    assert "--dry-run" in result.output
    assert "findings/" in result.output
    # T131/V35/V36: findings only — not extract promote, not state capture
    out_l = result.output.lower()
    assert "never writes config" in out_l or "not extract" in out_l


def test_cli_reconcile_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Offline CLI: no password; inventory/ + optional config/ → findings/."""
    _inventory_tree(tmp_path, ACCOUNT_XML, ORPHAN_XML)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["reconcile"])
    assert result.exit_code == 0, result.output
    assert (tmp_path / "findings" / "summary.yaml").is_file()
    assert (tmp_path / "findings" / "unmapped.yaml").is_file()
    assert "write" in result.output
    # never touch config/
    assert not (tmp_path / "config").exists() or not any(
        (tmp_path / "config").rglob("*")
    )


def test_cli_reconcile_with_config_deltas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _inventory_tree(tmp_path, ACCOUNT_XML)
    _write_config_account(tmp_path)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["reconcile"])
    assert result.exit_code == 0, result.output
    deltas = yaml.safe_load(
        (tmp_path / "findings" / "deltas.yaml").read_text(encoding="utf-8")
    )
    assert deltas["kind"] == "deltas"
    assert any(d["key"] == ["20000"] for d in deltas["deltas"])


def test_cli_reconcile_dry_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _inventory_tree(tmp_path, ACCOUNT_XML)
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["reconcile", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "would write" in result.output
    assert not (tmp_path / "findings").exists()


def test_cli_reconcile_missing_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(cli.cli, ["reconcile"])
    assert result.exit_code == 1
    assert "inventory" in result.output.lower() or "not found" in result.output


def test_cli_reconcile_custom_paths(tmp_path: Path) -> None:
    inv = _inventory_tree(tmp_path / "src", ACCOUNT_XML)
    # re-home: _inventory_tree puts inventory under src/inventory
    out = tmp_path / "out-findings"
    result = CliRunner().invoke(
        cli.cli,
        [
            "reconcile",
            "--inventory",
            str(inv),
            "--out",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "summary.yaml").is_file()


def test_findings_not_seed_shape(tmp_path: Path) -> None:
    """V35: findings YAML never carries endpoint:/entity:/records seed shape."""
    inv = _inventory_tree(tmp_path, ACCOUNT_XML)
    tree = reconcile.load_inventory_tree(inv)
    bundle = reconcile.reconcile(tree, [], config_dir=None)
    out = tmp_path / "findings"
    reconcile.emit(bundle, out)
    for path in out.glob("*.yaml"):
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert "endpoint" not in doc
        if path.name != "summary.yaml":
            assert doc.get("kind") in {
                "unmapped",
                "rest_gaps",
                "deltas",
                "custom_columns",
            }
