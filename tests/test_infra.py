"""Guards on the Terraform configuration that need no AWS account.

These are text-level assertions on the HCL source. That is a real limitation
and worth stating: they prove the configuration *says* the right thing, not
that AWS *did* the right thing. Only an apply against a real account proves the
latter, and this project deliberately does not do that in CI.

What they do catch is the class of mistake that matters most here — a trust
policy quietly widened to every repository an owner has, or a resource wildcard
turning a deploy role into an account-wide role. Both are one-character edits
that no reviewer reliably notices and that `terraform validate` is perfectly
happy with.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

INFRA = Path(__file__).parent.parent / "infra"
TF_FILES = sorted(INFRA.rglob("*.tf"))
OIDC = INFRA / "modules" / "oidc_role" / "main.tf"
STORAGE = INFRA / "modules" / "lake_storage" / "main.tf"
ATHENA = INFRA / "modules" / "athena" / "main.tf"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _strip_comments(text: str) -> str:
    """Comments explain why a thing is avoided; they must not count as the thing."""
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"(?m)^\s*#.*$", " ", text)


def test_the_four_modules_exist() -> None:
    for module in ("lake_storage", "glue_catalog", "athena", "oidc_role"):
        assert (INFRA / "modules" / module / "main.tf").is_file()


def test_provider_versions_are_pinned() -> None:
    """An unpinned provider means the plan you reviewed and the plan that
    applies next month are different plans."""
    for path in TF_FILES:
        body = _read(path)
        if "required_providers" not in body:
            continue
        assert re.search(r'version\s*=\s*"~>', body), f"{path} does not pin the provider"


def test_no_local_exec_anywhere() -> None:
    """local-exec runs arbitrary commands on whatever applies the plan."""
    for path in TF_FILES:
        assert "local-exec" not in _strip_comments(_read(path)), f"local-exec in {path}"


# --------------------------------------------------------------------------
# The OIDC trust policy
# --------------------------------------------------------------------------


def test_trust_policy_pins_the_subject_with_string_equals() -> None:
    """StringLike with a trailing wildcard is the standard way this gets
    quietly widened to every repository an owner has."""
    body = _strip_comments(_read(OIDC))
    subject_block = body[body.index("token.actions.githubusercontent.com:sub") - 200 :][:400]

    assert 'test     = "StringEquals"' in subject_block
    assert "StringLike" not in subject_block


def test_trust_policy_constrains_the_audience() -> None:
    body = _strip_comments(_read(OIDC))

    assert "token.actions.githubusercontent.com:aud" in body
    assert '"sts.amazonaws.com"' in body


def test_subject_claim_includes_both_repository_and_ref() -> None:
    """Scoping to a repository but not a ref means any branch, which means any
    pull request, which means anyone who can open one."""
    body = _read(OIDC)

    assert "repo:${var.github_repository}:ref:${var.github_ref}" in body


def test_wildcards_in_repository_or_ref_are_rejected_by_validation() -> None:
    body = _read(INFRA / "modules" / "oidc_role" / "variables.tf")

    assert 'strcontains(var.github_repository, "*")' in body
    assert 'strcontains(var.github_ref, "*")' in body


# --------------------------------------------------------------------------
# Least privilege
# --------------------------------------------------------------------------


def test_no_policy_statement_uses_a_bare_resource_wildcard() -> None:
    """`Resource = "*"` turns a deploy role into an account-wide role."""
    offenders: list[str] = []
    for path in TF_FILES:
        body = _strip_comments(_read(path))
        if re.search(r'resources\s*=\s*\[\s*"\*"\s*\]', body):
            offenders.append(path.name)

    assert not offenders, f"bare resource wildcard in {offenders}"


def test_deploy_policy_scopes_glue_to_one_database() -> None:
    """Table-level wildcards are unavoidable because dbt creates tables
    dynamically. They must be scoped inside a single named database."""
    body = _read(OIDC)

    assert "database/${var.glue_database_name}" in body
    assert "table/${var.glue_database_name}/*" in body
    assert "table/*" not in body, "a catalog-wide table wildcard is not scoped"


def test_deploy_policy_scopes_s3_to_named_prefixes() -> None:
    body = _read(OIDC)

    assert '"${var.bucket_arn}/${prefix}/*"' in body
    assert '"${var.bucket_arn}/*"' not in body, "the role must not reach the whole bucket"


def test_athena_access_is_scoped_to_the_capped_workgroup() -> None:
    """A role able to query in any workgroup can query in one with no cost
    ceiling, which makes the ceiling a convention rather than a control."""
    body = _read(OIDC)

    assert "resources = [var.athena_workgroup_arn]" in body


def test_session_duration_is_bounded() -> None:
    body = _read(INFRA / "modules" / "oidc_role" / "variables.tf")

    assert "max_session_duration" in body
    assert "default     = 3600" in body


# --------------------------------------------------------------------------
# Cost controls
# --------------------------------------------------------------------------


def test_athena_workgroup_enforces_its_own_configuration() -> None:
    """Without enforcement the byte cutoff is a default any client may
    override, which is not a guardrail."""
    body = _strip_comments(_read(ATHENA))

    assert "enforce_workgroup_configuration = true" in body
    assert "bytes_scanned_cutoff_per_query" in body


def test_bytes_scanned_cutoff_default_is_ten_gib() -> None:
    body = _read(INFRA / "variables.tf")

    assert "10737418240" in body


def test_no_crawler_or_glue_job_anywhere() -> None:
    """Crawlers bill per DPU-hour with a 10-minute minimum. See ADR 0002."""
    for path in TF_FILES:
        body = _strip_comments(_read(path))
        assert "aws_glue_crawler" not in body, f"crawler in {path}"
        assert "aws_glue_job" not in body, f"glue job in {path}"


@pytest.mark.parametrize(
    "forbidden",
    [
        "aws_nat_gateway",
        "aws_vpc",
        "aws_ecs_cluster",
        "aws_redshift_cluster",
        "aws_mwaa_environment",
    ],
)
def test_no_expensive_always_on_resources(forbidden: str) -> None:
    """A NAT Gateway alone is ~$32/month, dwarfing everything else here."""
    for path in TF_FILES:
        assert forbidden not in _strip_comments(_read(path)), f"{forbidden} in {path}"


# --------------------------------------------------------------------------
# Storage hardening
# --------------------------------------------------------------------------


def test_all_four_public_access_block_settings_are_true() -> None:
    """Three of four is not blocked."""
    body = _strip_comments(_read(STORAGE))

    for setting in (
        "block_public_acls",
        "block_public_policy",
        "ignore_public_acls",
        "restrict_public_buckets",
    ):
        assert re.search(rf"{setting}\s*=\s*true", body), f"{setting} is not true"


def test_encryption_and_versioning_are_enabled() -> None:
    body = _strip_comments(_read(STORAGE))

    assert "aws_s3_bucket_server_side_encryption_configuration" in body
    assert 'sse_algorithm = "AES256"' in body
    assert 'status = "Enabled"' in body


def test_insecure_transport_is_denied() -> None:
    body = _strip_comments(_read(STORAGE))

    assert "aws:SecureTransport" in body
    assert 'effect = "Deny"' in body


def test_lifecycle_covers_expiry_transition_and_aborted_uploads() -> None:
    body = _strip_comments(_read(STORAGE))

    assert "expire-bronze" in body
    assert "STANDARD_IA" in body
    assert "abort_incomplete_multipart_upload" in body


# --------------------------------------------------------------------------
# Suppressions must justify themselves
# --------------------------------------------------------------------------


def test_every_checkov_suppression_carries_a_real_reason() -> None:
    """A suppression is the one thing in this directory a reviewer should be
    most suspicious of. `skip=CKV_X:wontfix` must not pass review, so it does
    not pass here either."""
    pattern = re.compile(r"checkov:skip=(?P<check>[A-Z0-9_]+):(?P<reason>.*)")
    found = 0

    for path in TF_FILES:
        for match in pattern.finditer(_read(path)):
            found += 1
            reason = match.group("reason").strip()
            assert len(reason) >= 60, (
                f"{match.group('check')} in {path.name} has a {len(reason)}-character "
                f"justification: {reason!r}"
            )

    assert found > 0, "the test is meaningless if it matches nothing"


def test_no_twelve_digit_strings_in_infra() -> None:
    """An AWS account id is 12 digits and gitleaks does not flag it."""
    for path in INFRA.rglob("*"):
        if not path.is_file() or ".terraform" in path.parts:
            continue
        if path.suffix in {".tf", ".example", ".md", ".hcl"} or path.name.endswith(
            ".tfvars.example"
        ):
            body = path.read_text(encoding="utf-8", errors="ignore")
            hits = [h for h in re.findall(r"\b\d{12}\b", body) if h != "10737418240"]
            assert not hits, f"12-digit string {hits} in {path}"
