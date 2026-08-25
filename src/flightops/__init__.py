"""flightops — a cost-safe lakehouse over live flight telemetry.

The package exists to keep the ingestion and normalisation layers importable and
unit-testable without any cloud dependency: every module here is pure Python
operating on local paths, so the whole bronze -> silver path runs in CI with no
AWS credentials. Cloud placement is a deployment concern handled in `infra/`.
"""

__version__ = "0.1.0"
