# Snagentic Source Exchange

This directory is a Git-friendly source skeleton for a ServiceNow scoped
application. It defines versioned Scripted REST endpoints for safe discovery,
export, validation, development-only change application, tombstones, and
diagnostics.

## API

Base path: `/api/x_snagentic_source/source/v1`

| Method | Resource | Required role |
|---|---|---|
| GET | `/capabilities` | `x_snagentic_source.export` |
| GET | `/domains` | `x_snagentic_source.export` |
| GET | `/contexts` | `x_snagentic_source.export` |
| GET | `/artifacts` | `x_snagentic_source.export` |
| POST | `/artifacts/export` | `x_snagentic_source.export` |
| POST | `/preflight` | `x_snagentic_source.validate` |
| POST | `/change-bundles/apply` | `x_snagentic_source.dev_import` |
| GET | `/tombstones` | `x_snagentic_source.export` |
| GET | `/diagnostics` | `x_snagentic_source.diagnostics` |

The implementation never accepts a table name. Callers select an `artifact_type`
whose table and permitted fields are resolved from a fixed server-side map.
All returned artifacts include explicit `domain` and `application_scope`
objects. Record writes require both `sys_mod_count` and revision/hash checks and
are denied unless the application environment property is exactly
`development`.

See:

- [Installation and packaging](docs/INSTALL.md)
- [Setup](docs/SETUP.md)
- [Endpoint contract](docs/ENDPOINTS.md)
- [Security model](docs/SECURITY.md)
- [Instance-generated metadata](docs/PACKAGING.md)

## Source layout

- `manifest/`: human-maintained application and REST descriptors
- `src/script_includes/`: ES5-compatible server-side modules
- `src/rest_resources/`: Scripted REST resource scripts
- `roles/`: roles and ACL requirements
- `schemas/`: portable JSON contract schemas

These files intentionally omit instance-specific `sys_id` values and update-set
XML. A real ServiceNow instance must create those records and generate its own
metadata before the application can be installed.
