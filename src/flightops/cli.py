"""Command-line entrypoint.

Kept deliberately thin: the CLI parses arguments, configures logging and calls
into the library. Everything worth testing lives in `ingest`, `normalise` and
`quality`, which take a `Settings` object rather than reading the environment
themselves -- so tests can construct configuration directly instead of
mutating `os.environ` and hoping the teardown ran.
"""

from __future__ import annotations

import argparse
import dataclasses
import logging
import sys
from collections.abc import Sequence

from flightops import __version__
from flightops.config import ConfigError, Settings
from flightops.ingest import IngestError, ingest_once
from flightops.normalise import (
    NormaliseError,
    find_bronze_objects,
    normalise,
    write_silver,
)
from flightops.quality import QualityError, assert_quality

logger = logging.getLogger("flightops")

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s %(message)s"

# Attributes every LogRecord carries. Anything outside this set arrived via
# `extra=` and is therefore caller-supplied context worth printing.
_STANDARD_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class ContextFormatter(logging.Formatter):
    """Render `extra={...}` context instead of silently discarding it.

    The modules in this package log with structured context -- state counts,
    attempt numbers, paths, sources. The stdlib's default formatter attaches
    those to the record and then never prints them, which makes the context
    worthless exactly when you need it. This appends them as key=value pairs,
    which greps cleanly and stays readable in a terminal.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        context = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
        }
        if not context:
            return base
        rendered = " ".join(f"{k}={v}" for k, v in sorted(context.items()))
        return f"{base} [{rendered}]"


def _configure_logging(verbosity: int) -> None:
    level = logging.DEBUG if verbosity else logging.INFO
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(ContextFormatter(LOG_FORMAT))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # requests/urllib3 are chatty at DEBUG and drown out our own records.
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="flightops",
        description="Ingest and transform OpenSky flight telemetry into a local lakehouse.",
    )
    parser.add_argument("--version", action="version", version=f"flightops {__version__}")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="debug logging")

    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="fetch one snapshot into the bronze layer")
    ingest.add_argument(
        "--allow-fixture-fallback",
        action="store_true",
        help=(
            "if the live fetch fails, replay committed fixture data instead. "
            "The resulting bronze object is tagged fixture-replay, never as observation."
        ),
    )

    normalise = sub.add_parser(
        "normalise", help="convert bronze snapshots into typed silver Parquet"
    )
    normalise.add_argument(
        "--skip-quality",
        action="store_true",
        help=(
            "write silver without enforcing the quality contract. "
            "Intended for inspecting a bad batch, not for routine use."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Return a process exit code rather than calling sys.exit, so tests can assert on it."""
    args = build_parser().parse_args(argv)
    _configure_logging(args.verbose)

    try:
        settings = Settings.from_env()
    except ConfigError as exc:
        logger.error("invalid configuration: %s", exc)
        return 2

    if args.command == "ingest":
        if args.allow_fixture_fallback:
            settings = replace_fallback(settings, allowed=True)
        try:
            written = ingest_once(settings)
        except IngestError as exc:
            logger.error("ingestion failed: %s", exc)
            return 1
        logger.info("ingest complete: %s", written.as_posix())
        return 0

    if args.command == "normalise":
        try:
            paths = find_bronze_objects(settings)
            if not paths:
                logger.error("no bronze objects under %s", settings.bronze_root.as_posix())
                return 1
            result = normalise(paths)
        except NormaliseError as exc:
            logger.error("normalisation failed: %s", exc)
            return 1

        # Validate before writing. Checking afterwards would leave a bad batch
        # on disk for a downstream reader to find first, which defeats the
        # point of having a contract at this boundary at all.
        if not args.skip_quality:
            try:
                assert_quality(result.table)
            except QualityError as exc:
                logger.error("quality contract failed, nothing written: %s", exc)
                return 3

        written = write_silver(result.table, settings)
        logger.info(
            "normalise complete",
            extra={
                "files_read": result.files_read,
                "rows_in": result.rows_in,
                "rows_out": result.rows_out,
                "duplicates_removed": result.duplicates_removed,
                "partitions": len(written),
            },
        )
        return 0

    return 2


def replace_fallback(settings: Settings, *, allowed: bool) -> Settings:
    """Return a copy of `settings` with the fixture fallback flag overridden.

    A CLI flag has to be able to beat the environment -- that is what a flag is
    for -- but `Settings` is frozen on purpose, so the override is an explicit
    copy rather than an in-place mutation.
    """
    return dataclasses.replace(settings, allow_fixture_fallback=allowed)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
