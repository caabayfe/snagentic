# Offline promotion scaffolding

`snagentic` can turn an already generated development change-plan JSON into a
content-free promotion manifest or a PR-oriented report. These commands are
offline-only: they do not load project configuration, credentials, or a
`ServiceNowClient`, and they do not call ServiceNow or GitHub.

```bash
snagentic --json promotion-manifest plan.json \
  --source development --target test --mechanism update_set \
  --created-at 2026-09-20T19:00:00Z --git-commit 0123456789abcdef

snagentic --json change-report plan.json \
  --source development --target production --mechanism servicenow_cicd \
  --created-at 2026-09-20T19:00:00Z
```

The source must be exactly `development`; the target must be exactly `test` or
`production`. Supported deployment mechanisms are exactly `application`,
`update_set`, and `servicenow_cicd`. Clear write/apply/approve/deploy execution
directives and direct-write mechanism or deployment-method aliases are rejected
recursively, including case, hyphen, and underscore variants. Normal source
change operations (`create`, `update`, and `delete`) remain valid. The generated
files are review and deployment-pipeline scaffolding, not deployment
instructions, and never authorize a direct write.

## Validation and safety

The input must use the current `SyncEngine.create_change_plan()` shape:
`{"bundle_id": "...", "changes": [...]}`. Its 32-character source bundle ID is
verified against the first 32 hexadecimal characters of SHA-256 over canonical
JSON for the original `changes` array. This preserves compatibility with the
development change-plan algorithm while detecting missing or tampered
provenance.

Create and update operations require a non-empty `values` object. Its canonical
SHA-256 becomes `artifact_hash`; the values themselves are never emitted.
Update and delete operations also require a valid `expected.hash`. Every
operation receives an `operation_hash` over its content-free canonical
metadata. Operations are sorted by that canonical metadata before output.

Both output schemas contain only:

- schema/kind and promotion bundle identity;
- verified source-plan identity and full changes digest;
- explicit creation, environment, and deployment metadata, plus Git metadata
  when supplied;
- artifact type, operation, domain/scope identifiers, record identifier where
  applicable, and canonical hashes;
- report operation counts.

Arbitrary input fields are not copied. Recursive sanitization removes
case-insensitive secret, credential, token, password, private-key, journal,
work-note, values, content, source, script, code, and body fields from generic
sanitized data. Manifest and report construction additionally use a strict
allowlist, so nested source text and sensitive payloads cannot enter output.

## Determinism and bundle identity

`--created-at` is mandatory caller-supplied RFC3339 UTC metadata. `Z` and
`+00:00` inputs are normalized to `Z`; no wall clock is read. `--git-commit` is
optional. When supplied, it must be a 7-64 character hexadecimal object ID and
is normalized to lowercase. When omitted, the `git_commit` key is absent from
both output and canonical bundle material; it is never emitted as `null` or
fabricated. Identical validated inputs and explicit metadata therefore produce
identical dictionaries and bytes when serialized canonically.

The 64-character `promotion_bundle_id` is SHA-256 over canonical JSON containing
schema version, verified source bundle ID, full canonical source-changes hash,
normalized creation time, environment kinds, deployment mechanism, the
stably ordered content-free operations, and the normalized Git commit only when
one was supplied. Supplied and omitted Git provenance therefore produce
different IDs. The ID field itself is excluded, avoiding a self-reference. The
source bundle ID remains provenance for the development plan; it is not trusted
as the promotion bundle identity.
