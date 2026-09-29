"""Safe, deterministic mapping between artifact identities and workspace paths."""

from __future__ import annotations

from pathlib import Path, PurePath

from snagentic.models import ArtifactIdentity


class DomainPathMapper:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def artifact_directory(self, identity: ArtifactIdentity) -> Path:
        artifact_type = _safe_component(identity.artifact_type, "artifact_type")
        scope = _safe_component(identity.scope, "scope")
        natural_key = _safe_component(identity.natural_key, "natural_key")
        domain = _normalized_domain(identity.domain_stable_id)

        if domain == "global":
            relative = Path("global", artifact_type, natural_key)
        elif scope == "global":
            relative = Path("domains", domain, artifact_type, natural_key)
        else:
            relative = Path("domains", domain, "scopes", scope, artifact_type, natural_key)
        return self._contained(relative)

    def domain_directory(self, stable_id: str) -> Path:
        domain = _normalized_domain(stable_id)
        if domain == "global":
            return self._contained(Path("global"))
        return self._contained(Path("domains", domain))

    def validate_relative_path(
        self, path: Path, *, expected_domain: str | None = None
    ) -> Path:
        if path.is_absolute():
            raise ValueError("workspace paths must be relative")
        parts = PurePath(path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("workspace path contains traversal or empty components")
        if parts[0] == "global":
            actual_domain = "global"
        elif len(parts) >= 2 and parts[0] == "domains":
            actual_domain = _normalized_domain(parts[1])
            if actual_domain == "global":
                raise ValueError("global artifacts must use the global directory")
        else:
            raise ValueError("workspace path must begin with global or domains/<stable-id>")
        if expected_domain is not None:
            normalized_expected = _normalized_domain(expected_domain)
            if actual_domain != normalized_expected:
                raise ValueError(
                    f"path domain {actual_domain!r} does not match expected domain "
                    f"{normalized_expected!r}"
                )
        return self._contained(Path(*parts))

    def _contained(self, relative: Path) -> Path:
        candidate = (self.workspace / relative).resolve()
        if not candidate.is_relative_to(self.workspace):
            raise ValueError("workspace path escapes the configured workspace")
        return candidate


def _normalized_domain(value: str) -> str:
    if value == "global":
        return "global"
    return _safe_component(value, "domain_stable_id")


def _safe_component(value: str, field_name: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise ValueError(f"{field_name} is not a safe path component")
    return value
