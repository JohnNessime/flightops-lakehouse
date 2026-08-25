"""Smoke tests for the package skeleton.

These exist so the test gate is meaningful from Phase 1 onward: an empty test
suite exits non-zero under pytest and would otherwise force the CI gate to be
written with a special case that quietly survives into later phases.
"""

import flightops


def test_package_imports() -> None:
    assert flightops.__doc__


def test_version_is_exposed() -> None:
    assert flightops.__version__ == "0.1.0"
