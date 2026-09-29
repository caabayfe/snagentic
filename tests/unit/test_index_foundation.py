from datetime import UTC, datetime
from pathlib import Path

from snagentic.artifacts import content_hash
from snagentic.index import ArtifactIndex
from snagentic.models import (
    ArtifactIdentity,
    ArtifactRevision,
    NormalizedArtifact,
    Provenance,
)


def _artifact(
    name: str, content: str, *, domain: str = "global", artifact_type: str = "script_include"
) -> NormalizedArtifact:
    identity = ArtifactIdentity(
        artifact_type=artifact_type,
        scope="global",
        domain_stable_id=domain,
        natural_key=name,
        sys_id=f"sys-{name}",
    )
    now = datetime.now(UTC)
    return NormalizedArtifact(
        identity=identity,
        metadata={"name": name, "active": True},
        content=content,
        revision=ArtifactRevision(
            identity=identity,
            content_hash=content_hash(content),
            observed_at=now,
        ),
        provenance=Provenance(
            source_instance="dev",
            source_table="sys_script_include",
            source_sys_id=identity.sys_id,
            captured_at=now,
        ),
    )


def test_rebuild_and_query_by_text_domain_and_type(tmp_path: Path) -> None:
    index = ArtifactIndex(tmp_path / "index.sqlite3")
    artifacts = [
        _artifact("Alpha", "function alpha() { return 'needle'; }", domain="customer-a"),
        _artifact(
            "Beta",
            "function beta() { return true; }",
            domain="customer-b",
            artifact_type="client_script",
        ),
    ]
    assert index.rebuild(artifacts) == 2
    assert [item.natural_key for item in index.query(text="needle")] == ["Alpha"]
    assert [item.natural_key for item in index.query(domain="customer-b")] == ["Beta"]
    assert [item.natural_key for item in index.query(artifact_type="client_script")] == [
        "Beta"
    ]


def test_rebuild_replaces_stale_rows_and_empty_index_is_queryable(tmp_path: Path) -> None:
    index = ArtifactIndex(tmp_path / "index.sqlite3")
    index.rebuild([_artifact("Old", "old text")])
    index.rebuild([_artifact("New", "new text")])
    assert index.query(text="old") == []
    assert [item.natural_key for item in index.query()] == ["New"]
    assert ArtifactIndex(tmp_path / "missing.sqlite3").query() == []
