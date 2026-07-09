"""Shared fixtures: a fake Instance — no live Acumatica anywhere in the suite."""

import pytest

from acumatica_cli.config import Instance


@pytest.fixture
def instance() -> Instance:
    """A host-derived target (plus overrides) that never resolves to a real host."""
    return Instance(
        host="acu.test",
        tenant="T1",
        ssh="user@acu.test",
        ac_exe="C:\\Acumatica\\ac.exe",
        db_name="AcuDB",
        username="admin",
        password="pw",
    )
