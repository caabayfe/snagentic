from pathlib import Path

import pytest

from snagentic.domains import DomainPathMapper
from snagentic.models import ArtifactIdentity


def test_maps_global_and_domain_artifacts(tmp_path: Path) -> None:
    mapper = DomainPathMapper(tmp_path)
    global_identity = ArtifactIdentity(
        artifact_type="script_include", scope="global", natural_key="example"
    )
    domain_identity = ArtifactIdentity(
        artifact_type="client_script",
        scope="x_app",
        domain_stable_id="customer-a",
        natural_key="example",
    )
    assert mapper.artifact_directory(global_identity) == (
        tmp_path / "global/script_include/example"
    )
    assert mapper.artifact_directory(domain_identity) == (
        tmp_path / "domains/customer-a/scopes/x_app/client_script/example"
    )


@pytest.mark.parametrize("component", ["..", "../other", "a/b", "a\\b", "\x00"])
def test_rejects_unsafe_identity_components(tmp_path: Path, component: str) -> None:
    mapper = DomainPathMapper(tmp_path)
    identity = ArtifactIdentity(
        artifact_type="script_include",
        scope="global",
        domain_stable_id=component,
        natural_key="example",
    )
    with pytest.raises(ValueError):
        mapper.artifact_directory(identity)


def test_rejects_cross_domain_and_ambiguous_paths(tmp_path: Path) -> None:
    mapper = DomainPathMapper(tmp_path)
    with pytest.raises(ValueError):
        mapper.validate_relative_path(
            Path("domains/customer-b/script_include/example"),
            expected_domain="customer-a",
        )
    with pytest.raises(ValueError):
        mapper.validate_relative_path(Path("domains/global/script_include/example"))
    with pytest.raises(ValueError):
        mapper.domain_directory("")
