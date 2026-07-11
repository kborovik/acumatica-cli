"""Shared fixtures: a fake Instance — no live Acumatica anywhere in the suite."""

import pytest

from acumatica_cli.config import Instance


@pytest.fixture
def instance() -> Instance:
    """A target (required keys plus overrides) that never resolves to a real host."""
    return Instance(
        base_url="http://acu.test/AcumaticaERP",
        ssh="user@acu.test",
        tenant="T1",
        ac_exe="C:\\Acumatica\\ac.exe",
        db_name="AcuDB",
        username="admin",
        password="pw",
    )
