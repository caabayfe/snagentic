---
name: servicenow-server-scripting
description: ServiceNow server-side scripting standards for business rules, script includes, scheduled jobs, fix scripts, scripted REST and transform scripts. Use when writing or changing any server-side ServiceNow JavaScript.
---

# ServiceNow server-side scripting

## Business rules

- Choose the timing deliberately:
  - **before**: change fields on `current` (never call `current.update()` — SN-PERF-001).
  - **after**: update *other* records that must be consistent in the same transaction.
  - **async**: anything slow or remote (integrations, heavy recalculation) — SN-PERF-007.
  - **display**: only populate `g_scratchpad` for client scripts.
- Put the filter in the `condition`/filter conditions, not in an `if` around the script,
  so the rule is skipped without compiling the script.
- Wrap the script in the default IIFE `(function executeRule(current, previous) { ... })`.
- Keep rules thin: call a script include for anything reusable.
- Avoid `setWorkflow(false)` (SN-MNT-004). If a data fix truly needs it, say why in a
  comment and in the change description.

## Queries

- `addQuery(field, operator, value)` per condition; do not concatenate encoded queries
  from variables (SN-SEC-003). If an encoded query is unavoidable, only concatenate
  validated sys_ids.
- One query instead of one per row: `addQuery('sys_id', 'IN', ids)`, `GlideAggregate`,
  `addJoinQuery` — never `new GlideRecord` inside `while (gr.next())` (SN-PERF-002).
- Count with `GlideAggregate` + `addAggregate('COUNT')`; test existence with
  `setLimit(1)` + `hasNext()`; never `getRowCount()` (SN-PERF-003).
- `setLimit()` whenever you need only some rows; `chooseWindow()` for paging.
- `get(sys_id)` / `get(field, value)` for single records; check its return value.
- Use `getValue()`/`setValue()` and `getDisplayValue()` instead of dot-walking into
  string coercion.
- `GlideRecordSecure` (or `canRead()`/`canWrite()`) when data is returned to a user.
- `GlideQuery` is fine in new scoped code and avoids many of these mistakes.

## Script includes

- Class name must equal the record name (SN-MNT-005):
  `var MyUtil = Class.create(); MyUtil.prototype = { initialize: function () {}, type: 'MyUtil' };`
- Client-callable includes extend `global.AbstractAjaxProcessor`, validate every
  `getParameter()` value, check roles, and return only what the caller needs.
- Mark includes `accessible from: This application scope only` unless they are an API.

## Never

- `eval`, `new Function`, `GlideEvaluator` on dynamic input (SN-SEC-001).
- `Packages.*` (SN-SEC-005).
- `gs.sleep()` (SN-PERF-004) — schedule the work instead.
- Hardcoded sys_ids (SN-MNT-001), instance URLs (SN-MNT-002) or credentials
  (SN-SEC-002) — use system properties, `gs.getProperty('glide.servlet.uri')`,
  Connection & Credential aliases.
- `gs.log()`/`gs.print()` in scoped apps (SN-MNT-003) — use `gs.info/warn/error/debug`.

## Logging and errors

- Log with a source prefix and no personal or secret data.
- `try { ... } catch (e) { gs.error('MyUtil: ' + e.message); }` around integration and
  parsing code; do not swallow errors silently.
- User-facing messages via `gs.addErrorMessage(gs.getMessage('key'))`.
