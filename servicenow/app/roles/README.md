# Roles and ACL requirements

Roles are deliberately independent; none implies another.

| Role | REST access | Data access |
|---|---|---|
| `x_snagentic_source.export` | capabilities, domains, contexts, inventory, export, tombstones | read only on allowlisted metadata tables and tombstones |
| `x_snagentic_source.diagnostics` | diagnostics | aggregate/read metadata only |
| `x_snagentic_source.validate` | preflight | read only on allowlisted metadata tables |
| `x_snagentic_source.dev_import` | bundle apply | create/update/delete only on the code allowlist and only in development |

Create REST resource ACLs requiring the exact corresponding role. Create table
ACLs for the tombstone table so export can read and dev import can create; no
API role may delete tombstones. Permit dev import to update only the `state`
field so the application can finalize a pending tombstone after deletion.

Do not grant wildcard table ACLs, `admin`, `security_admin`, or broad business
record access through these roles. Existing platform/table/field ACLs continue
to apply. Cross-scope privileges, if required by the selected ServiceNow
release, must name each allowlisted table and operation explicitly.
