"""Shared test setup."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "understanding"
SHIPPED = Path(__file__).parents[1] / "explanation-packages"


@pytest.fixture(autouse=True)
def _understanding_fixtures(request, monkeypatch):
    """Understanding tests see the synthetic fixture packages first, then the shipped ones."""
    if "understanding" in request.node.nodeid or "test_console.py" in request.node.nodeid:
        monkeypatch.setenv("LIF_EXPLANATION_PACKAGES", f"{FIXTURES}{os.pathsep}{SHIPPED}")
