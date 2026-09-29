# Authoring instance documentation

`snagentic` combines two sources:

- mirrored ServiceNow metadata and derived models provide deterministic technical facts;
- files under `instances/<name>/documentation/` provide reviewed business purpose, process
  meaning, and user instructions.

Generated output is written to `instances/<name>/docs/`. Do not hand-edit generated pages except
during the legacy narrative migration period.

## Information model

Use three document types:

- **Capability**: why the platform feature exists, the benefit, scope, entry points, supported
  processes, and related guides.
- **Process**: trigger, actors, inputs, outputs, steps, decisions, lifecycle, interactions,
  exceptions, controls, and technical evidence.
- **Guide**: role-specific instructions for achieving a user outcome, including prerequisites,
  expected results, troubleshooting, and escalation.

Each file is Markdown with YAML front matter. File names and `id` values must match and use
lowercase letters, digits, and hyphens.

## Create source files

```bash
snagentic instance -i dev docs scaffold --type capability --id incident-management
snagentic instance -i dev docs scaffold --type process --id resolve-incident
snagentic instance -i dev docs scaffold --type guide --id resolve-assigned-incident
```

Optional `documentation/manifest.yaml` controls the site name, default owners, and navigation
order:

```yaml
site_name: Service operations handbook
introduction: >
  Explains the supported service-management capabilities, operating processes, and user tasks.
default_owners:
  - Service Management
capabilities:
  - incident-management
processes:
  - resolve-incident
guides:
  - resolve-assigned-incident
```

## Capability example

```markdown
---
id: incident-management
title: Incident management
summary: Restore normal service quickly and communicate progress to affected users.
status: approved
owners:
  - Incident Management
audiences:
  - end_users
  - process_owners
  - admins
  - developers
benefits:
  - Reduces disruption through consistent prioritization and ownership.
in_scope:
  - Logging, triage, assignment, investigation, resolution, and closure.
out_of_scope:
  - Root-cause analysis after restoration.
entry_points:
  - Service Operations Workspace
processes:
  - resolve-incident
guides:
  - resolve-assigned-incident
evidence:
  - kind: table
    target: incident
reviewed_on: 2026-09-24
review_interval_days: 180
---

## Description

Explain the business boundaries, terminology, and outcomes that cannot be inferred from metadata.
```

## Process example

Process steps form a directed graph. The first step is the entry point, every referenced target
must exist, and every step must be reachable. Use labeled transitions for decisions.

```markdown
---
id: resolve-incident
title: Resolve an incident
summary: Diagnose an assigned incident, restore service, and record the resolution.
status: approved
owners:
  - Incident Management
audiences:
  - end_users
  - process_owners
  - admins
capabilities:
  - incident-management
trigger: An incident is assigned to a support group.
actors:
  - Fulfiller
  - Incident manager
preconditions:
  - The fulfiller can access the assigned incident.
inputs:
  - Incident description and affected service
outputs:
  - Restored service and a documented resolution
guides:
  - resolve-assigned-incident
steps:
  - id: assess
    title: Assess impact and urgency
    actor: Fulfiller
    action: Confirm the affected service, impact, urgency, and available diagnostics.
    next:
      - target: investigate
        label: Standard incident
      - target: escalate
        label: Major impact
    evidence:
      - kind: table
        target: incident
  - id: investigate
    title: Investigate and restore
    actor: Fulfiller
    action: Diagnose the issue, apply a safe resolution, and validate service restoration.
    next:
      - close
  - id: escalate
    title: Start major incident handling
    actor: Incident manager
    action: Follow the approved major incident procedure.
  - id: close
    title: Record and close
    actor: Fulfiller
    action: Record resolution details and confirm closure criteria.
states:
  - id: new
    title: New
    next:
      - in-progress
  - id: in-progress
    title: In progress
    next:
      - resolved
  - id: resolved
    title: Resolved
interactions:
  - source: ServiceNow
    target: Notification service
    message: Notify the caller when the incident is resolved
evidence:
  - kind: event
    target: incident.resolved
reviewed_on: 2026-09-24
review_interval_days: 180
---

## Exceptions and controls

Explain exceptions, approval boundaries, audit controls, and operating policies.
```

## Guide example

```markdown
---
id: resolve-assigned-incident
title: Resolve an assigned incident
summary: Complete the standard resolution steps for an incident assigned to you.
status: approved
owners:
  - Service Desk
audiences:
  - end_users
role: Fulfiller
goal: Restore service and leave a complete resolution record.
processes:
  - resolve-incident
process_steps:
  - resolve-incident:investigate
  - resolve-incident:close
prerequisites:
  - The incident is assigned to you or your group.
steps:
  - title: Review the incident
    instruction: Open the incident and confirm the description, impact, urgency, and affected item.
    expected_result: The incident contains enough information to begin diagnosis.
  - title: Record the resolution
    instruction: Enter the resolution code and concise resolution notes, then resolve the incident.
    expected_result: The incident is resolved and the caller is notified.
troubleshooting:
  - If mandatory information is missing, return the incident to the appropriate triage step.
escalation: Follow the support group's escalation procedure if service cannot be restored safely.
reviewed_on: 2026-09-24
review_interval_days: 180
---

## Additional guidance

Use screenshots only when they materially reduce ambiguity. Store approved images under
`documentation/assets/`, add alt text, and ensure they contain no credentials, personal data, or
business records.
```

## Evidence references

Supported evidence kinds are:

| Kind | Target |
| --- | --- |
| `table` | Table name resolved through `model/tables/<name>.yaml` |
| `artifact` | Instance-relative metadata file or record directory |
| `flow` | Flow/subflow record name or sys_id |
| `catalog_item` | Catalog item name or sys_id |
| `role` | Role name or sys_id |
| `event` | Event name or sys_id |
| `property` | System property name or sys_id; values are never rendered |
| `script_include` | Script include name/API name or sys_id |

Use evidence to support technical claims. Purpose, benefits, ownership, policy, and user procedures
must be reviewed by the responsible people.

## Build, review, and publish

```bash
# Generate the site and fingerprint manifest.
snagentic instance -i dev docs build

# Fail on invalid references, overdue approved documents, or generated drift.
snagentic instance -i dev docs check

# Also compile the complete site with MkDocs strict validation.
snagentic instance -i dev docs check --strict

# Preview locally.
mkdocs serve -f instances/dev/docs/mkdocs.yml
```

`docs check` distinguishes authored review findings from generated-output drift. Changes to
referenced metadata alter document fingerprints and therefore identify which capability, process,
or guide needs review. Automated builds may refresh technical sections, but must not silently
rewrite authored explanations.

## Process-owner-assisted authoring

Use the repository prompt
`.github/prompts/servicenow-process-owner-document.prompt.md` when the documentation should be
developed from a process-owner perspective. The prompt makes Copilot:

1. inspect the instance and existing authored documentation;
2. distinguish verified technical facts from business decisions;
3. interview the process owner for purpose, value, scope, roles, decisions, exceptions, controls,
   KPIs, and user outcomes;
4. create or update capability, process, and guide sources;
5. generate diagrams and evidence pages;
6. run build/check and leave approval with the real process owner.

This role-playing approach is useful for structure and review discipline, but it is not a substitute
for an accountable process owner. Missing business decisions remain explicitly pending instead of
being fabricated.

To preserve existing narrative blocks before moving to the new model:

```bash
snagentic instance -i dev docs migrate
```

This copies them to `documentation/legacy-narratives.yaml`. Legacy blocks continue to feed existing
scope and table pages while content is moved into capability, process, and guide documents.
