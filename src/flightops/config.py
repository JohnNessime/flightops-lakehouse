"""Runtime configuration, resolved exclusively from the environment.

Why this module exists at all: every value that could differ between a laptop,
CI and AWS -- endpoints, bounding boxes, output roots, retry budgets -- has to
be injectable without editing code, and none of it may be baked into the
repository. A public repo that hardcodes a bucket name or a region leaks
infrastructure detail even when it leaks no credential.

The settings object is frozen. Configuration that can be mutated mid-run is
configuration you cannot reason about from a log line.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

ENV_PREFIX = "FLIGHTOPS_"

# The public, anonymous OpenSky endpoint. Anonymous access is deliberate: an
# OAuth2 client secret is exactly the class of value this repository forbids,
# so the ingest path is designed to need no credential at all.
DEFAULT_BASE_URL = "https://opensky-network.org/api"
DEFAULT_USER_AGENT = "flightops-lakehouse/0.1 (+https://github.com/JohnNessime/flightops-lakehouse)"


class ConfigError(ValueError):
    """Raised when the environment holds a value that cannot be used."""


def _env(name: str, default: str) -> str:
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def _env_float(name: str, default: float) -> float:
    raw = _env(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be a number, got {raw!r}") from exc


def _env_int(name: str, default: int) -> int:
    raw = _env(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_PREFIX}{name} must be an integer, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{ENV_PREFIX}{name} must be a boolean, got {raw!r}")


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """A geographic filter passed to OpenSky as query parameters.

    Restricting the query is a cost and courtesy measure rather than a
    functional one: an unbounded /states/all response is several megabytes and
    tens of thousands of aircraft, which is far more than this project needs to
    demonstrate partitioning, and it consumes a much larger share of a shared
    public API's anonymous rate budget.
    """

    lamin: float
    lomin: float
    lamax: float
    lomax: float

    def __post_init__(self) -> None:
        if not -90.0 <= self.lamin <= 90.0 or not -90.0 <= self.lamax <= 90.0:
            raise ConfigError(f"latitude out of range: {self.lamin}, {self.lamax}")
        if not -180.0 <= self.lomin <= 180.0 or not -180.0 <= self.lomax <= 180.0:
            raise ConfigError(f"longitude out of range: {self.lomin}, {self.lomax}")
        if self.lamin >= self.lamax:
            raise ConfigError(f"lamin ({self.lamin}) must be south of lamax ({self.lamax})")
        if self.lomin >= self.lomax:
            raise ConfigError(f"lomin ({self.lomin}) must be west of lomax ({self.lomax})")

    def as_params(self) -> dict[str, str]:
        return {
            "lamin": str(self.lamin),
            "lomin": str(self.lomin),
            "lamax": str(self.lamax),
            "lomax": str(self.lomax),
        }


@dataclass(frozen=True, slots=True)
class Settings:
    """Everything the ingest path needs, resolved once at startup."""

    base_url: str
    user_agent: str
    bbox: BoundingBox | None
    data_root: Path
    bronze_prefix: str
    fixture_dir: Path
    timeout_seconds: float
    max_retries: int
    backoff_base_seconds: float
    backoff_max_seconds: float
    allow_fixture_fallback: bool

    @property
    def states_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/states/all"

    @property
    def bronze_root(self) -> Path:
        return self.data_root / self.bronze_prefix

    @classmethod
    def from_env(cls) -> Settings:
        """Build settings from the process environment.

        Defaults are chosen so that `flightops ingest` works on a clean
        checkout with no environment set at all -- a five-minute-quickstart
        requirement, and the reason nothing here is mandatory.
        """
        bbox = _bbox_from_env()
        settings = cls(
            base_url=_env("OPENSKY_BASE_URL", DEFAULT_BASE_URL),
            user_agent=_env("USER_AGENT", DEFAULT_USER_AGENT),
            bbox=bbox,
            data_root=Path(_env("DATA_ROOT", "data")).expanduser(),
            bronze_prefix=_env("BRONZE_PREFIX", "bronze"),
            fixture_dir=Path(_env("FIXTURE_DIR", "tests/fixtures")).expanduser(),
            timeout_seconds=_env_float("HTTP_TIMEOUT", 30.0),
            max_retries=_env_int("MAX_RETRIES", 4),
            backoff_base_seconds=_env_float("BACKOFF_BASE", 2.0),
            backoff_max_seconds=_env_float("BACKOFF_MAX", 60.0),
            allow_fixture_fallback=_env_bool("ALLOW_FIXTURE_FALLBACK", default=False),
        )
        if settings.max_retries < 0:
            raise ConfigError(f"{ENV_PREFIX}MAX_RETRIES must not be negative")
        if settings.timeout_seconds <= 0:
            raise ConfigError(f"{ENV_PREFIX}HTTP_TIMEOUT must be positive")
        return settings


def _bbox_from_env() -> BoundingBox | None:
    """Parse the optional bounding box.

    Absent means 'query the whole world', which is valid but heavy; the four
    coordinates are all-or-nothing so that a half-configured box fails loudly
    instead of silently widening the query.
    """
    raw = {
        key: os.environ.get(f"{ENV_PREFIX}BBOX_{key.upper()}")
        for key in ("lamin", "lomin", "lamax", "lomax")
    }
    provided = {k: v for k, v in raw.items() if v is not None}
    if not provided:
        return None
    if len(provided) != 4:
        missing = sorted(set(raw) - set(provided))
        raise ConfigError(
            "a bounding box needs all four coordinates; missing "
            + ", ".join(f"{ENV_PREFIX}BBOX_{m.upper()}" for m in missing)
        )
    try:
        values = {k: float(v) for k, v in provided.items()}
    except ValueError as exc:
        raise ConfigError(f"bounding box coordinates must be numbers: {provided}") from exc
    return BoundingBox(**values)
