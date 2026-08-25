"""Guards on the GitHub Actions workflows.

These were originally shell greps inside the security workflow itself. That was
wrong in a way worth recording: a grep for `id-token:\\s*write` run over the
directory containing that very grep matches its own source, so the check failed
on a perfectly clean repository. A pattern-matching check that lives inside the
text it searches is self-referential by construction.

Parsing the YAML fixes it properly. `jobs.*.steps[*].uses` is a structured
field; reading it as structure rather than as text cannot match a comment, a
grep pattern, or a prose mention of an action name.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW_DIR = Path(__file__).parent.parent / ".github" / "workflows"
WORKFLOWS = sorted(WORKFLOW_DIR.glob("*.yml"))
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

# Anything that would let CI hold cloud credentials.
AWS_CREDENTIAL_MARKERS = (
    "aws-actions/",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "role-to-assume",
)


def _load(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(workflow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for job in workflow.get("jobs", {}).values()
        for step in job.get("steps", [])
        if isinstance(step, dict)
    ]


def _uses(workflow: dict[str, Any]) -> list[str]:
    return [step["uses"] for step in _steps(workflow) if "uses" in step]


def _run_commands(path: Path) -> str:
    """Every `run:` script in a workflow, with comment lines removed.

    Reading the raw file instead would match the prose explaining a rule as
    though it were a violation of that rule -- which is exactly the bug this
    module was written to fix, and which the first version of
    `test_ci_installs_only_the_local_and_dev_extras` reproduced by asserting
    `"[aws]" not in body` against a file containing a comment about [aws].
    """
    scripts = [step["run"] for step in _steps(_load(path)) if "run" in step]
    lines = [
        line
        for script in scripts
        for line in script.splitlines()
        if not line.strip().startswith("#")
    ]
    return "\n".join(lines)


def test_both_workflows_exist() -> None:
    assert {p.name for p in WORKFLOWS} == {"ci.yml", "security.yml"}


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_yaml(path: Path) -> None:
    assert _load(path).get("jobs")


# --------------------------------------------------------------------------
# Supply chain
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(path: Path) -> None:
    """A tag is a movable pointer. `@v4` today and `@v4` next month can be
    different code, and whoever compromises an action's repository can retag
    without any workflow file changing."""
    for reference in _uses(_load(path)):
        if reference.startswith("./"):
            continue  # a local action is this repository's own code
        assert "@" in reference, f"{reference} has no ref at all"
        ref = reference.split("@", 1)[1]
        assert SHA_PATTERN.match(ref), f"{reference} is not pinned to a 40-character SHA"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_pinned_actions_keep_a_readable_tag_comment(path: Path) -> None:
    """A bare SHA is unreviewable. The trailing comment is what lets a human
    tell v7.0.1 from an arbitrary commit without opening GitHub."""
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped.startswith("- uses:") and not stripped.startswith("uses:"):
            continue
        if "./" in stripped:
            continue
        assert "#" in stripped, f"pinned action without a version comment: {stripped}"


# --------------------------------------------------------------------------
# No cloud credentials, structurally
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_can_obtain_aws_credentials(path: Path) -> None:
    """The repository is public and the entire design rests on CI never holding
    cloud credentials. Checked against the parsed structure, so a mention in a
    comment or in prose cannot trip it and cannot hide a real one either."""
    workflow = _load(path)

    for job in workflow.get("jobs", {}).values():
        for step in job.get("steps", []):
            if not isinstance(step, dict):
                continue
            rendered = yaml.safe_dump(step)
            for marker in AWS_CREDENTIAL_MARKERS:
                assert marker not in rendered, f"{marker} appears in step {step.get('name')!r}"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_workflow_requests_the_oidc_id_token(path: Path) -> None:
    """`id-token: write` is what makes an OIDC assume-role possible. Without it
    the deploy role is unreachable from CI no matter what else changes."""
    workflow = _load(path)

    scopes = [workflow.get("permissions", {})]
    scopes += [job.get("permissions", {}) for job in workflow.get("jobs", {}).values()]

    for permissions in scopes:
        if isinstance(permissions, dict):
            assert permissions.get("id-token") != "write"


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_default_permissions_are_read_only(path: Path) -> None:
    assert _load(path).get("permissions") == {"contents": "read"}


# --------------------------------------------------------------------------
# Coverage of the gate
# --------------------------------------------------------------------------


def test_ci_runs_every_required_check() -> None:
    """The build contract names these explicitly; a silently dropped step is
    the failure mode this catches."""
    body = _run_commands(WORKFLOW_DIR / "ci.yml")

    for required in (
        "ruff check",
        "ruff format --check",
        "pytest",
        "dbt build",
        "terraform -chdir=infra fmt -check",
        "terraform -chdir=infra validate",
        "tflint",
        "checkov",
    ):
        assert required in body, f"CI does not run {required!r}"


def test_ci_builds_the_lake_without_the_network() -> None:
    """A live fetch with a fallback still makes a request, is still rate
    limited, and still fails for reasons unrelated to the code under test."""
    body = _run_commands(WORKFLOW_DIR / "ci.yml")

    assert "flightops ingest --from-fixtures" in body
    assert "--allow-fixture-fallback" not in body


def test_ci_installs_only_the_local_and_dev_extras() -> None:
    """Installing [aws] would put an AWS SDK on the runner. There is no use for
    one, and the surest way to not misuse a credential is to have no client."""
    body = _run_commands(WORKFLOW_DIR / "ci.yml")

    assert '".[local,dev]"' in body
    assert "[aws]" not in body


def test_security_workflow_scans_full_history() -> None:
    """A secret committed and later 'removed' is still in history and still
    compromised. Scanning only the tip reports clean on exactly that case."""
    workflow = _load(WORKFLOW_DIR / "security.yml")
    checkouts = [step for step in _steps(workflow) if "actions/checkout" in step.get("uses", "")]

    assert any(step.get("with", {}).get("fetch-depth") == 0 for step in checkouts)


def test_security_workflow_is_scheduled() -> None:
    """CI answers 'did this change break anything'. This answers 'did the world
    change under us' -- a CVE published last Tuesday fails no test."""
    workflow = _load(WORKFLOW_DIR / "security.yml")
    # PyYAML parses the bare key `on` as the boolean True.
    triggers = workflow.get("on") or workflow.get(True)

    assert "schedule" in triggers
    assert triggers["schedule"][0]["cron"]


def test_security_workflow_runs_gitleaks_and_pip_audit() -> None:
    body = _run_commands(WORKFLOW_DIR / "security.yml")

    assert "gitleaks detect" in body
    assert "pip-audit" in body


def test_every_job_has_a_timeout() -> None:
    """A hung job holds a runner until GitHub's six-hour default expires."""
    for path in WORKFLOWS:
        for name, job in _load(path).get("jobs", {}).items():
            assert job.get("timeout-minutes"), f"{path.name}:{name} has no timeout"
