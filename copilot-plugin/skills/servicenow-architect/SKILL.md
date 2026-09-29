---
name: servicenow-architect
description: Design ServiceNow changes before building them. Use when a request needs new or changed ServiceNow behaviour (tables, business rules, client scripts, flows, ACLs, integrations, portals, scoped apps) to choose the least-custom, upgrade-safe option and record the design decision.
---

# ServiceNow architect

Decide *how* to meet a requirement before any file under `instances/<name>/metadata/`
is edited. Your output is a short design record that the user approves.

## 1. Understand what already exists

- `snagentic_instance_table` for every table involved: fields, inheritance and all
  behaviour (own and inherited) with paths. Read the scripts it points to.
- `snagentic_instance_search` / `snagentic_instance_refs` for script includes, events and
  properties you plan to call or reuse.
- `snagentic_instance_activity` and `snagentic_instance_collisions` for work in progress.
- `snagentic_instance_review --customized` shows existing technical debt near the change.

## 2. Pick the least-custom option (in this order)

1. **Out-of-box capability or configuration**: an existing plugin, property, choice,
   form/list layout, assignment or approval rule, SLA, notification.
2. **Declarative/low-code**: UI policy (not client script), data policy (server-side
   mandatory), dictionary/reference qualifier, Flow Designer flow or subflow,
   Flow action / IntegrationHub spoke, decision table, Workspace/UI Builder configuration.
3. **Script**, only when 1-2 cannot express it. Put reusable logic in a script include
   and keep business rules, UI actions and client scripts thin.

Justify every step you skip. "Faster to write" is not a reason.

## 3. Upgrade and ownership

- Never edit an out-of-box record when you can add one: deactivate-and-copy, add a new
  business rule with an order, extend a script include (`Object.extendsObject`) or
  override via a new UI action. Editing baseline records is flagged as SN-UPG-002 and
  skipped on upgrade.
- New applications or clearly separable features belong in a **scoped application**;
  do not add new global artifacts for them.
- Extend existing tables (`task`, `cmdb_ci`) only when the new record *is* one of them;
  otherwise create a table in the app scope. Prefer extending over adding many fields to
  a baseline table.

## 4. Security, performance, data

- Every new table needs ACLs for read/write/create/delete (roles first, then conditions,
  script last). See the `servicenow-security` skill.
- Server-side work that is slow or remote goes async (async business rule, event + script
  action, flow). See `servicenow-integrations`.
- No hardcoded sys_ids, URLs or credentials: system properties, lookups by stable keys,
  Connection & Credential aliases.

## 5. Design record (present to the user before building)

```
Requirement:        <one line>
Option chosen:      <OOB | configuration | low-code | script> and why
Options rejected:   <each with the reason>
Scope:              <app scope or global, and why>
Records to change:  <table / name / create|update>, noting any out-of-box record
Security:           <ACLs/roles affected>
Performance:        <sync vs async, query volume>
Upgrade impact:     <baseline records customized, if any>
Test approach:      <ATF test or manual steps>
```

Then build, run `snagentic_instance_plan`, and hand over to the `servicenow-reviewer`
skill before asking for approval.
