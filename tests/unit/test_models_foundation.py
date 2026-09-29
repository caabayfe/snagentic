from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from snagentic.models import (
    ArtifactIdentity,
    ArtifactRevision,
    ChangeKind,
    ChangeOperation,
    Domain,
    Provenance,
)


def test_logical_key_is_stable_and_excludes_sys_id() -> None:
    first = ArtifactIdentity(
        artifact_type="script_include",
        scope="x_app",
        domain_stable_id="customer-a",
        natural_key="Example",
        sys_id="one",
    )
    second = first.model_copy(update={"sys_id": "two"})
    assert first.logical_key == second.logical_key
    assert first.logical_key == '["script_include","x_app","customer-a","Example"]'


def test_revision_requires_timezone_and_sha256_hash() -> None:
    identity = ArtifactIdentity(
        artifact_type="script_include", scope="global", natural_key="Example"
    )
    with pytest.raises(ValidationError):
        ArtifactRevision(
            identity=identity,
            content_hash="not-a-hash",
            observed_at=datetime.now(),
        )


def test_domain_rejects_self_parent() -> None:
    with pytest.raises(ValidationError):
        Domain(stable_id="customer-a", name="Customer A", parent_stable_id="customer-a")


def test_change_operation_enforces_payload_shape() -> None:
    identity = ArtifactIdentity(
        artifact_type="script_include", scope="global", natural_key="Example"
    )
    provenance = Provenance(
        source_instance="dev",
        source_table="sys_script_include",
        captured_at=datetime.now(UTC),
    )
    revision = ArtifactRevision(
        identity=identity,
        content_hash="0" * 64,
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        ChangeOperation(operation=ChangeKind.CREATE, identity=identity)
    operation = ChangeOperation(
        operation=ChangeKind.DELETE,
        identity=identity,
        expected_revision=revision,
    )
    assert operation.expected_revision is revision
    assert provenance.source_instance == "dev"


def test_change_operation_rejects_revision_for_another_artifact() -> None:
    identity = ArtifactIdentity(
        artifact_type="script_include", scope="global", natural_key="Example"
    )
    other = identity.model_copy(update={"natural_key": "Other"})
    revision = ArtifactRevision(
        identity=other,
        content_hash="0" * 64,
        observed_at=datetime.now(UTC),
    )
    with pytest.raises(ValidationError):
        ChangeOperation(
            operation=ChangeKind.DELETE,
            identity=identity,
            expected_revision=revision,
        )
