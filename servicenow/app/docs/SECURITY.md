# Security model

Controls are implemented centrally in `SnSourceSecurity` and reused by every
resource.

- Every operation performs an explicit role check.
- Artifact types map to fixed tables and fixed readable/writable field lists.
- Managed writes are limited to script includes, business rules, ACLs,
  dictionary entries, client scripts, UI actions, UI policies, and Scripted
  REST API definitions/resources.
- Scheduled jobs and notifications are export-only and have no writable
  fields.
- System properties are export-metadata-only: the `value` field is neither
  readable nor writable, regardless of its dictionary type or ACLs.
- Change validation rejects create, update, and delete operations unless the
  artifact capability category is exactly `managed_bidirectional`.
- Caller-supplied table names and encoded queries are never accepted.
- Domain and application scope are mandatory for artifact access.
- Bundle writes fail closed unless the configured environment is exactly
  `development`; production and test are explicitly rejected.
- Updates and deletes use `sys_mod_count` plus canonical SHA-256 hash/revision
  optimistic concurrency checks.
- Requests, pages, and diagnostics have hard limits.
- Correlation IDs are generated or accepted only after strict validation.
- Error envelopes are sanitized and never contain exception details.
- Response redaction recursively removes forbidden names and patterns.
- Tombstones use a pending/complete lifecycle; only completed deletion records
  are visible through the API or included in diagnostics.

Forbidden content includes credentials, passwords, tokens, secrets, encryption
material, encrypted properties, journal fields/content, request bodies,
attachments, and unrestricted business records. The allowed artifact tables
are configuration metadata tables only. References are returned only as their
stored scalar values; the export contract does not expand arbitrary referenced
records.

Update sets, application versions, deployment history, flows, and subflows are
not exposed as companion artifact keys. Their local registry classifications
remain export-only, native-source-control, or diagnostic-only metadata and do
not grant companion write access.

ServiceNow ACLs remain authoritative. The application must not grant users
broader access than they already possess. The API's checks are defense in depth,
not an ACL bypass.

## Audit guidance

Log only operation name, correlation ID, caller identifier, outcome, artifact
type, and item count. Never log request bodies, field values, exported scripts,
or exception stack traces. Production logging configuration must redact HTTP
payloads for this API.
