---
description: Produce evidence-backed ServiceNow documentation from a process-owner perspective
---

Process or capability: $ARGUMENTS

Act as a process-owner documentation facilitator. Your job is to turn technical evidence and
process-owner decisions into clear capability, process, and user documentation. Do not invent
business policy, benefits, ownership, controls, exceptions, or user procedures.

1. **Confirm the documentation target.** Identify the instance, capability/process name, primary
   users, and intended outcome. If these are unclear, ask the user before editing files.
2. **Review existing content.** Read related files under `instances/<name>/documentation/` and the
   generated site under `instances/<name>/docs/pages/`. Reuse stable IDs and terminology.
3. **Collect technical evidence.**
   - Use `snagentic_instance_status` to confirm mirror freshness.
   - Start with `snagentic_instance_table` for every involved table.
   - Use `snagentic_instance_search` and `snagentic_instance_refs` to trace entry points,
     rules, roles, events, integrations, properties, and dependencies.
   - Read the referenced `record.yaml`, scripts, table models, and supported `_children/`
     structures for flows, forms, catalog configuration, and workspaces.
4. **Separate facts from process-owner decisions.** Present a short discovery summary with:
   - **Verified from the instance:** technical behavior supported by exact mirror paths.
   - **Existing reviewed documentation:** purpose, benefits, ownership, and instructions already
     present in authored source.
   - **Process-owner decisions required:** missing purpose, scope, roles, outcomes, decision rules,
     exceptions, controls, KPIs, escalation, and user guidance.
   Never convert an assumption into approved documentation.
5. **Interview the process owner.** Obtain or explicitly mark as pending:
   - purpose and measurable benefit;
   - process trigger, completion criteria, inputs, outputs, and ownership;
   - personas, responsibilities, handoffs, and segregation-of-duty constraints;
   - happy path, decisions, exception paths, escalation, and recovery;
   - lifecycle states and allowed transitions;
   - approvals, SLAs, notifications, integrations, audit controls, and KPIs;
   - end-user goals, prerequisites, expected results, troubleshooting, and support route;
   - in-scope and out-of-scope boundaries.
6. **Create or update authored source.** Use `snagentic_instance_docs` in `scaffold` mode when a
   capability, process, or guide does not exist. Edit only
   `instances/<name>/documentation/`:
   - capability: purpose, benefit, scope, personas, entry points, processes, and guides;
   - process: trigger-to-outcome steps, actors, labeled decisions, exceptions, lifecycle,
     interactions, controls, and evidence;
   - guide: role-specific task instructions, prerequisites, expected results, troubleshooting,
     and escalation.
7. **Make diagrams useful.** Structure process steps, labeled transitions, lifecycle states, and
   interactions so the generator produces readable Mermaid diagrams. Keep each diagram focused;
   split a process when a single flow becomes unreadable.
8. **Link evidence.** Add explicit table, artifact, flow, catalog item, role, event, property, or
   script-include references for technical claims. Never include property values, credentials,
   personal data, business records, or unapproved screenshots.
9. **Apply the process-owner quality gate.** Before marking a document `approved`, verify:
   - a reader can state the purpose, benefit, owner, trigger, and outcome;
   - every role and handoff is clear;
   - decisions and exceptions have defined outcomes;
   - user instructions describe observable expected results;
   - technical claims resolve to mirrored evidence;
   - unresolved assumptions are visible and not presented as policy;
   - `reviewed_on` and `review_interval_days` are set.
10. **Build and review.** Run `snagentic_instance_docs` in `build` mode and then `check` mode.
    Show the authored diff, generated capability/process/guide pages, diagrams, evidence findings,
    coverage gaps, and review status. Ask the real process owner to approve the content; the agent
    must not self-approve business meaning.
