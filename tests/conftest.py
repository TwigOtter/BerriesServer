"""
tests/conftest.py

Shared fixtures. Tracing is disabled for the whole suite so running tests
doesn't write trace files into logs/traces/ — tests/test_trace.py re-enables
it explicitly against a tmp_path.
"""

import pytest

from shared import trace


@pytest.fixture(autouse=True)
def _disable_tracing(monkeypatch):
    monkeypatch.setattr(trace, "TRACE_ENABLED", False)
