"""Shared fixtures: a fake Instance — no live Acumatica anywhere in the suite."""

import os

import pytest

from acumatica_cli.config import Instance


@pytest.fixture(autouse=True)
def isolate_acu_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermetic resolution: ambient ACU_* vars never leak into a test.

    Instance is a BaseSettings reading the ACU_* environment (I.cfg), so a
    developer's shell (ACU_TENANT, ACU_DEBUG, ...) would otherwise bleed
    into resolution-sensitive assertions. Tests that need a var set it
    explicitly after this scrub.
    """
    for key in [k for k in os.environ if k.startswith("ACU_")]:
        monkeypatch.delenv(key)


@pytest.fixture
def instance() -> Instance:
    """A target (required keys plus overrides) that never resolves to a real host."""
    return Instance(
        base_url="http://acu.test/AcumaticaERP",
        ssh="user@acu.test",
        tenant="T1",
        user="admin",
        password="pw",
    )
