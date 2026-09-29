"""Instance registry: one folder per ServiceNow instance under ``instances/<name>/``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

from snagentic.config import AuthConfig, EnvironmentConfig
from snagentic.errors import ConfigurationError

INSTANCE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
INSTANCES_DIRECTORY = Path("instances")
STATE_DIRECTORY = Path(".snagentic")
MIRROR_BRANCH_PREFIX = "servicenow-remote/"
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")
PROPERTY_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
SENSITIVE_PROPERTY_NAME = re.compile(
    r"(^|[._-])(api[_-]?key|auth|credential|credentials|oauth|passwd|password|private[_-]?key|"
    r"pwd|secret|token)([._-]|$)",
    re.IGNORECASE,
)

# Classes mirrored in full, out-of-box records included, so agents can read how the
# platform behaves. "*" means every class extending sys_metadata (business rules, ACLs,
# flows, catalog items, UI Builder, Virtual Agent, Now Assist skills, ATF, ...).
DEFAULT_BASELINE_CLASSES = ("*",)
# Exact classes (not their subclasses) that are too large or derivable to mirror record by
# record; customized records are still mirrored and the table model covers the schema.
DEFAULT_BASELINE_EXCLUDE = (
    "sys_dictionary",          # schema: model/tables/<table>.yaml (subclasses are kept)
    "sys_documentation",       # field labels and help text
    "sys_translated",          # translations
    "sys_security_acl_role",   # ACL roles: listed on each ACL in the table model
    "sys_hub_flow_snapshot",   # compiled copies of flows
    "sys_hub_action_type_snapshot",
)


class ChildTable(BaseModel):
    """A non-metadata table whose rows belong to a mirrored record (for example flow logic
    belongs to its flow). ``parent_field`` references the owning record, or a row of a
    child table listed earlier (legacy workflow activities belong to a version)."""

    model_config = ConfigDict(extra="forbid")

    parent_field: str

    @field_validator("parent_field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]+", value):
            raise ValueError(f"invalid parent field: {value!r}")
        return value


_FLOW_CHILDREN = (
    "sys_hub_trigger_instance_v2",
    "sys_hub_action_instance_v2",
    "sys_hub_flow_logic_instance_v2",
    "sys_hub_sub_flow_instance_v2",
    "sys_hub_flow_stage",
    "sys_hub_trigger_instance",
    "sys_hub_action_instance",
    "sys_hub_sub_flow_instance",
)
# Order matters: a parent child table must precede the tables that reference it.
DEFAULT_CHILD_TABLES: dict[str, str] = {
    **{table: "flow" for table in _FLOW_CHILDREN},
    "sys_hub_step_instance": "action",
    "sys_ui_element": "sys_ui_section",
    "sys_ui_list_element": "list_id",
    "sys_ui_related_list_entry": "list_id",
    "sys_ui_form_section": "sys_ui_form",
    "wf_workflow_version": "workflow",
    "wf_stage": "workflow_version",
    "wf_activity": "workflow_version",
    "wf_condition": "activity",
    "wf_transition": "from",
    "sys_variable_value": "document_key",
}
DEFAULT_DENIED_CLASSES = (
    "sys_documentation",
    "sys_translated_text",
    "sys_ui_message",
    "sys_ux_lib_asset",
    "sys_metadata_link",
    "sys_auth_profile_basic",
    "sys_auth_profile_oauth2",
    "oauth_entity",
    "oauth_entity_profile",
    "discovery_credentials",
    "sys_certificate",
    "sys_alias",
    "sys_connection",
    "sys_cred",
)
DEFAULT_OPERATIONAL_TABLES = (
    "v_plugin",
    "sys_plugins",
    "sys_store_app",
    "domain",
)


class SyncSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # False mirrors only customer updates (records named by a sys_update_xml row);
    # True mirrors every sys_metadata record, including hundreds of thousands out of box.
    include_baseline: bool = False
    # Classes (and their subclasses) mirrored in full even when include_baseline is false;
    # "*" selects every sys_metadata class.
    baseline_classes: list[str] = Field(default_factory=lambda: list(DEFAULT_BASELINE_CLASSES))
    baseline_exclude: list[str] = Field(default_factory=lambda: list(DEFAULT_BASELINE_EXCLUDE))
    # Child rows written to <record>/_children/<table>.yaml (read-only in the mirror).
    child_tables: dict[str, ChildTable] = Field(
        default_factory=lambda: {
            table: ChildTable(parent_field=field) for table, field in DEFAULT_CHILD_TABLES.items()
        }
    )
    # Concurrent Table API requests during fetch.
    workers: int = Field(default=6, ge=1, le=16)
    scopes: list[str] = Field(default_factory=list)
    include_classes: list[str] = Field(default_factory=list)
    deny_classes: list[str] = Field(default_factory=lambda: list(DEFAULT_DENIED_CLASSES))
    # Operational inventory outside sys_metadata. Records are searchable but never writable.
    operational_tables: list[str] = Field(
        default_factory=lambda: list(DEFAULT_OPERATIONAL_TABLES)
    )
    redact_fields: dict[str, list[str]] = Field(
        default_factory=lambda: {"sys_properties": ["value"]}
    )
    # Exact, reviewed property names whose non-secret values may be versioned and changed.
    property_value_allowlist: list[str] = Field(default_factory=list)
    watermark_overlap_seconds: int = Field(default=15 * 3600, ge=0, le=7 * 86400)
    batch_size: int = Field(default=100, ge=1, le=500)
    bulk_page_size: int = Field(default=200, ge=1, le=1000)
    # Generate the derived table model (model/tables/<table>.yaml) on every fetch.
    model: bool = True
    update_set_window_days: int = Field(default=30, ge=0, le=3650)

    @field_validator(
        "scopes", "include_classes", "deny_classes", "baseline_classes", "baseline_exclude",
        "operational_tables",
    )
    @classmethod
    def validate_identifiers(cls, values: list[str]) -> list[str]:
        for value in values:
            if value != "*" and not re.fullmatch(r"[A-Za-z0-9_.$-]+", value):
                raise ValueError(f"invalid identifier in sync settings: {value!r}")
        return values

    @field_validator("property_value_allowlist")
    @classmethod
    def validate_property_value_allowlist(cls, values: list[str]) -> list[str]:
        for value in values:
            if not PROPERTY_NAME.fullmatch(value):
                raise ValueError(f"invalid system property name: {value!r}")
            if SENSITIVE_PROPERTY_NAME.search(value):
                raise ValueError(f"sensitive system property cannot expose its value: {value!r}")
        return values

    @field_validator("child_tables")
    @classmethod
    def validate_child_tables(cls, values: dict[str, ChildTable]) -> dict[str, ChildTable]:
        for table in values:
            if not re.fullmatch(r"[a-z0-9_]+", table):
                raise ValueError(f"invalid child table: {table!r}")
        return values


class DocsSettings(BaseModel):
    """Generated documentation. ``tables`` get a functional behaviour page; tables with
    customized behaviour are always documented as well."""

    model_config = ConfigDict(extra="forbid")

    tables: list[str] = Field(default_factory=lambda: [
        "task", "incident", "problem", "change_request", "sc_request", "sc_req_item",
        "sc_task", "kb_knowledge",
    ])

    @field_validator("tables")
    @classmethod
    def validate_tables(cls, values: list[str]) -> list[str]:
        for value in values:
            if not re.fullmatch(r"[a-z0-9_]{1,80}", value):
                raise ValueError(f"invalid table name: {value!r}")
        return values


class UiSettings(BaseModel):
    """Local (non-SSO) UI login used by Playwright recipes. Environment variable names only."""

    model_config = ConfigDict(extra="forbid")

    username_env: str | None = None
    password_env: str | None = None
    headless: bool = True
    timeout_seconds: float = Field(default=120.0, gt=0, le=3600)

    @field_validator("username_env", "password_env")
    @classmethod
    def validate_env_name(cls, value: str | None) -> str | None:
        if value is not None and not ENV_NAME.fullmatch(value):
            raise ValueError("UI credential settings must be environment variable names")
        return value


class InstanceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    url: HttpUrl
    kind: Literal["development", "test", "production"]
    auth: AuthConfig = Field(default_factory=AuthConfig)
    verify_tls: bool = True
    timeout_seconds: float = Field(default=60.0, gt=0, le=300)
    page_size: int = Field(default=500, ge=1, le=10_000)
    sync: SyncSettings = Field(default_factory=SyncSettings)
    docs: DocsSettings = Field(default_factory=DocsSettings)
    ui: UiSettings = Field(default_factory=UiSettings)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not INSTANCE_NAME.fullmatch(value):
            raise ValueError("instance name must be lowercase letters, digits, '-' or '_'")
        return value

    @property
    def writable(self) -> bool:
        return self.kind == "development"

    @property
    def mirror_branch(self) -> str:
        return f"{MIRROR_BRANCH_PREFIX}{self.name}"

    def ui_credential_names(self) -> tuple[str, str]:
        username = self.ui.username_env or self.auth.username_env
        password = self.ui.password_env or self.auth.password_env
        if not username or not password:
            raise ConfigurationError(
                f"instance {self.name} needs ui.username_env and ui.password_env "
                "(or basic auth) for UI recipes"
            )
        return username, password

    def environment(self) -> EnvironmentConfig:
        return EnvironmentConfig(
            name=self.name,
            url=self.url,
            kind=self.kind,
            auth=self.auth,
            verify_tls=self.verify_tls,
            timeout_seconds=self.timeout_seconds,
            page_size=self.page_size,
        )


@dataclass(frozen=True)
class InstancePaths:
    root: Path
    name: str

    @property
    def relative_workspace(self) -> Path:
        return INSTANCES_DIRECTORY / self.name

    @property
    def workspace(self) -> Path:
        return self.root / self.relative_workspace

    @property
    def config_file(self) -> Path:
        return self.workspace / "instance.yaml"

    @property
    def metadata(self) -> Path:
        return self.workspace / "metadata"

    @property
    def update_sets(self) -> Path:
        return self.workspace / "update-sets"

    @property
    def docs(self) -> Path:
        return self.workspace / "docs"

    @property
    def documentation(self) -> Path:
        return self.workspace / "documentation"

    @property
    def state(self) -> Path:
        return self.root / STATE_DIRECTORY / self.name

    @property
    def mirror_tree(self) -> Path:
        """Private work tree whose ``instances/<name>/`` holds the latest remote snapshot."""

        return self.state / "mirror-tree"

    @property
    def mirror_workspace(self) -> Path:
        return self.mirror_tree / self.relative_workspace

    @property
    def mirror_index(self) -> Path:
        return self.state / "mirror.index"

    @property
    def sync_state(self) -> Path:
        return self.state / "sync-state.json"

    @property
    def catalog_cache(self) -> Path:
        return self.state / "catalog.json"

    @property
    def search_index(self) -> Path:
        return self.state / "index.sqlite"

    @property
    def ui_evidence(self) -> Path:
        return self.state / "ui"


class InstanceRegistry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def names(self) -> list[str]:
        directory = self.root / INSTANCES_DIRECTORY
        if not directory.is_dir():
            return []
        return sorted(
            child.name
            for child in directory.iterdir()
            if (child / "instance.yaml").is_file() and INSTANCE_NAME.fullmatch(child.name)
        )

    def paths(self, name: str) -> InstancePaths:
        if not INSTANCE_NAME.fullmatch(name):
            raise ConfigurationError(f"invalid instance name: {name!r}")
        return InstancePaths(self.root, name)

    def load(self, name: str | None) -> InstanceConfig:
        selected = name or self._default()
        path = self.paths(selected).config_file
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ConfigurationError(f"instance is not configured: {selected} ({path})") from exc
        except yaml.YAMLError as exc:
            raise ConfigurationError(f"invalid YAML in {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigurationError(f"instance configuration must be a mapping: {path}")
        try:
            config = InstanceConfig.model_validate(raw)
        except ValueError as exc:
            raise ConfigurationError(f"{path}: {exc}") from exc
        if config.name != selected:
            raise ConfigurationError(
                f"instance name {config.name!r} does not match its folder {selected!r}"
            )
        return config

    def add(
        self,
        name: str,
        *,
        url: str,
        kind: str,
        auth_mode: str = "basic",
        store: str = "auto",
    ) -> Path:
        paths = self.paths(name)
        if paths.config_file.exists():
            raise ConfigurationError(f"instance already exists: {name}")
        prefix = "SNAGENTIC_" + re.sub(r"[^A-Z0-9]", "_", name.upper())
        auth: dict[str, str] = (
            {"mode": "bearer", "token_env": f"{prefix}_TOKEN"}
            if auth_mode == "bearer"
            else {
                "mode": "basic",
                "username_env": f"{prefix}_USERNAME",
                "password_env": f"{prefix}_PASSWORD",
            }
        )
        if store != "auto":
            auth["store"] = store
        raw = {"name": name, "url": url, "kind": kind, "auth": auth}
        try:
            InstanceConfig.model_validate(raw)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
        paths.workspace.mkdir(parents=True, exist_ok=True)
        paths.config_file.write_text(
            yaml.safe_dump(raw, sort_keys=False, allow_unicode=False), encoding="utf-8"
        )
        return paths.config_file

    def _default(self) -> str:
        names = self.names()
        if len(names) == 1:
            return names[0]
        if not names:
            raise ConfigurationError(
                "no instances are configured; run `snagentic instance add <name> ...`"
            )
        raise ConfigurationError(
            f"several instances are configured ({', '.join(names)}); pass --instance"
        )
