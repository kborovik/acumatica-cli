"""SnapshotArtifact IR parse — offline (T127, V10/V35/V37)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from acumatica_cli import inventory

# Minimal ac.exe / SM203520 table XML (docs/ac-exe.md shape).
ACCOUNT_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Description" type="NVarChar(60)"/>
  </table>
  <rows>
    <row AccountCD="20000" Description="AP"/>
    <row AccountCD="10100" Description="Cash"/>
  </rows>
</data>
"""

CURRENCY_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Currency">
    <col name="CuryID" type="NVarChar(5)"/>
    <col name="Description" type="NVarChar(60)"/>
  </table>
  <rows>
    <row CuryID="USD" Description="US Dollar"/>
    <row CuryID="CAD" Description="Canadian Dollar"/>
  </rows>
</data>
"""

MANIFEST_XML = """\
<?xml version="1.0" encoding="utf-8"?>
<Snapshot>
  <Name>Lab5-Settings</Name>
  <Version>26.101.0225</Version>
  <ExportMode>Settings</ExportMode>
</Snapshot>
"""


def _write_folder(tmp: Path, files: dict[str, str]) -> Path:
    root = tmp / "export"
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")
    return root


def _write_zip(tmp: Path, files: dict[str, str], name: str = "snap.zip") -> Path:
    path = tmp / name
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for member, body in files.items():
            zf.writestr(member, body)
    return path


# -- folder (ac.exe export xml) --


def test_parse_folder_export_xml(tmp_path: Path) -> None:
    """V37: ac.exe export xml folder → one IR with sorted tables/rows."""
    folder = _write_folder(
        tmp_path,
        {"Currency.xml": CURRENCY_XML, "Account.xml": ACCOUNT_XML},
    )
    art = inventory.parse_artifact(folder)
    assert art.export_mode == "xml-folder"
    assert art.table_names == ["Account", "Currency"]  # alpha
    account = art.tables[0]
    assert account.name == "Account"
    assert [c.name for c in account.columns] == ["AccountCD", "Description"]
    # rows sorted by column values (10100 before 20000 despite file order)
    assert [r["AccountCD"] for r in account.rows] == ["10100", "20000"]
    assert art.row_counts == {"Account": 2, "Currency": 2}
    assert art.erp is None  # no manifest in bare export


def test_parse_folder_uses_filename_when_table_name_absent(tmp_path: Path) -> None:
    body = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table>
    <col name="ID" type="Int"/>
  </table>
  <rows>
    <row ID="1"/>
  </rows>
</data>
"""
    folder = _write_folder(tmp_path, {"MyTable.xml": body})
    art = inventory.parse_artifact(folder)
    assert art.table_names == ["MyTable"]


# -- ZIP (SM203520 XML) --


def test_parse_zip_with_manifest(tmp_path: Path) -> None:
    """V37: SM203520 XML ZIP (manifest.xml + table XML) → IR + erp."""
    zpath = _write_zip(
        tmp_path,
        {
            "manifest.xml": MANIFEST_XML,
            "Account.xml": ACCOUNT_XML,
            "Currency.xml": CURRENCY_XML,
        },
    )
    art = inventory.parse_artifact(zpath)
    assert art.export_mode == "xml-zip"
    assert art.erp == "26.101.0225"
    assert art.table_names == ["Account", "Currency"]
    assert art.row_counts["Account"] == 2


def test_parse_zip_nested_manifest(tmp_path: Path) -> None:
    """Manifest may live under a single top folder in the ZIP."""
    zpath = _write_zip(
        tmp_path,
        {
            "snap/manifest.xml": MANIFEST_XML,
            "snap/Account.xml": ACCOUNT_XML,
        },
    )
    art = inventory.parse_artifact(zpath)
    assert art.erp == "26.101.0225"
    assert art.table_names == ["Account"]


def test_parse_zip_requires_manifest(tmp_path: Path) -> None:
    zpath = _write_zip(tmp_path, {"Account.xml": ACCOUNT_XML})
    with pytest.raises(SystemExit, match=r"manifest\.xml"):
        inventory.parse_artifact(zpath)


# -- binary .adb reject (V37) --


def test_reject_adb_file(tmp_path: Path) -> None:
    adb = tmp_path / "tenant.adb"
    adb.write_bytes(b"\x00binary")
    with pytest.raises(SystemExit, match=r"\.adb"):
        inventory.parse_artifact(adb)


def test_reject_zip_containing_adb(tmp_path: Path) -> None:
    zpath = _write_zip(
        tmp_path,
        {"manifest.xml": MANIFEST_XML, "data.adb": "not-really-binary"},
    )
    with pytest.raises(SystemExit, match=r"\.adb"):
        inventory.parse_artifact(zpath)


def test_reject_folder_containing_adb(tmp_path: Path) -> None:
    folder = _write_folder(tmp_path, {"Account.xml": ACCOUNT_XML})
    (folder / "blob.adb").write_bytes(b"\x00")
    with pytest.raises(SystemExit, match=r"\.adb"):
        inventory.parse_artifact(folder)


# -- determinism --


