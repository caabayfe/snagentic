# Setup

## Application properties

Create these scoped system properties:

| Property | Type | Required value/default |
|---|---|---|
| `x_snagentic_source.environment` | string | `development`, `test`, or `production`; default `production` |
| `x_snagentic_source.export_page_size` | integer | default `100`, maximum `200` |
| `x_snagentic_source.diagnostic_window_minutes` | integer | default `15`, maximum `60` |

The write gate permits bundle application only when
`x_snagentic_source.environment=development`. Missing, `test`, `production`, or
any other value is denied. It also denies writes when platform production/test
signals are present.

Properties must not contain secrets. The API never exports property values.

## Domain separation

Domain separation remains enforced by ServiceNow ACL/query behavior. The API
adds an explicit domain constraint to artifact queries. The caller must provide
a domain `sys_id` for inventory/export and each change item must carry a domain
object with `sys_id`. Global-domain access requires the same explicit identifier.

## Scope

Each query is constrained by an explicit application-scope `sys_id`. Every
artifact response includes scope `sys_id`, scope name, and display name. Change
items must carry an application scope and cannot move an existing record to a
different scope or domain.

## Operational setup

- Assign the least-privileged roles described in `../roles/README.md`.
- Keep `dev_import` separate from export and validation assignments.
- Enable REST request/response logging only at metadata level. Never log request
  bodies or response payloads.
- Configure rate limiting at the Scripted REST API level.
- Verify table cross-scope access for only the tables in the code allowlist.
- Do not add generic table or encoded-query parameters.
- Retain tombstones according to organizational policy and purge them only
  through an administrator-controlled job, not this API.
