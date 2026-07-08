"""Shared fixtures: a fake Instance — no live Acumatica anywhere in the suite."""

import pytest

from acumatica_cli.config import Instance


@pytest.fixture
def instance() -> Instance:
    """A fully populated target that never resolves to a real host."""
    return Instance(
        name="test",
        base_url="http://acu.test/AcumaticaERP",
        endpoint="Default/25.200.001",
        tenant="T1",
        ssh="user@acu.test",
        ac_exe="C:\\Acumatica\\ac.exe",
        instance_name="AcumaticaERP",
        instance_path="C:\\Acumatica\\AcumaticaERP",
        db_name="AcuDB",
        username="admin",
        password="pw",
    )
