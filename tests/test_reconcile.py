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


def test_pad_trim_key_join_and_fields(tmp_path: Path) -> None:
    """V38/T132: pad-trim both sides — padded inv keys join; field pad no delta."""
    # NVarChar-class padding on AccountCD + Description (common in XML dumps).
    padded_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Description" type="NVarChar(60)"/>
  </table>
  <rows>
    <row AccountCD="10100   " Description="Cash  "/>
    <row AccountCD="20000   " Description="AP"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, padded_xml)
    tree = reconcile.load_inventory_tree(inv)
    # raw inventory still carries padding (IR preserves XML text)
    assert tree.by_name["Account"].rows[0]["AccountCD"] == "10100   "
    body = """\
entity: Account
key: AccountCD
records:
  - AccountCD: "10100"
    Description: Cash
  - AccountCD: "20000"
    Description: Accounts Payable
"""
    cfg = _write_config_account(tmp_path, body)
    seeds = reconcile.load_config_seeds(cfg)
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg)
    # join must succeed despite key padding; 10100 Description pad-equal → no delta
    assert not any(d.key == ["10100"] for d in bundle.deltas)
    # 20000 still real mismatch after trim
    assert any(d.field == "Description" and d.key == ["20000"] for d in bundle.deltas)
    # reported key identity is pad-trimmed (stable findings)
    for d in bundle.deltas:
        assert d.key == [k.strip() for k in d.key]


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


def test_snapshot_map_v1_rows_still_load() -> None:
    """V38/T133: v1 {table, entity} only — keys/fields optional empty."""
    smap = reconcile.SnapshotMap.model_validate(
        {
            "tables": [
                {"table": "Sub", "entity": "Subaccount"},
                {"table": "UnitOfMeasure", "entity": "UnitsOfMeasure"},
            ]
        }
    )
    assert smap.entity_for("Sub") == "Subaccount"
    entry = smap.entry_for("Sub")
    assert entry is not None
    assert entry.keys == {}
    assert entry.fields == {}


def test_key_field_aliases_subaccount_and_uom(tmp_path: Path) -> None:
    """V38/T133: SubaccountCD→SubCD + UnitID→Unit / Description→Descr join."""
    sub_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Sub">
    <col name="SubCD" type="NVarChar(30)"/>
    <col name="Description" type="NVarChar(255)"/>
    <col name="Active" type="Bit"/>
  </table>
  <rows>
    <row SubCD="000000    " Description="Default" Active="1"/>
  </rows>
