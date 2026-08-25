"""Shared test fixtures, plus a hard guarantee that the suite is offline.

The build contract says tests must not touch the network or AWS. Saying so in a
document is not enforcement, so `_no_network` below actively severs outbound
sockets for the whole session: any test that reaches the network fails with a
pointed message instead of quietly passing on a developer machine and hanging
in CI.
"""

from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from flightops.config import BoundingBox, Settings

FIXTURE_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True, scope="session")
def _no_network() -> Iterator[None]:
    """Fail loudly on any real outbound connection attempt."""
    real_connect = socket.socket.connect

    def guard(self: socket.socket, address: Any) -> None:
        raise RuntimeError(
            f"test attempted a real network connection to {address!r}; mock the HTTP layer instead"
        )

    socket.socket.connect = guard  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """A complete Settings object pointed at a temporary data root.

    Constructed directly rather than through `from_env` so tests never mutate
    the process environment and never depend on the developer's shell.
    """
    return Settings(
        base_url="https://opensky.invalid/api",
        user_agent="flightops-tests/0.1",
        bbox=BoundingBox(lamin=45.0, lomin=5.0, lamax=48.0, lomax=11.0),
        data_root=tmp_path / "data",
        bronze_prefix="bronze",
        silver_prefix="silver",
        fixture_dir=FIXTURE_DIR,
        timeout_seconds=5.0,
        max_retries=2,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.05,
        allow_fixture_fallback=False,
    )


@pytest.fixture
def states_payload() -> dict[str, Any]:
    """A small, hand-built payload with known values for exact assertions.

    Real captured fixtures are used where realism matters; this one exists so
    tests can assert on specific numbers without pinning them to whatever
    happened to be flying over Switzerland on the capture date.
    """
    return {
        "time": 1_700_000_000,  # 2023-11-14T22:13:20Z
        "states": [
            [
                "abc123",
                "TEST123 ",
                "Switzerland",
                1_700_000_000,
                1_700_000_000,
                8.5,
                47.4,
                11000.0,
                False,
                230.0,
                90.0,
                0.0,
                None,
                11200.0,
                "1000",
                False,
                0,
            ],
            [
                "def456",
                "TEST456 ",
                "France",
                1_699_999_990,
                1_700_000_000,
                7.1,
                46.2,
                0.0,
                True,
                0.0,
                180.0,
                0.0,
                None,
                0.0,
                "2000",
                False,
                0,
            ],
        ],
    }


@pytest.fixture
def captured_fixture_names() -> list[str]:
    return sorted(p.name for p in FIXTURE_DIR.glob("states_*.json"))


def read_envelope(path: Path) -> dict[str, Any]:
    """Helper: load a written bronze object."""
    return json.loads(path.read_text(encoding="utf-8"))
