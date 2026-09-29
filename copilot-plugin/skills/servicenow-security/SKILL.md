---
name: servicenow-security
description: ServiceNow security standards for ACLs, roles, client-callable script includes, scripted REST APIs, credentials and data exposure. Use when creating tables, ACLs, roles, APIs or any code that returns data to users or external systems.
---

# ServiceNow security

## Access control

- Every new table gets ACLs for `read`, `write`, `create` and `delete` (and field ACLs
  for sensitive fields). Table defaults from the app creator are a starting point, not
  a review.
- Evaluate in this order: **roles**, then **condition**, then **script** only if the
  first two cannot express it. A script that always returns true adds nothing
  (SN-SEC-004).
- Grant roles to groups, never to users. Create app roles (`x_app.user`,
  `x_app.admin`) that contain platform roles, not the other way round.
- Do not uncheck *Admin overrides* without a reason; do not use `security_admin`
  elevation in code.
- Deny-by-default: when unsure, remove access and add a narrow allow.

## Code that exposes data

- Client-callable script includes: `isPublic()` returns false; check
  `gs.hasRole()` / `canRead()` on every method; validate `getParameter()` values; return
  only needed fields.
- Scripted REST: require authentication and an ACL/role on the resource; validate the
  request body; use `GlideRecordSecure`; set explicit status codes.
- Never build encoded queries from input (SN-SEC-003), never `eval` input
  (SN-SEC-001).
- Escape user data in Jelly (`${JS:...}`/`${HTML:...}`) and portal templates.

## Secrets

- No passwords, tokens, API keys or Authorization headers in scripts or properties
  (SN-SEC-002). Use Connection & Credential aliases, credential records or `password2`
  fields; read them with the platform APIs.
- Do not log request bodies, headers or personal data.
- snagentic never mirrors property values unless allowlisted; do not add secret
  properties to `property_value_allowlist`.

## Review questions

- Who can read/write each new table and field? Show the ACL list.
- Can an unauthenticated or low-privilege user reach the new endpoint or AJAX method?
- Does any field hold personal or sensitive data that needs field-level ACLs,
  encryption or data classification?