</data>
"""
    uom_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="UnitOfMeasure">
    <col name="Unit" type="NVarChar(6)"/>
    <col name="Descr" type="NVarChar(6)"/>
    <col name="L3Code" type="NVarChar(3)"/>
  </table>
  <rows>
    <row Unit="EA" Descr="Each" L3Code="EA"/>
    <row Unit="KG" Descr="Kilogram" L3Code="KGM"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, sub_xml, uom_xml)
    tree = reconcile.load_inventory_tree(inv)

    cfg = tmp_path / "config"
    (cfg / "baseline").mkdir(parents=True)
    (cfg / "baseline" / "10-subaccounts.yaml").write_text(
        "entity: Subaccount\n"
        "key: SubaccountCD\n"
        "records:\n"
        "  - SubaccountCD: '000000'\n"
        "    Description: Default\n",
        encoding="utf-8",
    )
    (cfg / "baseline" / "90-uoms.yaml").write_text(
        "entity: UnitsOfMeasure\n"
        "key: UnitID\n"
        "records:\n"
        "  - UnitID: EA\n"
        "    Description: Each\n"
        "    L3Code: EA\n"
        "  - UnitID: KG\n"
        "    Description: Kg\n"
        "    L3Code: KGM\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)

    # Without aliases: key names miss → rows skipped → no deltas (false quiet)
    bare = reconcile.SnapshotMap.model_validate(
        {
            "tables": [
                {"table": "Sub", "entity": "Subaccount"},
                {"table": "UnitOfMeasure", "entity": "UnitsOfMeasure"},
            ]
        }
    )
    quiet = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=bare)
    assert quiet.deltas == []

    smap = reconcile.SnapshotMap.model_validate(
        {
            "tables": [
                {
                    "table": "Sub",
                    "entity": "Subaccount",
                    "keys": {"SubaccountCD": "SubCD"},
                },
                {
                    "table": "UnitOfMeasure",
                    "entity": "UnitsOfMeasure",
                    "keys": {"UnitID": "Unit"},
                    "fields": {"Description": "Descr"},
                },
            ]
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    # Sub joins; Description matches after pad-trim → no Subaccount delta
    assert not any(d.entity == "Subaccount" for d in bundle.deltas)
    # UOM EA Description matches via Descr alias → no delta
    assert not any(
        d.entity == "UnitsOfMeasure" and d.key == ["EA"] for d in bundle.deltas
    )
    # UOM KG Description seed "Kg" vs inv "Kilogram" → real delta on seed field name
    assert any(
        d.entity == "UnitsOfMeasure"
        and d.field == "Description"
        and d.key == ["KG"]
        and d.seed == "Kg"
        and d.inventory == "Kilogram"
        for d in bundle.deltas
    )


def test_package_snapshot_map_loads_sub_and_uom_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Package defaults ship Sub + UnitOfMeasure aliases (T133) + M5 enums."""
    # No data-repo map → load package snapshot_map.yaml
    monkeypatch.chdir(tmp_path)
    smap = reconcile.load_snapshot_map()
    sub = smap.entry_for("Sub")
    assert sub is not None
    assert sub.entity == "Subaccount"
    assert sub.keys == {"SubaccountCD": "SubCD"}
    assert sub.enums.get("Active") == "bool_bit"
    uom = smap.entry_for("UnitOfMeasure")
    assert uom is not None
    assert uom.entity == "UnitsOfMeasure"
    assert uom.keys == {"UnitID": "Unit"}
    assert uom.fields == {"Description": "Descr"}
    # CreditTerms: no key aliases; enums for Due/Disc/Visible
    terms = smap.entry_for("Terms")
    assert terms is not None
    assert terms.entity == "CreditTerms"
    assert terms.keys == {}
    assert terms.enums.get("DueType") == "due_type"
    # T134: Account/Sub resolvers + ReasonCode/VendorClass resolves
    assert "account_cd" in smap.resolvers
    assert smap.resolvers["account_cd"].table == "Account"
    assert smap.resolvers["sub_cd"].cd == "SubCD"
    assert "branch_cd" in smap.resolvers
    rc = smap.entry_for("ReasonCode")
    assert rc is not None
    assert rc.resolves["AccountID"] == "account_cd"
    assert rc.resolves["SubID"] == "sub_cd"
    assert rc.enums.get("Usage") == "reason_usage"
    vc = smap.entry_for("VendorClass")
    assert vc is not None
    assert vc.resolves["APAcctID"] == "account_cd"
    assert vc.resolves["ExpenseSubID"] == "sub_cd"
    # T137/T138: enums + PostingClass resolves
    assert "reason_usage" in smap.enums
    assert smap.enums["reason_usage"]["Adjustment"] == "A"
    assert smap.enums["bool_bit"]["true"] == "1"
    pc = smap.entry_for("INPostClass")
    assert pc is not None
    assert pc.resolves["InvtAcctID"] == "account_cd"
    assert pc.resolves["SalesSubID"] == "sub_cd"
    acct = smap.entry_for("Account")
    assert acct is not None
    assert acct.enums["Type"] == "account_type"
    assert acct.enums["Active"] == "bool_bit"


def test_package_snapshot_map_roles_users_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T146: Roles/Users/UsersInRoles map to Bootstrap Role/User."""
    monkeypatch.chdir(tmp_path)
    smap = reconcile.load_snapshot_map()
    roles = smap.entry_for("Roles")
    assert roles is not None
    assert roles.entity == "Role"
    users = smap.entry_for("Users")
    assert users is not None
    assert users.entity == "User"
    assert users.enums.get("IsApproved") == "bool_bit"
    membership = smap.entry_for("UsersInRoles")
    assert membership is not None
    assert membership.entity == "User"


def test_package_snapshot_map_numbering_sequence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """T151: NumberingSequence (+ Numbering header) map to Bootstrap entity."""
    monkeypatch.chdir(tmp_path)
    smap = reconcile.load_snapshot_map()
    detail = smap.entry_for("NumberingSequence")
    assert detail is not None
    assert detail.entity == "NumberingSequence"
    header = smap.entry_for("Numbering")
    assert header is not None
    assert header.entity == "NumberingSequence"


def test_fk_resolve_reason_code_and_vendor_class(tmp_path: Path) -> None:
    """V38/T134: inv AccountID/SubID int → CD; match seed → 0 false deltas."""
    account_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountID" type="Int"/>
    <col name="AccountCD" type="NVarChar(10)"/>
  </table>
  <rows>
    <row AccountID="24" AccountCD="50300     "/>
    <row AccountID="10" AccountCD="20100     "/>
    <row AccountID="20" AccountCD="49100     "/>
    <row AccountID="21" AccountCD="50000     "/>
    <row AccountID="11" AccountCD="20200     "/>
  </rows>
</data>
"""
    sub_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Sub">
    <col name="SubID" type="Int"/>
    <col name="SubCD" type="NVarChar(30)"/>
  </table>
  <rows>
    <row SubID="1" SubCD="000000    "/>
  </rows>
