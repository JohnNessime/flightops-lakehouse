"""Tests for environment-driven configuration.

These use monkeypatch rather than touching os.environ directly so that a
failing test cannot leak state into the rest of the session.
"""

from __future__ import annotations

import pytest

from flightops.config import DEFAULT_BASE_URL, BoundingBox, ConfigError, Settings

PREFIX = "FLIGHTOPS_"
ALL_VARS = [
    "OPENSKY_BASE_URL",
    "USER_AGENT",
    "DATA_ROOT",
    "BRONZE_PREFIX",
    "FIXTURE_DIR",
    "HTTP_TIMEOUT",
    "MAX_RETRIES",
    "BACKOFF_BASE",
    "BACKOFF_MAX",
    "ALLOW_FIXTURE_FALLBACK",
    "BBOX_LAMIN",
    "BBOX_LOMIN",
    "BBOX_LAMAX",
    "BBOX_LOMAX",
]


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ALL_VARS:
        monkeypatch.delenv(PREFIX + var, raising=False)


def test_defaults_work_with_no_environment(clean_env: None) -> None:
    """A clean checkout must run with nothing configured -- the quickstart
    promise in the README depends on this."""
    settings = Settings.from_env()

    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.states_url == f"{DEFAULT_BASE_URL}/states/all"
    assert settings.bbox is None
    assert settings.bronze_root.as_posix() == "data/bronze"
    assert settings.allow_fixture_fallback is False


def test_environment_overrides_are_applied(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(PREFIX + "OPENSKY_BASE_URL", "https://mirror.invalid/api/")
    monkeypatch.setenv(PREFIX + "DATA_ROOT", "/lake")
    monkeypatch.setenv(PREFIX + "BRONZE_PREFIX", "raw")
    monkeypatch.setenv(PREFIX + "MAX_RETRIES", "7")

    settings = Settings.from_env()

    assert settings.states_url == "https://mirror.invalid/api/states/all"
    assert settings.bronze_root.as_posix().endswith("lake/raw")
    assert settings.max_retries == 7


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_booleans(clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(PREFIX + "ALLOW_FIXTURE_FALLBACK", raw)
    assert Settings.from_env().allow_fixture_fallback is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_falsy_booleans(clean_env: None, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv(PREFIX + "ALLOW_FIXTURE_FALLBACK", raw)
    assert Settings.from_env().allow_fixture_fallback is False


def test_nonsense_boolean_raises(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFIX + "ALLOW_FIXTURE_FALLBACK", "maybe")
    with pytest.raises(ConfigError, match="must be a boolean"):
        Settings.from_env()


def test_nonsense_number_raises(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFIX + "HTTP_TIMEOUT", "soon")
    with pytest.raises(ConfigError, match="must be a number"):
        Settings.from_env()


def test_negative_retries_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFIX + "MAX_RETRIES", "-1")
    with pytest.raises(ConfigError, match="must not be negative"):
        Settings.from_env()


def test_zero_timeout_rejected(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(PREFIX + "HTTP_TIMEOUT", "0")
    with pytest.raises(ConfigError, match="must be positive"):
        Settings.from_env()


# --------------------------------------------------------------------------
# Bounding box
# --------------------------------------------------------------------------


def test_full_bounding_box_is_parsed(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in [
        ("BBOX_LAMIN", "45.8"),
        ("BBOX_LOMIN", "6.0"),
        ("BBOX_LAMAX", "47.8"),
        ("BBOX_LOMAX", "10.5"),
    ]:
        monkeypatch.setenv(PREFIX + var, value)

    bbox = Settings.from_env().bbox

    assert bbox is not None
    assert bbox.as_params() == {
        "lamin": "45.8",
        "lomin": "6.0",
        "lamax": "47.8",
        "lomax": "10.5",
    }


def test_partial_bounding_box_fails_loudly(
    clean_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A half-configured box must not silently widen the query to the whole
    world -- that is a real cost and rate-limit event, not a nuisance."""
    monkeypatch.setenv(PREFIX + "BBOX_LAMIN", "45.8")
    monkeypatch.setenv(PREFIX + "BBOX_LOMIN", "6.0")

    with pytest.raises(ConfigError, match="needs all four coordinates"):
        Settings.from_env()


def test_non_numeric_bounding_box_raises(clean_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    for var, value in [
        ("BBOX_LAMIN", "north"),
        ("BBOX_LOMIN", "6.0"),
        ("BBOX_LAMAX", "47.8"),
        ("BBOX_LOMAX", "10.5"),
    ]:
        monkeypatch.setenv(PREFIX + var, value)

    with pytest.raises(ConfigError, match="must be numbers"):
        Settings.from_env()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"lamin": 91.0, "lomin": 0.0, "lamax": 92.0, "lomax": 1.0}, "latitude out of range"),
        ({"lamin": 0.0, "lomin": -181.0, "lamax": 1.0, "lomax": 1.0}, "longitude out of range"),
        ({"lamin": 48.0, "lomin": 0.0, "lamax": 45.0, "lomax": 1.0}, "south of"),
        ({"lamin": 45.0, "lomin": 11.0, "lamax": 48.0, "lomax": 5.0}, "west of"),
    ],
)
def test_invalid_bounding_boxes_rejected(kwargs: dict[str, float], message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        BoundingBox(**kwargs)


def test_valid_bounding_box_accepted() -> None:
    box = BoundingBox(lamin=45.0, lomin=5.0, lamax=48.0, lomax=11.0)
    assert box.as_params()["lamin"] == "45.0"
