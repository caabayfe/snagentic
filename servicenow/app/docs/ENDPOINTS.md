# Endpoint contract

All endpoints are under `/api/x_snagentic_source/source/v1`, require
authentication, accept or generate `X-Correlation-ID`, and return it in both
the response header and JSON envelope.

Success envelope:

```json
{"ok":true,"correlation_id":"...","data":{},"meta":{}}
```

Error envelope:

```json
{"ok":false,"correlation_id":"...","error":{"code":"...","message":"...","details":[]}}
```

Errors never include stack traces, request bodies, journal content, or raw
record values.

## `GET /capabilities`

Returns capabilities using only these categories:

- `native_source_control`
- `managed_bidirectional`
- `export_only`
- `diagnostic_only`
- `excluded`

The response also includes an explicit `data.artifact_types` array derived from
the server-side artifact allowlist. Each entry contains:

```json
{"key": "script_include", "category": "managed_bidirectional"}
```

Clients should use this array instead of probing unrecognized artifact type
keys. The array is sorted by key and never contains table names.

The companion allowlist currently reports:

| Artifact type | Category |
|---|---|
| `acl` | `managed_bidirectional` |
| `business_rule` | `managed_bidirectional` |
| `client_script` | `managed_bidirectional` |
| `dictionary` | `managed_bidirectional` |
| `notification` | `export_only` |
| `scheduled_job` | `export_only` |
| `script_include` | `managed_bidirectional` |
| `scripted_rest_api` | `managed_bidirectional` |
| `scripted_rest_resource` | `managed_bidirectional` |
| `system_property` | `export_only` |
| `ui_action` | `managed_bidirectional` |
| `ui_policy` | `managed_bidirectional` |

`system_property` exports metadata only. Its `value` field is never readable or
writable through this API. Export-only types have no writable fields.

The Python registry also describes update sets, application versions,
deployment history, flows, and subflows for local classification. They are not
companion artifact keys: update sets, flows, and subflows require proven safe
round-trip semantics; application versions remain native source-control
metadata; deployment history remains diagnostic metadata.

## `GET /domains`

Lists visible domains, bounded to 200 records. Parameters:

- `limit`: 1-200, default 100
- `cursor`: prior page `next_cursor` (`sys_id`)

## `GET /contexts`

Discovers domain/application-scope partitions containing at least one readable
record from the fixed artifact allowlist. It requires
`x_snagentic_source.export` and never queries caller-selected tables or
business-record tables.

Each `data.items` entry contains:

```json
{
  "domain": {"sys_id": "...", "name": "...", "path": "..."},
  "application_scope": {"sys_id": "...", "name": "...", "scope": "..."}
}
```

Parameters:

- `limit`: 1-200, default 100
- `cursor`: prior page `next_cursor`, formatted as
  `<domain_sys_id>:<application_scope_sys_id>`

Only records with explicit, readable `sys_domain` and `sys_scope` values are
considered. A record without either value is skipped; it is never inferred to
belong to the global domain or scope. Candidate discovery is capped at 1,000
distinct context groups per allowlisted artifact table and fails closed if the
cap is exceeded.

## `GET /artifacts`

Inventory metadata only; script/content fields are omitted. Parameters:

- `artifact_type`: fixed allowlist key
- `domain_id`: required
- `scope_id`: required
- `limit`: 1-200
- `cursor`: prior page `next_cursor`

## `POST /artifacts/export`

Exports at most 100 explicitly identified artifacts. See
`artifact-export-request.schema.json`. Sensitive and journal fields are
forbidden even if later added to a table dictionary.

## `POST /preflight`

Validates a change bundle without writing. Checks schema, allowlists,
domain/scope presence, duplicate targets, allowed fields, and concurrency.
Maximum 50 changes and a 512 KiB parsed JSON representation.
Only artifact types reported as `managed_bidirectional` may appear in change
bundles. The server rejects create, update, and delete operations for every
other capability category before operation-specific validation. Export-only
types also have empty writable field lists.

## `POST /change-bundles/apply`

Applies a bundle only in development and only for a caller with
`x_snagentic_source.dev_import`. The entire bundle is preflighted first.
Updates/deletes require matching `sys_mod_count`, `revision`, and `hash`.
Creates require explicit domain and scope. Processing stops on the first
failure; this is not a database transaction, so the response reports completed
items for reconciliation. Deletes create a `pending` tombstone before deleting
the artifact and mark it `complete` only after deletion succeeds. API reads and
diagnostic counts exclude pending tombstones, which an administrator must
reconcile if a delete or finalization fails.

Change bundles are defined only for `managed_bidirectional` types. Create,
update, and delete changes for `export_only`, `native_source_control`,
`diagnostic_only`, or excluded types are rejected.

When an operation fails, `error.details[0]` contains `failed_index`,
`completed_count`, and a sanitized `completed` array describing every earlier
operation that committed. Completed entries contain only operation identity,
domain/scope, revision/hash, and tombstone status; artifact values and content
are never included. The server does not automatically roll back or retry
completed operations.

## `GET /tombstones`

Lists tombstones filtered by required `domain_id` and `scope_id`, with bounded
cursor pagination. Only completed tombstones are returned. Tombstones contain
identity and concurrency metadata only, never deleted record content.

## `GET /diagnostics`

Returns bounded operational metadata: environment classification, configured
limits, allowlist keys, and aggregate tombstone counts for a short window. It
never returns request bodies, journal content, attachments, credentials,
encrypted properties, arbitrary business records, or record payloads.
