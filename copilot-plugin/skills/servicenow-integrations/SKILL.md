---
name: servicenow-integrations
description: ServiceNow integration and background-processing standards (REST/SOAP outbound, IntegrationHub, events, scheduled jobs, imports and transform maps). Use when ServiceNow must call or be called by another system, or when work should run asynchronously.
---

# ServiceNow integrations and background work

## Outbound calls

- Prefer **IntegrationHub spokes / Flow actions** (retry, logging, credentials built in).
- If scripting, define an outbound **REST Message** record with an authentication
  profile or Connection & Credential alias; call it with `sn_ws.RESTMessageV2(name,
  method)`. Never hardcode endpoints, instance URLs (SN-MNT-002) or credentials
  (SN-SEC-002).
- Never call out from a before/display business rule (SN-PERF-007). Use an async
  business rule, `gs.eventQueue()` + script action, or a flow. Use `executeAsync()` with
  a timeout when a synchronous result is not needed.
- Handle non-2xx responses and timeouts; log status and correlation IDs, not bodies.
- Route through a MID Server for on-premise targets.

## Inbound

- Use **Import Sets + transform maps** (or IntegrationHub ETL / Robust Transform Engine)
  rather than writing target tables directly; coalesce on stable keys.
- Scripted REST APIs: versioned, authenticated, ACL-protected, validated input,
  `GlideRecordSecure`.

## Background processing

- Events (`gs.eventQueue`) plus script actions decouple work from user transactions.
- Scheduled jobs: bounded queries (`setLimit`, `chooseWindow`), idempotent, restartable;
  no `gs.sleep()` (SN-PERF-004).
- Avoid `setWorkflow(false)` (SN-MNT-004) in data fixes unless the change record
  explains which rules are intentionally skipped.
