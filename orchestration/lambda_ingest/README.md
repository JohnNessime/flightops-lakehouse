# lambda_ingest

The Lambda entrypoint for scheduled bronze ingestion. See
[../README.md](../README.md) for the design and the deployment package.

`handler.py` is the only file here. Its dependencies are vendored into
`build/lambda_ingest` by `make lambda-package`; nothing is committed, because a
deployment package is a build artefact.
