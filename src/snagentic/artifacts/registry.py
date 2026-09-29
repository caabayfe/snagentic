"""Allowlisted ServiceNow artifact types and their synchronization capabilities."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from pydantic import Field

from snagentic.models import ArtifactCapability, StrictModel


class ArtifactDefinition(StrictModel):
    artifact_type: str = Field(min_length=1)
    table: str = Field(min_length=1)
    capability: ArtifactCapability
    natural_key_fields: tuple[str, ...] = ("name",)
    code_fields: tuple[str, ...] = ()
    excluded_fields: frozenset[str] = frozenset()


class ArtifactRegistry:
    def __init__(self, definitions: Iterable[ArtifactDefinition]) -> None:
        by_type: dict[str, ArtifactDefinition] = {}
        for definition in definitions:
            if definition.artifact_type in by_type:
                raise ValueError(f"duplicate artifact type: {definition.artifact_type}")
            by_type[definition.artifact_type] = definition
        self._definitions = by_type

    def __iter__(self) -> Iterator[ArtifactDefinition]:
        return iter(self.all())

    def all(self) -> tuple[ArtifactDefinition, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))

    def get(self, artifact_type: str) -> ArtifactDefinition | None:
        return self._definitions.get(artifact_type)

    def require(self, artifact_type: str) -> ArtifactDefinition:
        try:
            return self._definitions[artifact_type]
        except KeyError as exc:
            raise KeyError(f"artifact type is not allowlisted: {artifact_type}") from exc

    def supports_write(self, artifact_type: str) -> bool:
        return self.require(artifact_type).capability == ArtifactCapability.MANAGED_BIDIRECTIONAL


DEFAULT_ARTIFACT_REGISTRY = ArtifactRegistry(
    (
        ArtifactDefinition(
            artifact_type="script_include",
            table="sys_script_include",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("api_name",),
            code_fields=("script",),
        ),
        ArtifactDefinition(
            artifact_type="business_rule",
            table="sys_script",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("collection", "name"),
            code_fields=("script", "condition"),
        ),
        ArtifactDefinition(
            artifact_type="acl",
            table="sys_security_acl",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("type", "name", "operation"),
            code_fields=("script", "condition"),
        ),
        ArtifactDefinition(
            artifact_type="dictionary",
            table="sys_dictionary",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("name", "element"),
        ),
        ArtifactDefinition(
            artifact_type="system_property",
            table="sys_properties",
            capability=ArtifactCapability.EXPORT_ONLY,
            excluded_fields=frozenset({"value"}),
        ),
        ArtifactDefinition(
            artifact_type="client_script",
            table="sys_script_client",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            code_fields=("script",),
        ),
        ArtifactDefinition(
            artifact_type="ui_action",
            table="sys_ui_action",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("name", "table"),
            code_fields=("script", "condition"),
        ),
        ArtifactDefinition(
            artifact_type="ui_policy",
            table="sys_ui_policy",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("short_description", "table"),
            code_fields=("script_true", "script_false"),
        ),
        ArtifactDefinition(
            artifact_type="scripted_rest_api",
            table="sys_ws_definition",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("service_id",),
        ),
        ArtifactDefinition(
            artifact_type="scripted_rest_resource",
            table="sys_ws_operation",
            capability=ArtifactCapability.MANAGED_BIDIRECTIONAL,
            natural_key_fields=("web_service_definition", "name", "http_method"),
            code_fields=("operation_script",),
        ),
        ArtifactDefinition(
            artifact_type="scheduled_job",
            table="sysauto_script",
            capability=ArtifactCapability.EXPORT_ONLY,
            code_fields=("script",),
        ),
        ArtifactDefinition(
            artifact_type="notification",
            table="sysevent_email_action",
            capability=ArtifactCapability.EXPORT_ONLY,
            code_fields=("condition", "advanced_condition"),
        ),
        ArtifactDefinition(
            artifact_type="flow",
            table="sys_hub_flow",
            capability=ArtifactCapability.EXPORT_ONLY,
            natural_key_fields=("internal_name",),
        ),
        ArtifactDefinition(
            artifact_type="subflow",
            table="sys_hub_flow",
            capability=ArtifactCapability.EXPORT_ONLY,
        ),
        ArtifactDefinition(
            artifact_type="update_set",
            table="sys_update_set",
            capability=ArtifactCapability.EXPORT_ONLY,
        ),
        ArtifactDefinition(
            artifact_type="app_version",
            table="sys_app",
            capability=ArtifactCapability.NATIVE_SOURCE_CONTROL,
        ),
        ArtifactDefinition(
            artifact_type="deployment_history",
            table="sys_upgrade_history",
            capability=ArtifactCapability.DIAGNOSTIC_ONLY,
            natural_key_fields=("sys_id",),
        ),
    )
)