def test_row_order_independent_of_xml_order(tmp_path: Path) -> None:
    """Same rows in different XML order → identical IR rows (V37)."""
    shuffled = """\
<?xml version="1.0" encoding="utf-8"?>
<data>
  <table name="Account">
    <col name="AccountCD" type="NVarChar(10)"/>
    <col name="Description" type="NVarChar(60)"/>
  </table>
  <rows>
    <row AccountCD="10100" Description="Cash"/>
    <row AccountCD="20000" Description="AP"/>
  </rows>
</data>
"""
    a = inventory.parse_artifact(
        _write_folder(tmp_path / "a", {"Account.xml": ACCOUNT_XML})
    )
    b = inventory.parse_artifact(
        _write_folder(tmp_path / "b", {"Account.xml": shuffled})
    )
    assert a.tables[0].rows == b.tables[0].rows
    assert a.model_dump(exclude={"source"}) == b.model_dump(exclude={"source"})


def test_table_order_independent_of_file_order(tmp_path: Path) -> None:
    """ZIP member order must not affect table order."""
    files_a = {
        "manifest.xml": MANIFEST_XML,
        "Account.xml": ACCOUNT_XML,
        "Currency.xml": CURRENCY_XML,
    }
    files_b = {
        "manifest.xml": MANIFEST_XML,
        "Currency.xml": CURRENCY_XML,
        "Account.xml": ACCOUNT_XML,
    }
    # Build ZIPs with deliberate member order via ZipFile
    za = tmp_path / "a.zip"
    zb = tmp_path / "b.zip"
    with zipfile.ZipFile(za, "w") as zf:
        for k, v in files_a.items():
            zf.writestr(k, v)
    with zipfile.ZipFile(zb, "w") as zf:
        for k, v in files_b.items():
            zf.writestr(k, v)
    da = inventory.parse_artifact(za).model_dump(exclude={"source"})
    db = inventory.parse_artifact(zb).model_dump(exclude={"source"})
    assert da == db


# -- erp pin (V37) --


def test_assert_erp_matches_ok() -> None:
    art = inventory.SnapshotArtifact(
        erp="26.101.0225",
        export_mode="xml-zip",
        source="x",
        tables=[],
    )
    inventory.assert_erp_matches(art, "26.101.0225")  # no raise


def test_assert_erp_matches_mismatch() -> None:
    art = inventory.SnapshotArtifact(
        erp="26.101.0225",
        export_mode="xml-zip",
        source="snap.zip",
        tables=[],
    )
    with pytest.raises(SystemExit, match=r"does not match target\.yaml erp"):
        inventory.assert_erp_matches(art, "25.200.001")


def test_assert_erp_matches_skips_when_artifact_erp_absent() -> None:
    art = inventory.SnapshotArtifact(
        erp=None,
        export_mode="xml-folder",
        source="export/",
        tables=[],
    )
    inventory.assert_erp_matches(art, "26.101.0225")  # no raise


# -- V10 models / V35 non-seed --


def test_models_forbid_extra() -> None:
    """V10: Model extra=forbid."""
    with pytest.raises(ValidationError):
        inventory.ColumnDef.model_validate({"name": "X", "bogus": 1})


def test_ir_has_no_endpoint_or_seed_shape(tmp_path: Path) -> None:
    """V35: IR is not seed — no endpoint: / entity / records keys."""
    folder = _write_folder(tmp_path, {"Account.xml": ACCOUNT_XML})
    art = inventory.parse_artifact(folder)
    dumped = art.model_dump()
    assert "endpoint" not in dumped
    assert "entity" not in dumped
    assert "records" not in dumped
    # table rows are raw maps, not seed records
    assert "AccountCD" in art.tables[0].rows[0]


def test_missing_path() -> None:
    with pytest.raises(SystemExit, match="not found"):
        inventory.parse_artifact(Path("/no/such/artifact-xyz"))


def test_empty_folder(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SystemExit, match="no table XML"):
        inventory.parse_artifact(empty)


def test_bad_root_element(tmp_path: Path) -> None:
    body = '<?xml version="1.0"?><notdata><table/></notdata>'
    folder = _write_folder(tmp_path, {"X.xml": body})
    with pytest.raises(SystemExit, match="expected root <data>"):
        inventory.parse_artifact(folder)


def test_manifest_upsnapshot_row_attrs(tmp_path: Path) -> None:
    """Manifest data-set / UPSnapshot row shape still yields erp."""
    manifest = """\
<?xml version="1.0" encoding="utf-8"?>
<data-set>
  <data>
    <UPSnapshot>
      <row Name="Lab5" Version="26.101.0225" ExportMode="Settings"/>
    </UPSnapshot>
  </data>
</data-set>
"""
    zpath = _write_zip(
        tmp_path,
        {"manifest.xml": manifest, "Account.xml": ACCOUNT_XML},
    )
    art = inventory.parse_artifact(zpath)
    assert art.erp == "26.101.0225"


def test_parse_returns_snapshot_artifact(tmp_path: Path) -> None:
    """Smoke: parse does not require writing inventory/ (T128 owns emit)."""
    folder = _write_folder(tmp_path, {"Account.xml": ACCOUNT_XML})
    art = inventory.parse_artifact(folder)
    assert isinstance(art, inventory.SnapshotArtifact)
