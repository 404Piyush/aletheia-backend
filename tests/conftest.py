"""Functional tests run with RBAC open (they test behavior, not authz).
test_rbac.py closes it explicitly and pins the gate itself."""

import pytest


@pytest.fixture(autouse=True)
def _rbac_open_for_functional_tests(monkeypatch):
    import backend.main as _main
    monkeypatch.setattr(_main, "RBAC_OPEN", True)