</data>
"""
    reason_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="ReasonCode">
    <col name="ReasonCodeID" type="NVarChar(20)"/>
    <col name="Descr" type="NVarChar(60)"/>
    <col name="AccountID" type="Int"/>
    <col name="SubID" type="Int"/>
  </table>
  <rows>
    <row ReasonCodeID="INISSUE" Descr="Inventory issue" AccountID="24" SubID="1"/>
    <row ReasonCodeID="INRECEIPT" Descr="Inventory receipt" AccountID="24" SubID="1"/>
  </rows>
</data>
"""
    vendor_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="VendorClass">
    <col name="VendorClassID" type="NVarChar(10)"/>
    <col name="Descr" type="NVarChar(60)"/>
    <col name="APAcctID" type="Int"/>
    <col name="APSubID" type="Int"/>
    <col name="DiscTakenAcctID" type="Int"/>
    <col name="DiscTakenSubID" type="Int"/>
    <col name="ExpenseAcctID" type="Int"/>
    <col name="ExpenseSubID" type="Int"/>
    <col name="POAccrualAcctID" type="Int"/>
    <col name="POAccrualSubID" type="Int"/>
  </table>
  <rows>
    <row VendorClassID="SUPPLIER" Descr="Component suppliers"
         APAcctID="10" APSubID="1" DiscTakenAcctID="20" DiscTakenSubID="1"
         ExpenseAcctID="21" ExpenseSubID="1" POAccrualAcctID="11" POAccrualSubID="1"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, account_xml, sub_xml, reason_xml, vendor_xml)
    tree = reconcile.load_inventory_tree(inv)

    cfg = tmp_path / "config"
    (cfg / "master").mkdir(parents=True)
    (cfg / "master" / "10-reason-codes.yaml").write_text(
        "entity: ReasonCode\n"
        "key: ReasonCodeID\n"
        "endpoint: bootstrap\n"
        "records:\n"
        "  - ReasonCodeID: INISSUE\n"
        "    Descr: Inventory issue\n"
        "    AccountID: '50300'\n"
        "    SubID: '000000'\n"
        "  - ReasonCodeID: INRECEIPT\n"
        "    Descr: Inventory receipt\n"
        "    AccountID: '50300'\n"
        "    SubID: '000000'\n",
        encoding="utf-8",
    )
    (cfg / "master" / "70-vendor-classes.yaml").write_text(
        "entity: VendorClass\n"
        "key: VendorClassID\n"
        "endpoint: bootstrap\n"
        "records:\n"
        "  - VendorClassID: SUPPLIER\n"
        "    Descr: Component suppliers\n"
        "    APAcctID: '20100'\n"
        "    APSubID: '000000'\n"
        "    DiscTakenAcctID: '49100'\n"
        "    DiscTakenSubID: '000000'\n"
        "    ExpenseAcctID: '50000'\n"
        "    ExpenseSubID: '000000'\n"
        "    POAccrualAcctID: '20200'\n"
        "    POAccrualSubID: '000000'\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)

    # No resolvers → false CD-vs-int deltas
    bare = reconcile.SnapshotMap()
    false_deltas = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=bare)
    assert any(
        d.entity == "ReasonCode" and d.field == "AccountID" and d.inventory == "24"
        for d in false_deltas.deltas
    )
    assert any(
        d.entity == "VendorClass" and d.field == "APAcctID" and d.inventory == "10"
        for d in false_deltas.deltas
    )

    smap = reconcile.SnapshotMap.model_validate(
        {
            "resolvers": {
                "account_cd": {
                    "table": "Account",
                    "id": "AccountID",
                    "cd": "AccountCD",
                },
                "sub_cd": {"table": "Sub", "id": "SubID", "cd": "SubCD"},
            },
            "tables": [
                {
                    "table": "ReasonCode",
                    "entity": "ReasonCode",
                    "resolves": {
                        "AccountID": "account_cd",
                        "SubID": "sub_cd",
                    },
                },
                {
                    "table": "VendorClass",
                    "entity": "VendorClass",
                    "resolves": {
                        "APAcctID": "account_cd",
                        "APSubID": "sub_cd",
                        "DiscTakenAcctID": "account_cd",
                        "DiscTakenSubID": "sub_cd",
                        "ExpenseAcctID": "account_cd",
                        "ExpenseSubID": "sub_cd",
                        "POAccrualAcctID": "account_cd",
                        "POAccrualSubID": "sub_cd",
                    },
                },
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    # All *AcctID/*SubID false deltas gone when CD matches
    fk_fields = {
        "AccountID",
        "SubID",
        "APAcctID",
        "APSubID",
        "DiscTakenAcctID",
        "DiscTakenSubID",
        "ExpenseAcctID",
        "ExpenseSubID",
        "POAccrualAcctID",
        "POAccrualSubID",
    }
    assert not any(d.field in fk_fields for d in bundle.deltas)
    # Descr still compared; matches → no deltas at all
    assert bundle.deltas == []


def test_lab5_style_trim_alias_and_fk_resolve(tmp_path: Path) -> None:
    """V38/T135: combined pad-trim + aliases + Account/Sub FK → 0 when match."""
    # Inventory mimics LAB5 snapshot: padded CDs, Sub/UnitOfMeasure renames,
    # ReasonCode/VendorClass int FKs. Package map + resolve must zero false noise.
    account_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountID" type="Int"/>
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Description" type="NVarChar(60)"/>
  </table>
  <rows>
    <row AccountID="24" AccountCD="50300     " Description="COGS   "/>
    <row AccountID="10" AccountCD="20100     " Description="AP"/>
  </rows>
</data>
"""
    sub_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Sub">
    <col name="SubID" type="Int"/>
    <col name="SubCD" type="NVarChar(30)"/>
    <col name="Description" type="NVarChar(255)"/>
  </table>
  <rows>
    <row SubID="1" SubCD="000000    " Description="Default"/>
  </rows>
</data>
"""
    reason_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="ReasonCode">
    <col name="ReasonCodeID" type="NVarChar(20)"/>
    <col name="Descr" type="NVarChar(60)"/>
    <col name="AccountID" type="Int"/>
    <col name="SubID" type="Int"/>
  </table>
  <rows>
    <row ReasonCodeID="INISSUE" Descr="Inventory issue" AccountID="24" SubID="1"/>
  </rows>
</data>
"""
    vendor_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="VendorClass">
    <col name="VendorClassID" type="NVarChar(10)"/>
    <col name="Descr" type="NVarChar(60)"/>
    <col name="APAcctID" type="Int"/>
    <col name="APSubID" type="Int"/>
    <col name="DiscTakenAcctID" type="Int"/>
    <col name="DiscTakenSubID" type="Int"/>
    <col name="ExpenseAcctID" type="Int"/>
    <col name="ExpenseSubID" type="Int"/>
    <col name="POAccrualAcctID" type="Int"/>
    <col name="POAccrualSubID" type="Int"/>
  </table>
  <rows>
    <row VendorClassID="SUPPLIER" Descr="Component suppliers"
         APAcctID="10" APSubID="1" DiscTakenAcctID="10" DiscTakenSubID="1"
         ExpenseAcctID="24" ExpenseSubID="1" POAccrualAcctID="10" POAccrualSubID="1"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, account_xml, sub_xml, reason_xml, vendor_xml)
    tree = reconcile.load_inventory_tree(inv)

    cfg = tmp_path / "config"
    (cfg / "baseline").mkdir(parents=True)
    (cfg / "master").mkdir(parents=True)
    (cfg / "baseline" / "10-subaccounts.yaml").write_text(
        "entity: Subaccount\n"
        "key: SubaccountCD\n"
        "records:\n"
        "  - SubaccountCD: '000000'\n"
        "    Description: Default\n",
        encoding="utf-8",
    )
    (cfg / "baseline" / "20-accounts.yaml").write_text(
        "entity: Account\n"
        "key: AccountCD\n"
        "records:\n"
        "  - AccountCD: '50300'\n"
        "    Description: COGS\n"
        "  - AccountCD: '20100'\n"
        "    Description: AP\n",
        encoding="utf-8",
    )
    (cfg / "master" / "10-reason-codes.yaml").write_text(
        "entity: ReasonCode\n"
        "key: ReasonCodeID\n"
        "records:\n"
        "  - ReasonCodeID: INISSUE\n"
        "    Descr: Inventory issue\n"
        "    AccountID: '50300'\n"
        "    SubID: '000000'\n",
        encoding="utf-8",
    )
    (cfg / "master" / "70-vendor-classes.yaml").write_text(
        "entity: VendorClass\n"
        "key: VendorClassID\n"
        "records:\n"
        "  - VendorClassID: SUPPLIER\n"
        "    Descr: Component suppliers\n"
        "    APAcctID: '20100'\n"
        "    APSubID: '000000'\n"
        "    DiscTakenAcctID: '20100'\n"
        "    DiscTakenSubID: '000000'\n"
        "    ExpenseAcctID: '50300'\n"
        "    ExpenseSubID: '000000'\n"
        "    POAccrualAcctID: '20100'\n"
        "    POAccrualSubID: '000000'\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    # Use package-shaped map (aliases + resolvers) without cwd package load
    smap = reconcile.SnapshotMap.model_validate(
        {
            "resolvers": {
                "account_cd": {
                    "table": "Account",
                    "id": "AccountID",
                    "cd": "AccountCD",
                },
                "sub_cd": {"table": "Sub", "id": "SubID", "cd": "SubCD"},
            },
            "tables": [
                {
                    "table": "Sub",
                    "entity": "Subaccount",
                    "keys": {"SubaccountCD": "SubCD"},
                },
                {
                    "table": "ReasonCode",
                    "entity": "ReasonCode",
                    "resolves": {
                        "AccountID": "account_cd",
                        "SubID": "sub_cd",
                    },
                },
                {
                    "table": "VendorClass",
                    "entity": "VendorClass",
                    "resolves": {
                        "APAcctID": "account_cd",
                        "APSubID": "sub_cd",
                        "DiscTakenAcctID": "account_cd",
                        "DiscTakenSubID": "sub_cd",
                        "ExpenseAcctID": "account_cd",
                        "ExpenseSubID": "sub_cd",
                        "POAccrualAcctID": "account_cd",
                        "POAccrualSubID": "sub_cd",
                    },
                },
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    assert bundle.deltas == [], [d.model_dump() for d in bundle.deltas]


def test_fk_resolve_real_cd_mismatch_still_deltas(tmp_path: Path) -> None:
    """V38: resolved CD that differs from seed is a real delta (not swallowed)."""
    account_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountID" type="Int"/>
    <col name="AccountCD" type="NVarChar(10)"/>
  </table>
  <rows>
    <row AccountID="24" AccountCD="99999"/>
  </rows>
</data>
"""
    sub_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Sub">
    <col name="SubID" type="Int"/>
    <col name="SubCD" type="NVarChar(30)"/>
  </table>
  <rows>
    <row SubID="1" SubCD="000000"/>
  </rows>
</data>
"""
    reason_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="ReasonCode">
    <col name="ReasonCodeID" type="NVarChar(20)"/>
    <col name="AccountID" type="Int"/>
    <col name="SubID" type="Int"/>
  </table>
  <rows>
    <row ReasonCodeID="INISSUE" AccountID="24" SubID="1"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, account_xml, sub_xml, reason_xml)
    tree = reconcile.load_inventory_tree(inv)
    cfg = tmp_path / "config"
    (cfg / "master").mkdir(parents=True)
    (cfg / "master" / "10-reason-codes.yaml").write_text(
        "entity: ReasonCode\n"
        "key: ReasonCodeID\n"
        "records:\n"
        "  - ReasonCodeID: INISSUE\n"
        "    AccountID: '50300'\n"
        "    SubID: '000000'\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    smap = reconcile.SnapshotMap.model_validate(
        {
            "resolvers": {
                "account_cd": {
                    "table": "Account",
                    "id": "AccountID",
                    "cd": "AccountCD",
                },
                "sub_cd": {"table": "Sub", "id": "SubID", "cd": "SubCD"},
            },
            "tables": [
                {
                    "table": "ReasonCode",
                    "entity": "ReasonCode",
                    "resolves": {
                        "AccountID": "account_cd",
                        "SubID": "sub_cd",
                    },
                }
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    assert any(
        d.field == "AccountID" and d.seed == "50300" and d.inventory == "99999"
        for d in bundle.deltas
    )
    assert not any(d.field == "SubID" for d in bundle.deltas)


def test_enum_label_to_code_reason_code_usage(tmp_path: Path) -> None:
    """V38/T137: seed Usage label → DAC code; match inv → 0 false delta."""
    reason_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="ReasonCode">
    <col name="ReasonCodeID" type="NVarChar(20)"/>
    <col name="Usage" type="Char(1)"/>
  </table>
  <rows>
    <row ReasonCodeID="INISSUE" Usage="I"/>
    <row ReasonCodeID="INRECEIPT" Usage="R"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, reason_xml)
    tree = reconcile.load_inventory_tree(inv)
    cfg = tmp_path / "config"
    (cfg / "master").mkdir(parents=True)
    (cfg / "master" / "10-reason-codes.yaml").write_text(
        "entity: ReasonCode\n"
        "key: ReasonCodeID\n"
        "endpoint: bootstrap\n"
        "records:\n"
        "  - ReasonCodeID: INISSUE\n"
        "    Usage: Issue\n"
        "  - ReasonCodeID: INRECEIPT\n"
        "    Usage: Receipt\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    smap = reconcile.SnapshotMap.model_validate(
        {
            "enums": {
                "reason_usage": {
                    "Adjustment": "A",
                    "Issue": "I",
                    "Receipt": "R",
                }
            },
            "tables": [
                {
                    "table": "ReasonCode",
                    "entity": "ReasonCode",
                    "enums": {"Usage": "reason_usage"},
                }
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    assert not any(d.field == "Usage" for d in bundle.deltas)


def test_enum_bool_bit_and_account_type(tmp_path: Path) -> None:
    """V38/T137: Active true/1 and Type Asset/A normalize; real Type drift remains."""
    account_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Type" type="Char(1)"/>
    <col name="Active" type="Bit"/>
    <col name="RequireUnits" type="Bit"/>
    <col name="PostOption" type="Char(1)"/>
  </table>
  <rows>
    <row AccountCD="10100     " Type="A" Active="1" RequireUnits="0" PostOption="D"/>
    <row AccountCD="20000     " Type="L" Active="1" RequireUnits="0" PostOption="S"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, account_xml)
    tree = reconcile.load_inventory_tree(inv)
    cfg = tmp_path / "config"
    (cfg / "baseline").mkdir(parents=True)
    (cfg / "baseline" / "20-accounts.yaml").write_text(
        "entity: Account\n"
        "key: AccountCD\n"
        "records:\n"
        "  - AccountCD: '10100'\n"
        "    Type: Asset\n"
        "    Active: true\n"
        "    RequireUnits: false\n"
        "    PostOption: Detail\n"
        "  - AccountCD: '20000'\n"
        "    Type: Expense\n"
        "    Active: true\n"
        "    RequireUnits: false\n"
        "    PostOption: Summary\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    smap = reconcile.SnapshotMap.model_validate(
        {
            "enums": {
                "account_type": {
                    "Asset": "A",
                    "Liability": "L",
                    "Income": "I",
                    "Expense": "E",
                },
                "post_option": {"Summary": "S", "Detail": "D"},
                "bool_bit": {"true": "1", "false": "0"},
            },
            "tables": [
                {
                    "table": "Account",
                    "entity": "Account",
                    "enums": {
                        "Type": "account_type",
                        "PostOption": "post_option",
                        "Active": "bool_bit",
                        "RequireUnits": "bool_bit",
                    },
                }
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    # 10100 matches fully
    assert not any(d.key == ["10100"] for d in bundle.deltas)
    # 20000 seed Expense vs inv L (Liability) — real drift on Type only
    assert any(
        d.key == ["20000"]
        and d.field == "Type"
        and d.seed == "E"
        and d.inventory == "L"
        for d in bundle.deltas
    )
    assert not any(
        d.key == ["20000"] and d.field in ("Active", "RequireUnits", "PostOption")
        for d in bundle.deltas
    )


def test_posting_class_fk_resolve(tmp_path: Path) -> None:
    """V38/T138: PostingClass *AcctID/*SubID int → CD; match seed → 0 deltas."""
    account_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountID" type="Int"/>
    <col name="AccountCD" type="NVarChar(10)"/>
  </table>
  <rows>
    <row AccountID="4" AccountCD="12100     "/>
    <row AccountID="17" AccountCD="40000     "/>
    <row AccountID="21" AccountCD="50000     "/>
  </rows>
</data>
"""
    sub_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Sub">
    <col name="SubID" type="Int"/>
    <col name="SubCD" type="NVarChar(30)"/>
  </table>
  <rows>
    <row SubID="1" SubCD="000000    "/>
  </rows>
</data>
"""
    pc_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="INPostClass">
    <col name="PostClassID" type="NVarChar(10)"/>
    <col name="InvtAcctID" type="Int"/>
    <col name="InvtSubID" type="Int"/>
    <col name="SalesAcctID" type="Int"/>
    <col name="SalesSubID" type="Int"/>
    <col name="COGSAcctID" type="Int"/>
    <col name="COGSSubID" type="Int"/>
  </table>
  <rows>
    <row PostClassID="PARTS" InvtAcctID="4" InvtSubID="1"
         SalesAcctID="17" SalesSubID="1" COGSAcctID="21" COGSSubID="1"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, account_xml, sub_xml, pc_xml)
    tree = reconcile.load_inventory_tree(inv)
    cfg = tmp_path / "config"
    (cfg / "master").mkdir(parents=True)
    (cfg / "master" / "40-posting-classes.yaml").write_text(
        "entity: PostingClass\n"
        "key: PostClassID\n"
        "endpoint: bootstrap\n"
        "records:\n"
        "  - PostClassID: PARTS\n"
        "    InvtAcctID: '12100'\n"
        "    InvtSubID: '000000'\n"
        "    SalesAcctID: '40000'\n"
        "    SalesSubID: '000000'\n"
        "    COGSAcctID: '50000'\n"
        "    COGSSubID: '000000'\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    smap = reconcile.SnapshotMap.model_validate(
        {
            "resolvers": {
                "account_cd": {
                    "table": "Account",
                    "id": "AccountID",
                    "cd": "AccountCD",
                },
                "sub_cd": {"table": "Sub", "id": "SubID", "cd": "SubCD"},
            },
            "tables": [
                {
                    "table": "INPostClass",
                    "entity": "PostingClass",
                    "resolves": {
                        "InvtAcctID": "account_cd",
                        "InvtSubID": "sub_cd",
                        "SalesAcctID": "account_cd",
                        "SalesSubID": "sub_cd",
                        "COGSAcctID": "account_cd",
                        "COGSSubID": "sub_cd",
                    },
                }
            ],
        }
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    assert not any(d.entity == "PostingClass" for d in bundle.deltas)


def test_decimal_trailing_zero_compare(tmp_path: Path) -> None:
    """DiscPercent-style 0 vs 0.000000 collapses; bare CD 000000 stays a CD."""
    terms_xml = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Terms">
    <col name="TermsID" type="NVarChar(10)"/>
    <col name="DiscPercent" type="Decimal"/>
    <col name="DayDue00" type="SmallInt"/>
  </table>
  <rows>
    <row TermsID="NET30" DiscPercent="0.000000" DayDue00="30"/>
  </rows>
</data>
"""
    inv = _inventory_tree(tmp_path, terms_xml)
    tree = reconcile.load_inventory_tree(inv)
    cfg = tmp_path / "config"
    (cfg / "bootstrap").mkdir(parents=True)
    (cfg / "bootstrap" / "credit-terms.yaml").write_text(
        "entity: CreditTerms\n"
        "key: TermsID\n"
        "endpoint: bootstrap\n"
        "records:\n"
        "  - TermsID: NET30\n"
        "    DiscPercent: 0\n"
        "    DayDue00: 30\n",
        encoding="utf-8",
    )
    seeds = reconcile.load_config_seeds(cfg)
    smap = reconcile.SnapshotMap.model_validate(
        {"tables": [{"table": "Terms", "entity": "CreditTerms"}]}
    )
    bundle = reconcile.reconcile(tree, seeds, config_dir=cfg, snapshot_map=smap)
    assert not any(d.field == "DiscPercent" for d in bundle.deltas)


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
